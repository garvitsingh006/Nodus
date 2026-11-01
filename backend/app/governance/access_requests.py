"""NODUS Stage 09B: access-request lifecycle (request -> approve/deny ->
grant -> revoke) over the governance ledger tables defined in
``backend.app.governance.store`` (``nexus_access_requests``,
``nexus_access_decisions``, ``nexus_resource_grants``).

This module owns only the data-integrity invariants of that lifecycle
(PENDING-only transitions, no-widening approvals, atomic decision+grant
writes). Who is ALLOWED to call ``approve_request``/``deny_request``/
``revoke_grant`` at all is an API-layer concern (see ``backend.app.main``),
not duplicated here.

A grant created by ``approve_request`` lands in the exact same
``nexus_resource_grants`` columns that
``backend.app.governance.identity._active_granted_resource_ids`` already
reads, so it becomes effective immediately (including mid-agent-run, via
``refresh_principal_source_ids``) with zero changes to that module.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from backend.app.governance.identity import ACTION_VOCABULARY, NEXUS_TENANT_ID
from backend.app.governance.store import GovernanceStore

_json = json.dumps


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize_actions(actions: list[str], *, field_name: str) -> list[str]:
    """Validates ``actions`` against the closed ``ACTION_VOCABULARY`` and
    returns a de-duplicated, sorted list. Rejects (``ValueError``) any string
    outside the vocabulary -- including anything that merely looks similar to
    a valid action -- since actions are always compared as exact frozenset
    members, never by substring/prefix matching."""
    normalized: set[str] = set()
    for action in actions:
        if not isinstance(action, str) or action not in ACTION_VOCABULARY:
            raise ValueError(f"Unrecognized {field_name} entry: {action!r}; must be one of {sorted(ACTION_VOCABULARY)}")
        normalized.add(action)
    return sorted(normalized)


def _request_to_dict(row: dict) -> dict:
    data = dict(row.get("data") or {})
    return {
        "request_id": row["request_id"],
        "employee_id": row["employee_id"],
        "resource_id": row["resource_id"],
        "status": row["status"],
        **data,
    }


def _decision_to_dict(row: dict) -> dict:
    data = dict(row.get("data") or {})
    return {
        "decision_id": row["decision_id"],
        "request_id": row["request_id"],
        "grant_id": row["grant_id"],
        "decision_actor_employee_id": row["decision_actor_employee_id"],
        **data,
    }


def _grant_to_dict(row: dict) -> dict:
    data = dict(row.get("data") or {})
    return {
        "grant_id": row["grant_id"],
        "employee_id": row["employee_id"],
        "resource_id": row["resource_id"],
        "scope_project_id": row["scope_project_id"],
        "permission_level": row["permission_level"],
        "status": row["status"],
        "valid_from": row["valid_from"],
        "expires_at": row["expires_at"],
        "revoked_at": row["revoked_at"],
        "administrative_decision_id": row["administrative_decision_id"],
        **data,
    }


def create_access_request(
    store: GovernanceStore, *, requester_employee_id: str, company_id: str, resource_id: str,
    requested_actions: list[str], justification: str,
    requested_project_id: str | None = None, requested_duration_hours: float | None = None,
) -> dict:
    """Validates ``resource_id`` against the live source registry and inserts
    a new PENDING request. Never creates a grant."""
    rows = store.read(
        "SELECT source_id FROM nexus_sources WHERE company_id=%s AND source_id=%s",
        (company_id, resource_id))
    if not rows:
        raise ValueError(f"Unknown or out-of-scope resource_id: {resource_id!r}")

    normalized_actions = _normalize_actions(requested_actions, field_name="requested_actions")

    request_id = f"AR-{uuid4().hex}"
    data = {
        "requested_actions": normalized_actions,
        "justification": justification,
        "requested_project_id": requested_project_id,
        "requested_duration_hours": requested_duration_hours,
        "created_at": _now_iso(),
        "requester_employee_id": requester_employee_id,
    }
    with store.transaction() as cur:
        cur.execute("""
            INSERT INTO nexus_access_requests
                (tenant_id, company_id, request_id, employee_id, resource_id, status, data)
            VALUES (%s, %s, %s, %s, %s, 'PENDING', %s::jsonb)
            RETURNING *
            """, (NEXUS_TENANT_ID, company_id, request_id, requester_employee_id,
                  resource_id, _json(data)))
        row = cur.fetchone()
    return _request_to_dict(row)


def list_requests_for_employee(store: GovernanceStore, *, employee_id: str, company_id: str) -> list[dict]:
    rows = store.read("""
        SELECT * FROM nexus_access_requests WHERE company_id=%s AND employee_id=%s
        ORDER BY (data->>'created_at') DESC NULLS LAST
        """, (company_id, employee_id))
    return [_request_to_dict(row) for row in rows]


def list_pending_requests(store: GovernanceStore, *, company_id: str) -> list[dict]:
    rows = store.read("""
        SELECT * FROM nexus_access_requests WHERE company_id=%s AND status='PENDING'
        ORDER BY (data->>'created_at') ASC NULLS LAST
        """, (company_id,))
    return [_request_to_dict(row) for row in rows]


def _compute_expires_at(duration_hours: float | None) -> datetime | None:
    if duration_hours is None:
        return None
    return datetime.now(timezone.utc) + timedelta(hours=duration_hours)


def approve_request(
    store: GovernanceStore, *, request_id: str, company_id: str, approver_employee_id: str,
    approved_resource_id: str | None = None, approved_actions: list[str] | None = None,
    approved_project_id: str | None = None, approved_duration_hours: float | None = None,
) -> dict:
    with store.transaction() as cur:
        cur.execute("""
            SELECT * FROM nexus_access_requests
            WHERE company_id=%s AND request_id=%s
            FOR UPDATE
            """, (company_id, request_id))
        request_row = cur.fetchone()
        if request_row is None:
            raise ValueError(f"Unknown request_id: {request_id!r}")
        if request_row["status"] != "PENDING":
            raise ValueError(f"Request {request_id!r} is not PENDING (status={request_row['status']!r})")

        request_data = dict(request_row.get("data") or {})
        requested_resource_id = request_row["resource_id"]
        requested_actions: list[str] = list(request_data.get("requested_actions") or [])
        requested_project_id = request_data.get("requested_project_id")
        requested_duration_hours = request_data.get("requested_duration_hours")

        final_resource_id = approved_resource_id if approved_resource_id is not None else requested_resource_id
        if final_resource_id != requested_resource_id:
            raise ValueError(
                "approved_resource_id must match the requested resource_id (no widening to a "
                "different resource)")

        if approved_actions is not None:
            normalized_approved = _normalize_actions(approved_actions, field_name="approved_actions")
            if not set(normalized_approved).issubset(set(requested_actions)):
                raise ValueError("approved_actions must be a subset of the requested_actions")
            final_actions = normalized_approved
        else:
            final_actions = requested_actions

        final_project_id = approved_project_id if approved_project_id is not None else requested_project_id

        if approved_duration_hours is not None:
            if requested_duration_hours is not None and approved_duration_hours > requested_duration_hours:
                raise ValueError("approved_duration_hours must not exceed the requested duration")
            final_duration_hours = approved_duration_hours
        else:
            final_duration_hours = requested_duration_hours

        requester_employee_id = request_row["employee_id"]
        decision_id = f"AD-{uuid4().hex}"
        grant_id = f"GR-{uuid4().hex}"
        expires_at = _compute_expires_at(final_duration_hours)
        now = datetime.now(timezone.utc)

        grant_data = {
            "approved_actions": final_actions,
            "requested_duration_hours": requested_duration_hours,
            "approved_duration_hours": final_duration_hours,
            "source_request_id": request_id,
        }
        cur.execute("""
            INSERT INTO nexus_resource_grants
                (tenant_id, company_id, grant_id, employee_id, resource_id, scope_project_id,
                 permission_level, status, valid_from, expires_at, revoked_at,
                 administrative_decision_id, data)
            VALUES (%s, %s, %s, %s, %s, %s, %s, 'ACTIVE', %s, %s, NULL, %s, %s::jsonb)
            RETURNING *
            """, (NEXUS_TENANT_ID, company_id, grant_id, requester_employee_id,
                  final_resource_id, final_project_id, ",".join(final_actions) or None, now,
                  expires_at, decision_id, _json(grant_data)))
        grant_row = cur.fetchone()

        decision_data = {
            "decision": "APPROVED",
            "decided_at": _now_iso(),
            "approved_resource_id": final_resource_id,
            "approved_actions": final_actions,
            "approved_project_id": final_project_id,
            "approved_duration_hours": final_duration_hours,
        }
        cur.execute("""
            INSERT INTO nexus_access_decisions
                (tenant_id, company_id, decision_id, request_id, grant_id, decision_actor_employee_id, data)
            VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb)
            RETURNING *
            """, (NEXUS_TENANT_ID, company_id, decision_id, request_id, grant_id,
                  approver_employee_id, _json(decision_data)))
        decision_row = cur.fetchone()

        cur.execute("""
            UPDATE nexus_access_requests SET status='APPROVED' WHERE company_id=%s AND request_id=%s
            RETURNING *
            """, (company_id, request_id))
        updated_request_row = cur.fetchone()

    return {
        "request": _request_to_dict(updated_request_row),
        "decision": _decision_to_dict(decision_row),
        "grant": _grant_to_dict(grant_row),
    }


def deny_request(
    store: GovernanceStore, *, request_id: str, company_id: str, approver_employee_id: str,
    reason: str | None = None,
) -> dict:
    with store.transaction() as cur:
        cur.execute("""
            SELECT * FROM nexus_access_requests
            WHERE company_id=%s AND request_id=%s
            FOR UPDATE
            """, (company_id, request_id))
        request_row = cur.fetchone()
        if request_row is None:
            raise ValueError(f"Unknown request_id: {request_id!r}")
        if request_row["status"] != "PENDING":
            raise ValueError(f"Request {request_id!r} is not PENDING (status={request_row['status']!r})")

        decision_id = f"AD-{uuid4().hex}"
        decision_data = {"decision": "DENIED", "decided_at": _now_iso(), "reason": reason}
        cur.execute("""
            INSERT INTO nexus_access_decisions
                (tenant_id, company_id, decision_id, request_id, grant_id, decision_actor_employee_id, data)
            VALUES (%s, %s, %s, %s, NULL, %s, %s::jsonb)
            RETURNING *
            """, (NEXUS_TENANT_ID, company_id, decision_id, request_id,
                  approver_employee_id, _json(decision_data)))
        decision_row = cur.fetchone()

        cur.execute("""
            UPDATE nexus_access_requests SET status='DENIED' WHERE company_id=%s AND request_id=%s
            RETURNING *
            """, (company_id, request_id))
        updated_request_row = cur.fetchone()

    return {"request": _request_to_dict(updated_request_row), "decision": _decision_to_dict(decision_row)}


def revoke_grant(
    store: GovernanceStore, *, grant_id: str, company_id: str, approver_employee_id: str,
    reason: str | None = None,
) -> dict:
    with store.transaction() as cur:
        cur.execute("""
            SELECT * FROM nexus_resource_grants
            WHERE company_id=%s AND grant_id=%s
            FOR UPDATE
            """, (company_id, grant_id))
        grant_row = cur.fetchone()
        if grant_row is None:
            raise ValueError(f"Unknown grant_id: {grant_id!r}")
        if grant_row["status"] != "ACTIVE":
            raise ValueError(f"Grant {grant_id!r} is not ACTIVE (status={grant_row['status']!r}); "
                             "cannot revoke a grant that is not currently active")

        cur.execute("""
            UPDATE nexus_resource_grants SET status='REVOKED', revoked_at=now()
            WHERE company_id=%s AND grant_id=%s
            RETURNING *
            """, (company_id, grant_id))
        updated_grant_row = cur.fetchone()

        decision_id = f"AD-{uuid4().hex}"
        request_id = None
        source_request_id = (grant_row.get("data") or {}).get("source_request_id")
        if source_request_id:
            request_id = source_request_id
        decision_data = {"decision": "REVOKED", "decided_at": _now_iso(), "reason": reason}
        cur.execute("""
            INSERT INTO nexus_access_decisions
                (tenant_id, company_id, decision_id, request_id, grant_id, decision_actor_employee_id, data)
            VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb)
            RETURNING *
            """, (NEXUS_TENANT_ID, company_id, decision_id, request_id, grant_id,
                  approver_employee_id, _json(decision_data)))
        decision_row = cur.fetchone()

    return {"grant": _grant_to_dict(updated_grant_row), "decision": _decision_to_dict(decision_row)}


def get_authorization_scope(store: GovernanceStore, *, employee_id: str, company_id: str) -> dict:
    """Convenience read-only composite: role, project memberships, effective
    source access, and the employee's active grants. Delegates role/source
    resolution to ``backend.app.governance.identity`` rather than
    duplicating its SQL."""
    from backend.app.governance.identity import (
        _assigned_project_ids, _effective_source_ids, _role_code,
    )

    identity_rows = store.read(
        "SELECT role_id FROM nexus_demo_identities WHERE employee_id=%s", (employee_id,))
    role_code = _role_code(identity_rows[0]["role_id"], store) if identity_rows else None
    project_ids = sorted(_assigned_project_ids(employee_id, store))
    source_ids = list(_effective_source_ids(role_code, employee_id, store)) if role_code else []

    grants = store.read("""
        SELECT * FROM nexus_resource_grants
        WHERE company_id=%s AND employee_id=%s AND status='ACTIVE' AND revoked_at IS NULL
        ORDER BY grant_id
        """, (company_id, employee_id))

    return {
        "employee_id": employee_id,
        "company_id": company_id,
        "role_code": role_code,
        "project_ids": project_ids,
        "source_ids": source_ids,
        "active_grants": [_grant_to_dict(row) for row in grants],
    }
