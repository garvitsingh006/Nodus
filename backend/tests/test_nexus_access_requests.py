"""NODUS Stage 09B: access-request lifecycle tests against the live, already
imported NEXUS dataset (see ``backend.tests.nexus_live`` for why).

Every test here creates throwaway request/grant/decision rows with fresh
UUIDs (via the service functions themselves, which mint their own ids) and
deletes them in ``finally``, mirroring ``temporarily_revoked_active_grant`` in
``test_nexus_authorization.py`` — the shared dataset must never be left
corrupted.
"""
from __future__ import annotations

import pytest

from backend.app.governance.access_requests import (
    approve_request, create_access_request, deny_request, revoke_grant,
)
from backend.app.governance.identity import (
    authorized_actions_for_source, isolated_demo_mode_enabled, principal_authorizes_action,
)
from backend.app.security import Permission
from backend.tests.nexus_live import (
    admin_principal, developer_principal, intern_principal, nexus_neo4j, nexus_pg, nexus_settings, resolve,
)

pytestmark = pytest.mark.integration

_COMPANY_ID = "nexus"


def _cleanup_request(nexus_pg, request_id: str | None):
    if not request_id:
        return
    with nexus_pg.transaction() as cur:
        cur.execute("DELETE FROM nexus_access_decisions WHERE request_id=%s", (request_id,))
        cur.execute("DELETE FROM nexus_access_requests WHERE request_id=%s", (request_id,))


def _cleanup_grant(nexus_pg, grant_id: str | None):
    if not grant_id:
        return
    with nexus_pg.transaction() as cur:
        cur.execute("DELETE FROM nexus_access_decisions WHERE grant_id=%s", (grant_id,))
        cur.execute("DELETE FROM nexus_resource_grants WHERE grant_id=%s", (grant_id,))


def _restricted_resource_for_developer(nexus_pg, developer_principal) -> str:
    """An ADMINISTRATIVE_RECORDS source the developer does not currently hold."""
    rows = nexus_pg.read(
        "SELECT source_id FROM nexus_sources WHERE classification='ADMINISTRATIVE_RECORDS' ORDER BY source_id")
    assert rows, "fixture assumption broken: no ADMINISTRATIVE_RECORDS sources imported"
    for row in rows:
        if row["source_id"] not in developer_principal.source_ids:
            return row["source_id"]
    pytest.skip("Developer already holds every ADMINISTRATIVE_RECORDS source; cannot exercise a fresh grant")


def test_full_lifecycle_request_approve_narrower_scope(nexus_pg, developer_principal):
    employee_id = developer_principal.subject.removeprefix("nexus:")
    resource_id = _restricted_resource_for_developer(nexus_pg, developer_principal)
    unrelated_resource_rows = nexus_pg.read(
        "SELECT source_id FROM nexus_sources WHERE classification='ADMINISTRATIVE_RECORDS' "
        "AND source_id != %s ORDER BY source_id", (resource_id,))
    unrelated_resource = unrelated_resource_rows[0]["source_id"] if unrelated_resource_rows else None

    request_id = None
    grant_id = None
    try:
        request = create_access_request(
            nexus_pg, requester_employee_id=employee_id, company_id=_COMPANY_ID,
            resource_id=resource_id, requested_actions=["READ", "EXPORT"],
            justification="Investigating an incident", requested_duration_hours=48)
        request_id = request["request_id"]
        assert request["status"] == "PENDING"

        # No grant yet: developer still lacks access.
        refreshed = resolve(_demo_token(nexus_pg, employee_id), nexus_pg)
        assert resource_id not in refreshed.source_ids

        result = approve_request(
            nexus_pg, request_id=request_id, company_id=_COMPANY_ID,
            approver_employee_id="EMP-ADMIN-TEST", approved_actions=["READ"],
            approved_duration_hours=24)
        grant_id = result["grant"]["grant_id"]
        assert result["request"]["status"] == "APPROVED"
        assert result["grant"]["employee_id"] == employee_id
        assert result["grant"]["status"] == "ACTIVE"

        refreshed = resolve(_demo_token(nexus_pg, employee_id), nexus_pg)
        assert resource_id in refreshed.source_ids
        if unrelated_resource:
            assert unrelated_resource not in refreshed.source_ids
    finally:
        _cleanup_grant(nexus_pg, grant_id)
        _cleanup_request(nexus_pg, request_id)


