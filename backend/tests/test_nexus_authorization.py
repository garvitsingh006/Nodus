"""Tool/API-level authorization boundaries using real NEXUS demo identities
(ADMIN/DEVELOPER/INTERN), against the live, already-imported dataset. See
``backend.tests.nexus_live`` for why this suite uses the shared live database
instead of an isolated one.

Any test that mutates live Postgres grant state restores it in a
``try/finally`` so the suite never leaves the shared demo dataset corrupted,
even on assertion failure.
"""
from __future__ import annotations

import inspect

import pytest

from backend.app.governance.identity import (
    isolated_demo_mode_enabled, refresh_principal_source_ids, resolve_demo_principal,
)
from backend.tests.nexus_live import (
    NEXUS_WORKSPACE_ID, admin_principal, developer_principal, intern_principal,
    nexus_neo4j, nexus_pg, nexus_settings, repository_for, resolve,
)

pytestmark = pytest.mark.integration


# --- Role-scoped source visibility -----------------------------------------

def test_admin_sees_all_sources_including_administrative(nexus_pg, nexus_neo4j, admin_principal):
    assert len(admin_principal.source_ids) == 29
    repo = repository_for(nexus_neo4j, admin_principal)
    # ADMIN can read an administrative-classification source's evidence.
    rows = nexus_pg.read(
        "SELECT record_id FROM nexus_records WHERE source_id='NEX-SRC-SECURITY_GOVERNANCE' LIMIT 1")
    assert rows, "fixture assumption broken: no NEX-SRC-SECURITY_GOVERNANCE records imported"
    record = repo.source_record("NEX-SRC-SECURITY_GOVERNANCE", rows[0]["record_id"])
    assert record is not None


def test_developer_sees_exactly_their_granted_sources(developer_principal):
    assert len(developer_principal.source_ids) == 9
    assert "NEX-SRC-SECURITY_GOVERNANCE" not in developer_principal.source_ids
    assert "NEX-SRC-COMPANY_FOUNDATION" in developer_principal.source_ids


def test_developer_denied_administrative_source_read(nexus_pg, nexus_neo4j, developer_principal):
    repo = repository_for(nexus_neo4j, developer_principal)
    rows = nexus_pg.read(
        "SELECT record_id FROM nexus_records WHERE source_id='NEX-SRC-SECURITY_GOVERNANCE' LIMIT 1")
    assert rows
    # Denial is the existing empty-result convention, never an exception or a
    # distinguishable error — consistent with acl_subjects-based denial.
    assert repo.source_record("NEX-SRC-SECURITY_GOVERNANCE", rows[0]["record_id"]) is None


def test_intern_sees_only_shared_and_foundation_sources(nexus_pg, intern_principal):
    assert len(intern_principal.source_ids) == 5
    classifications = {
        row["source_id"]: row["classification"]
        for row in nexus_pg.read("SELECT source_id, classification FROM nexus_sources")
    }
    for source_id in intern_principal.source_ids:
        assert classifications[source_id] in ("COMPANY_SHARED", "FOUNDATIONAL")


def test_intern_denied_project_developer_source(nexus_pg, nexus_neo4j, intern_principal):
    repo = repository_for(nexus_neo4j, intern_principal)
    rows = nexus_pg.read(
        "SELECT record_id FROM nexus_records WHERE source_id='NEX-SRC-PAY-REPO' LIMIT 1")
    assert rows
    assert repo.source_record("NEX-SRC-PAY-REPO", rows[0]["record_id"]) is None


# --- Demo-mode gating --------------------------------------------------------

def test_demo_token_fails_when_demo_mode_disabled(nexus_pg):
    assert resolve(" 4".strip(), nexus_pg, demo_mode_enabled=False) is None


def test_isolated_demo_mode_enabled_reads_env(monkeypatch):
    monkeypatch.delenv("GRAPH_HACKS_NEXUS_DEMO_MODE", raising=False)
    assert isolated_demo_mode_enabled() is False
    monkeypatch.setenv("GRAPH_HACKS_NEXUS_DEMO_MODE", "true")
    assert isolated_demo_mode_enabled() is True
    monkeypatch.setenv("GRAPH_HACKS_NEXUS_DEMO_MODE", "0")
    assert isolated_demo_mode_enabled() is False


# --- Expired/revoked grants ---------------------------------------------------

def test_expired_grant_excluded_from_effective_access(nexus_pg):
    expired = nexus_pg.read("""
        SELECT grant_id, employee_id, resource_id FROM nexus_resource_grants
        WHERE status='EXPIRED' AND expires_at < now() LIMIT 1
        """)
    if not expired:
        pytest.skip("No expired grant present in the imported dataset")
    row = expired[0]
    identity_rows = nexus_pg.read(
        "SELECT demo_token FROM nexus_demo_identities WHERE employee_id=%s", (row["employee_id"],))
    if not identity_rows:
        pytest.skip(f"{row['employee_id']} has no demo identity to resolve through")
    principal = resolve(identity_rows[0]["demo_token"], nexus_pg)
    assert principal is not None
    assert row["resource_id"] not in principal.source_ids