def _demo_token(nexus_pg, employee_id: str) -> str:
    rows = nexus_pg.read("SELECT demo_token FROM nexus_demo_identities WHERE employee_id=%s", (employee_id,))
    if not rows:
        pytest.skip(f"{employee_id} has no demo identity to resolve through")
    return rows[0]["demo_token"]


def test_denial_creates_no_grant_and_cannot_be_reapproved(nexus_pg, developer_principal):
    employee_id = developer_principal.subject.removeprefix("nexus:")
    resource_id = _restricted_resource_for_developer(nexus_pg, developer_principal)

    request_id = None
    try:
        request = create_access_request(
            nexus_pg, requester_employee_id=employee_id, company_id=_COMPANY_ID,
            resource_id=resource_id, requested_actions=["READ"], justification="test")
        request_id = request["request_id"]

        deny_request(nexus_pg, request_id=request_id, company_id=_COMPANY_ID,
                     approver_employee_id="EMP-ADMIN-TEST", reason="Not justified")

        grants = nexus_pg.read(
            "SELECT grant_id FROM nexus_resource_grants WHERE data->>'source_request_id'=%s", (request_id,))
        assert grants == []

        with pytest.raises(ValueError):
            approve_request(nexus_pg, request_id=request_id, company_id=_COMPANY_ID,
                            approver_employee_id="EMP-ADMIN-TEST")
    finally:
        _cleanup_request(nexus_pg, request_id)


def test_revocation_removes_effective_access(nexus_pg, developer_principal):
    employee_id = developer_principal.subject.removeprefix("nexus:")
    resource_id = _restricted_resource_for_developer(nexus_pg, developer_principal)

    request_id = None
    grant_id = None
    try:
        request = create_access_request(
            nexus_pg, requester_employee_id=employee_id, company_id=_COMPANY_ID,
            resource_id=resource_id, requested_actions=["READ"], justification="test")
        request_id = request["request_id"]
        result = approve_request(nexus_pg, request_id=request_id, company_id=_COMPANY_ID,
                                 approver_employee_id="EMP-ADMIN-TEST")
        grant_id = result["grant"]["grant_id"]

        refreshed = resolve(_demo_token(nexus_pg, employee_id), nexus_pg)
        assert resource_id in refreshed.source_ids

        revoke_grant(nexus_pg, grant_id=grant_id, company_id=_COMPANY_ID,
                     approver_employee_id="EMP-ADMIN-TEST", reason="done")

        refreshed = resolve(_demo_token(nexus_pg, employee_id), nexus_pg)
        assert resource_id not in refreshed.source_ids

        with pytest.raises(ValueError):
            revoke_grant(nexus_pg, grant_id=grant_id, company_id=_COMPANY_ID,
                         approver_employee_id="EMP-ADMIN-TEST")
    finally:
        _cleanup_grant(nexus_pg, grant_id)
        _cleanup_request(nexus_pg, request_id)


def test_privilege_and_scope_guards(nexus_pg, developer_principal):
    # Non-admin actors lack the permission that /approve, /deny, /revoke
    # endpoints require (require("ADMIN")); checked at the permission level
    # since this suite doesn't spin up a live HTTP client against FastAPI.
    assert Permission.ADMIN not in developer_principal.permissions

    employee_id = developer_principal.subject.removeprefix("nexus:")
    resource_id = _restricted_resource_for_developer(nexus_pg, developer_principal)

    request_id = None
    try:
        request = create_access_request(
            nexus_pg, requester_employee_id=employee_id, company_id=_COMPANY_ID,
            resource_id=resource_id, requested_actions=["READ"], justification="test",
            requested_duration_hours=12)
        request_id = request["request_id"]

        with pytest.raises(ValueError):
            approve_request(nexus_pg, request_id=request_id, company_id=_COMPANY_ID,
                            approver_employee_id="EMP-ADMIN-TEST",
                            approved_resource_id="NEX-SRC-COMPANY_FOUNDATION")

        with pytest.raises(ValueError):
            approve_request(nexus_pg, request_id=request_id, company_id=_COMPANY_ID,
                            approver_employee_id="EMP-ADMIN-TEST",
                            approved_actions=["READ", "WRITE"])

        with pytest.raises(ValueError):
            approve_request(nexus_pg, request_id=request_id, company_id=_COMPANY_ID,
                            approver_employee_id="EMP-ADMIN-TEST",
                            approved_duration_hours=100)
    finally:
        _cleanup_request(nexus_pg, request_id)


def test_duplicate_decision_is_rejected(nexus_pg, developer_principal):
    employee_id = developer_principal.subject.removeprefix("nexus:")
    resource_id = _restricted_resource_for_developer(nexus_pg, developer_principal)

    request_id = None
    grant_id = None
    try:
        request = create_access_request(
            nexus_pg, requester_employee_id=employee_id, company_id=_COMPANY_ID,
            resource_id=resource_id, requested_actions=["READ"], justification="test")
        request_id = request["request_id"]

        result = approve_request(nexus_pg, request_id=request_id, company_id=_COMPANY_ID,
                                 approver_employee_id="EMP-ADMIN-TEST")
        grant_id = result["grant"]["grant_id"]

        with pytest.raises(ValueError):
            approve_request(nexus_pg, request_id=request_id, company_id=_COMPANY_ID,
                            approver_employee_id="EMP-ADMIN-TEST-2")

        with pytest.raises(ValueError):
            deny_request(nexus_pg, request_id=request_id, company_id=_COMPANY_ID,
                         approver_employee_id="EMP-ADMIN-TEST-2")

        grants = nexus_pg.read(
            "SELECT grant_id FROM nexus_resource_grants WHERE data->>'source_request_id'=%s", (request_id,))
        assert len(grants) == 1
    finally:
        _cleanup_grant(nexus_pg, grant_id)
        _cleanup_request(nexus_pg, request_id)


def test_create_access_request_rejects_unknown_resource(nexus_pg, developer_principal):
    employee_id = developer_principal.subject.removeprefix("nexus:")
    with pytest.raises(ValueError):
        create_access_request(
            nexus_pg, requester_employee_id=employee_id, company_id=_COMPANY_ID,
            resource_id="NEX-SRC-DOES-NOT-EXIST", requested_actions=["READ"],
            justification="forged resource id")


def test_demo_token_still_fails_when_demo_mode_disabled(nexus_pg):
    # Regression guard only: full coverage already lives in
    # test_nexus_authorization.py::test_demo_token_fails_when_demo_mode_disabled.
    assert resolve("4", nexus_pg, demo_mode_enabled=False) is None


# --- Gap 1: action-level enforcement -----------------------------------------

def test_action_level_grant_restricts_to_approved_actions_only(nexus_pg, developer_principal):
    """A grant approved for READ only must not authorize any other action on
    that same resource, while READ itself stays permitted -- proving
    enforcement is exact frozenset membership, never a substring/prefix
    match (e.g. a READ approval must not leak into some other action that
    happens to share a prefix or be "close" to READ)."""
    employee_id = developer_principal.subject.removeprefix("nexus:")
    resource_id = _restricted_resource_for_developer(nexus_pg, developer_principal)

    request_id = None
    grant_id = None
    try:
        request = create_access_request(
            nexus_pg, requester_employee_id=employee_id, company_id=_COMPANY_ID,
            resource_id=resource_id, requested_actions=["READ", "WRITE", "DELETE"],
            justification="action-level enforcement test")
        request_id = request["request_id"]

        result = approve_request(
            nexus_pg, request_id=request_id, company_id=_COMPANY_ID,
            approver_employee_id="EMP-ADMIN-TEST", approved_actions=["READ"])
        grant_id = result["grant"]["grant_id"]
        assert result["grant"]["status"] == "ACTIVE"

        refreshed = resolve(_demo_token(nexus_pg, employee_id), nexus_pg)
        assert resource_id in refreshed.source_ids

        granted_actions = authorized_actions_for_source(employee_id, resource_id, nexus_pg)
        assert granted_actions == frozenset({"READ"})

        assert principal_authorizes_action(refreshed, resource_id, "READ", nexus_pg) is True
        assert principal_authorizes_action(refreshed, resource_id, "WRITE", nexus_pg) is False
        assert principal_authorizes_action(refreshed, resource_id, "DELETE", nexus_pg) is False
        # Exact-set membership, not substring/prefix matching: an unrecognized
        # action string is rejected outright rather than fuzzy-matched
        # against an approved one.
        assert principal_authorizes_action(refreshed, resource_id, "READ_ALL", nexus_pg) is False

        # Vocabulary is closed and validated: an unrecognized action string
        # anywhere in the lifecycle is rejected rather than silently stored.
        with pytest.raises(ValueError):
            create_access_request(
                nexus_pg, requester_employee_id=employee_id, company_id=_COMPANY_ID,
                resource_id=resource_id, requested_actions=["READ", "FLY_TO_THE_MOON"],
                justification="invalid vocabulary")
    finally:
        _cleanup_grant(nexus_pg, grant_id)
        _cleanup_request(nexus_pg, request_id)