def test_revoked_grant_excluded_from_effective_access(nexus_pg):
    revoked = nexus_pg.read("""
        SELECT grant_id, employee_id, resource_id FROM nexus_resource_grants
        WHERE revoked_at IS NOT NULL LIMIT 1
        """)
    if not revoked:
        pytest.skip("No revoked grant present in the imported dataset")
    row = revoked[0]
    identity_rows = nexus_pg.read(
        "SELECT demo_token FROM nexus_demo_identities WHERE employee_id=%s", (row["employee_id"],))
    if not identity_rows:
        pytest.skip(f"{row['employee_id']} has no demo identity to resolve through")
    principal = resolve(identity_rows[0]["demo_token"], nexus_pg)
    assert principal is not None
    assert row["resource_id"] not in principal.source_ids


# --- Mid-run authorization freshness -----------------------------------------

@pytest.fixture
def temporarily_revoked_active_grant(nexus_pg):
    """Yields a real ACTIVE grant's id after revoking it, then restores it to
    its exact prior state in a ``finally`` so the shared demo dataset is never
    left corrupted, even if the test body raises."""
    active = nexus_pg.read("""
        SELECT grant_id, employee_id, resource_id, status, revoked_at, expires_at, valid_from
        FROM nexus_resource_grants
        WHERE status='ACTIVE' AND revoked_at IS NULL
          AND (expires_at IS NULL OR expires_at > now())
        ORDER BY grant_id LIMIT 1
        """)
    if not active:
        pytest.skip("No active grant available to exercise mid-run revocation")
    grant = active[0]
    try:
        with nexus_pg.transaction() as cur:
            cur.execute(
                "UPDATE nexus_resource_grants SET status='REVOKED', revoked_at=now() WHERE grant_id=%s",
                (grant["grant_id"],))
        yield grant
    finally:
        with nexus_pg.transaction() as cur:
            cur.execute("""
                UPDATE nexus_resource_grants
                SET status=%s, revoked_at=%s WHERE grant_id=%s
                """, (grant["status"], grant["revoked_at"], grant["grant_id"]))


def test_mid_run_refresh_drops_access_after_revocation(nexus_pg, temporarily_revoked_active_grant):
    grant = temporarily_revoked_active_grant
    identity_rows = nexus_pg.read(
        "SELECT demo_token FROM nexus_demo_identities WHERE employee_id=%s", (grant["employee_id"],))
    if not identity_rows:
        pytest.skip(f"{grant['employee_id']} has no demo identity to resolve through")
    token = identity_rows[0]["demo_token"]

    # Resolve the principal BEFORE revocation is visible to a fresh resolve
    # (simulates the context snapshot an investigation run starts with) is
    # not meaningful here since resolve_demo_principal always reads current
    # state; the real regression is `refresh_principal_source_ids` being
    # skipped entirely. So: build a principal from a *stale* source set (as
    # if captured at run start before the revoke) and confirm the refresh
    # helper used by backend/app/agent/tool_bridge.py::refresh_authorization
    # drops the now-revoked resource on every subsequent call.
    stale_principal = resolve(token, nexus_pg)
    # The revoke above already happened; a correctly-implemented resolve
    # reflects it immediately (this is the direct-resolution path, already
    # covered by test_revoked_grant_excluded_from_effective_access). The
    # mid-run-specific behavior is that `refresh_principal_source_ids` must
    # independently reach the same answer when called on an *old* Principal
    # object constructed before the revoke, proving the refresh re-queries
    # live state rather than trusting whatever it's handed.
    from backend.app.security import Principal
    pretend_stale = Principal(
        subject=stale_principal.subject, tenant_id=stale_principal.tenant_id,
        workspaces=stale_principal.workspaces, permissions=stale_principal.permissions,
        source_ids=tuple(set(stale_principal.source_ids) | {grant["resource_id"]}),
    )
    assert grant["resource_id"] in pretend_stale.source_ids  # the stale snapshot still "has" it

    refreshed = refresh_principal_source_ids(pretend_stale, nexus_pg)
    assert grant["resource_id"] not in refreshed.source_ids


def test_refresh_is_noop_for_admin_and_non_nexus_principals(nexus_pg, admin_principal):
    assert refresh_principal_source_ids(admin_principal, nexus_pg) == admin_principal

    from backend.app.security import Principal
    other_tenant = Principal(subject="dev", tenant_id="other-tenant", workspaces=("ws",),
                             source_ids=("local",))
    assert refresh_principal_source_ids(other_tenant, nexus_pg) == other_tenant


# --- Temporal security --------------------------------------------------------

def test_authorization_resolution_takes_no_caller_supplied_timestamp():
    """No function in the identity layer that decides effective access may
    accept a caller-supplied 'as of' time — current wall-clock time only, so
    a currently-unauthorized identity cannot bypass authorization by asking
    about an earlier date."""
    for fn in (resolve_demo_principal, refresh_principal_source_ids):
        params = inspect.signature(fn).parameters
        assert not any("date" in name or "as_of" in name or "when" in name for name in params), (
            f"{fn.__name__} must not accept a caller-supplied time parameter")