# --- Gap 2: HTTP denial must cause zero state changes ------------------------

def test_http_non_admin_denied_approve_with_no_side_effects(nexus_pg, developer_principal, monkeypatch):
    """A non-admin NEXUS identity (e.g. a developer) hitting the ADMIN-only
    /approve endpoint must get HTTP 403, with zero new grant/decision rows
    created as a side effect, and a response body that leaks nothing about
    the (nonexistent, here) target request."""
    from fastapi.testclient import TestClient

    from backend.app.main import app

    monkeypatch.setenv("GRAPH_HACKS_NEXUS_DEMO_MODE", "true")
    assert isolated_demo_mode_enabled() is True

    employee_id = developer_principal.subject.removeprefix("nexus:")
    token = _demo_token(nexus_pg, employee_id)

    before = nexus_pg.read("SELECT count(*) AS n FROM nexus_resource_grants")[0]["n"]
    before_decisions = nexus_pg.read("SELECT count(*) AS n FROM nexus_access_decisions")[0]["n"]

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/nexus/access-requests/AR-does-not-exist/approve",
            json={}, headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 403
    body = response.text
    assert "AR-does-not-exist" not in body
    assert "ADMINISTRATIVE_RECORDS" not in body

    after = nexus_pg.read("SELECT count(*) AS n FROM nexus_resource_grants")[0]["n"]
    after_decisions = nexus_pg.read("SELECT count(*) AS n FROM nexus_access_decisions")[0]["n"]
    assert after == before
    assert after_decisions == before_decisions


# --- Gap 3: action-level permission freshness --------------------------------

def test_revoked_grant_drops_action_level_access(nexus_pg, developer_principal):
    """Extends the existing ``refresh_principal_source_ids`` mid-run
    freshness guarantee (see test_nexus_authorization.py) to the new
    action-level function: a revoked grant's actions must disappear
    immediately, not just its source-level membership."""
    employee_id = developer_principal.subject.removeprefix("nexus:")
    resource_id = _restricted_resource_for_developer(nexus_pg, developer_principal)

    request_id = None
    grant_id = None
    try:
        request = create_access_request(
            nexus_pg, requester_employee_id=employee_id, company_id=_COMPANY_ID,
            resource_id=resource_id, requested_actions=["READ"], justification="freshness test")
        request_id = request["request_id"]
        result = approve_request(nexus_pg, request_id=request_id, company_id=_COMPANY_ID,
                                 approver_employee_id="EMP-ADMIN-TEST")
        grant_id = result["grant"]["grant_id"]

        refreshed = resolve(_demo_token(nexus_pg, employee_id), nexus_pg)
        assert principal_authorizes_action(refreshed, resource_id, "READ", nexus_pg) is True
        assert authorized_actions_for_source(employee_id, resource_id, nexus_pg) == frozenset({"READ"})

        revoke_grant(nexus_pg, grant_id=grant_id, company_id=_COMPANY_ID,
                     approver_employee_id="EMP-ADMIN-TEST", reason="freshness test revoke")

        assert authorized_actions_for_source(employee_id, resource_id, nexus_pg) == frozenset()
        refreshed_after = resolve(_demo_token(nexus_pg, employee_id), nexus_pg)
        assert resource_id not in refreshed_after.source_ids
        assert principal_authorizes_action(refreshed_after, resource_id, "READ", nexus_pg) is False
    finally:
        _cleanup_grant(nexus_pg, grant_id)
        _cleanup_request(nexus_pg, request_id)
