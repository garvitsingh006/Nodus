"""NODUS Stage-09-equivalent identity/authorization resolution: resolves a
NEXUS demo token to a ``Principal`` (the existing, unmodified security
contract — see ``backend.app.security``) by computing the employee's role and
CURRENT effective source access from PostgreSQL-authoritative governance
state (role baseline entitlements + project assignment + active resource
grants). Never trusts a client-supplied timestamp for this decision — "now"
is always the server wall clock, so a currently-unauthorized identity cannot
gain access by asking about an earlier date (temporal security requirement).
"""
from __future__ import annotations

import os
from datetime import datetime, timezone

from backend.app.governance.demo_tokens import canonical_demo_token
from backend.app.governance.store import GovernanceStore
from backend.app.security import AuthorizationContext, Permission, Principal

# A NEXUS demo identity only ever resolves within this one fixed tenant (see
# scripts/import_nexus.py, which projects the dataset into the same tenant).
# Shared by backend.app.main and every backend.app.tools/* module that builds
# its own GraphRepository, so every read path — HTTP API and agent-tool
# execution alike — applies the same source-scoping decision.
NEXUS_TENANT_ID = "nexus-tenant"
NEXUS_WORKSPACE_ID = "nexus-workspace"

ROLE_PERMISSIONS: dict[str, frozenset[Permission]] = {
    "ADMIN": frozenset({Permission.ADMIN}),
    "DEVELOPER": frozenset({Permission.READ_GRAPH, Permission.READ_EVIDENCE, Permission.EXECUTE_TOOL}),
    "INTERN": frozenset({Permission.READ_GRAPH, Permission.READ_EVIDENCE}),
}

# Stage 09B action-level vocabulary (NODUS gap-fix): the exact, closed set of
# action names ``create_access_request``/``approve_request`` accept in
# ``requested_actions``/``approved_actions`` and that
# ``authorized_actions_for``/``principal_authorizes_action`` compare against.
# Always compared as *members of this frozenset*, never by substring/prefix
# matching, so e.g. an approved "READ" never accidentally authorizes a
# hypothetical "READ_ALL". Any string outside this set is rejected
# (``ValueError``) by ``backend.app.governance.access_requests``'s
# normalization helper rather than silently accepted or ignored.
ACTION_VOCABULARY: frozenset[str] = frozenset({"READ", "WRITE", "EXECUTE", "DELETE", "EXPORT", "ADMIN"})

# The action set granted by BASELINE/role-derived source access (COMPANY_SHARED/
# FOUNDATIONAL classification, or role-based PROJECT_DEVELOPER/
# CROSS_PROJECT_CONFIDENTIAL/ADMINISTRATIVE_RECORDS eligibility computed in
# ``_effective_source_ids``) -- i.e. access NOT backed by an explicit,
# actions-scoped Stage 09B grant. This NEXUS deployment currently exposes no
# non-read operation anywhere (every HTTP endpoint and every agent tool is
# read-only -- see ``backend/app/agent/tool_bridge.py``'s READ_ONLY-only tool
# exposure), so baseline access is deliberately capped at READ until an
# explicit, actions-scoped grant widens it for a given resource.
BASELINE_ACTIONS: frozenset[str] = frozenset({"READ"})


def isolated_demo_mode_enabled() -> bool:
    return os.getenv("GRAPH_HACKS_NEXUS_DEMO_MODE", "false").strip().lower() in {"1", "true", "yes", "on"}


def _demo_identity_row(token: str, store: GovernanceStore) -> dict | None:
    rows = store.read(
        "SELECT demo_identity_id, employee_id, role_id FROM nexus_demo_identities WHERE demo_token=%s",
        (canonical_demo_token(token),))
    return rows[0] if rows else None


def _role_code(role_id: str, store: GovernanceStore) -> str | None:
    rows = store.read("SELECT role_code FROM nexus_roles WHERE role_id=%s", (role_id,))
    return rows[0]["role_code"] if rows else None


def _baseline_allowed_sources(role_id: str, store: GovernanceStore) -> list[dict]:
    """Sources whose classification matches a role's ALLOW baseline entitlement."""
    return store.read("""
        SELECT s.source_id, s.classification FROM nexus_sources s
        JOIN nexus_baseline_entitlements e
          ON e.resource_class = s.classification AND e.role_id = %s AND e.baseline_decision = 'ALLOW'
        """, (role_id,))


def _assigned_project_ids(employee_id: str, store: GovernanceStore) -> set[str]:
    rows = store.read("SELECT project_id FROM nexus_project_assignments WHERE employee_id=%s", (employee_id,))
    return {row["project_id"] for row in rows}


def _active_granted_resource_ids(employee_id: str, store: GovernanceStore, *, now: datetime) -> set[str]:
    rows = store.read("""
        SELECT resource_id FROM nexus_resource_grants
        WHERE employee_id=%s AND status='ACTIVE' AND revoked_at IS NULL
          AND (valid_from IS NULL OR valid_from <= %s)
          AND (expires_at IS NULL OR expires_at > %s)
        """, (employee_id, now, now))
    return {row["resource_id"] for row in rows}


def authorized_actions_for_source(employee_id: str, resource_id: str, store: GovernanceStore, *,
                                  now: datetime | None = None) -> frozenset[str]:
    """The action set CURRENTLY granted to ``employee_id`` on ``resource_id`` by
    an explicit Stage 09B access-request grant, using the exact same
    freshness rules as ``_active_granted_resource_ids`` (status='ACTIVE',
    revoked_at IS NULL, valid_from/expires_at window evaluated against the
    server wall clock) -- so an expired or revoked grant yields an empty
    action set automatically, with zero extra plumbing in
    ``refresh_principal_source_ids``/``tool_bridge.refresh_authorization``'s
    existing mid-run refresh.

    Reads the authoritative ``approved_actions`` list from the grant's
    ``data`` JSONB (written by
    ``backend.app.governance.access_requests.approve_request``) rather than
    splitting any comma-joined string, and discards any value outside
    ``ACTION_VOCABULARY`` defensively (grant rows are only ever written by
    ``approve_request``, which already validates this, but a direct SQL
    write or a future migration should never be able to smuggle in an
    unrecognized action here).
    """
    if now is None:
        now = datetime.now(timezone.utc)
    rows = store.read("""
        SELECT data FROM nexus_resource_grants
        WHERE employee_id=%s AND resource_id=%s AND status='ACTIVE' AND revoked_at IS NULL
          AND (valid_from IS NULL OR valid_from <= %s)
          AND (expires_at IS NULL OR expires_at > %s)
        """, (employee_id, resource_id, now, now))
    actions: set[str] = set()
    for row in rows:
        data = row.get("data") or {}
        actions.update(a for a in (data.get("approved_actions") or []) if a in ACTION_VOCABULARY)
    return frozenset(actions)


def authorized_actions_for(principal: Principal, resource_id: str, store: GovernanceStore) -> frozenset[str]:
    """The shared action-level authorization boundary used by BOTH the HTTP
    API (``backend.app.main``) and the agent-tool execution path -- do not
    fork a second implementation of this decision.

    Source-level authorization remains the first gate, unchanged:
    ``principal.permits_source(resource_id)`` must already be true, or this
    returns the empty set outright (no source access -> no actions,
    regardless of anything else).

    Within that:
      * ADMIN holds every action in ``ACTION_VOCABULARY`` unconditionally.
      * A NEXUS principal whose access to ``resource_id`` traces to an
        explicit, currently-ACTIVE Stage 09B grant (``_active_granted_resource_ids``)
        gets exactly that grant's ``approved_actions``
        (``authorized_actions_for_source``) -- e.g. a READ-only approval
        cannot be used for WRITE/EXPORT/etc, closing the gap where
        ``approved_actions`` was recorded but never enforced.
      * Any other access -- BASELINE/role-derived (COMPANY_SHARED/
        FOUNDATIONAL, or role-based PROJECT_DEVELOPER/
        CROSS_PROJECT_CONFIDENTIAL/ADMINISTRATIVE_RECORDS eligibility with no
        matching explicit grant, or a non-NEXUS principal) -- defaults to
        ``BASELINE_ACTIONS`` (READ-only; see that constant's docstring for
        why).
    """
    if Permission.ADMIN in principal.permissions:
        return ACTION_VOCABULARY
    if not principal.permits_source(resource_id):
        return frozenset()
    if principal.tenant_id != NEXUS_TENANT_ID or not principal.subject.startswith("nexus:"):
        return BASELINE_ACTIONS
    employee_id = principal.subject.removeprefix("nexus:")
    now = datetime.now(timezone.utc)
    if resource_id in _active_granted_resource_ids(employee_id, store, now=now):
        explicit = authorized_actions_for_source(employee_id, resource_id, store, now=now)
        if explicit:
            return explicit
    return BASELINE_ACTIONS


def principal_authorizes_action(principal: Principal, resource_id: str, action: str, store: GovernanceStore) -> bool:
    """True iff ``principal`` currently holds ``action`` on ``resource_id``.
    Unrecognized ``action`` strings are rejected (never authorized) by
    default, consistent with ``ACTION_VOCABULARY`` being a closed set."""
    if action not in ACTION_VOCABULARY:
        return False
    return action in authorized_actions_for(principal, resource_id, store)


def _effective_source_ids(role_code: str, employee_id: str, store: GovernanceStore) -> tuple[str, ...]:
    now = datetime.now(timezone.utc)
    all_sources = store.read("SELECT source_id, classification, owner_project_id FROM nexus_sources")
    if role_code == "ADMIN":
        return tuple(sorted(row["source_id"] for row in all_sources))

    effective: set[str] = set()
    for row in all_sources:
        if row["classification"] in ("COMPANY_SHARED", "FOUNDATIONAL"):
            effective.add(row["source_id"])

    granted = _active_granted_resource_ids(employee_id, store, now=now)
    assigned_projects = _assigned_project_ids(employee_id, store)
    for row in all_sources:
        source_id, classification, owner_project_id = row["source_id"], row["classification"], row["owner_project_id"]
        if source_id not in granted:
            continue
        if classification == "PROJECT_DEVELOPER":
            # Project membership alone is eligibility, never access: an
            # active grant is required in addition to assignment, and
            # interns never receive implicit project-source access.
            if role_code == "DEVELOPER" and owner_project_id in assigned_projects:
                effective.add(source_id)
        elif classification == "CROSS_PROJECT_CONFIDENTIAL":
            # Developer-tier only (NEX-ENT-004 is a DEVELOPER entitlement;
            # 7D defines no INTERN equivalent, and the data never grants one
            # to an intern — confirmed empty). Consistent with "INTERN...
            # cannot access developer or administrative sources by default."
            if role_code == "DEVELOPER":
                effective.add(source_id)
        elif classification == "ADMINISTRATIVE_RECORDS":
            # 7D: "intern_code_access: DENIED_BY_DEFAULT" — administrative
            # records stay admin-only regardless of any grant row found.
            if role_code != "INTERN":
                effective.add(source_id)
    return tuple(sorted(effective))


def resolve_demo_principal(token: str, store: GovernanceStore, *, tenant_id: str, workspace_id: str,
                           demo_mode_enabled: bool) -> Principal | None:
    # Demo identities have a fixed deployment scope. Never stamp a valid
    # NEXUS employee into another tenant: that would bypass the NEXUS-specific
    # source restriction in authorized_source_ids_for().
    if not demo_mode_enabled or tenant_id != NEXUS_TENANT_ID or workspace_id != NEXUS_WORKSPACE_ID:
        return None
    identity = _demo_identity_row(token, store)
    if identity is None:
        return None
    role_code = _role_code(identity["role_id"], store)
    if role_code not in ROLE_PERMISSIONS:
        return None
    source_ids = _effective_source_ids(role_code, identity["employee_id"], store)
    if not source_ids:
        return None
    return Principal(
        subject=f"nexus:{identity['employee_id']}", tenant_id=tenant_id, workspaces=(workspace_id,),
        permissions=ROLE_PERMISSIONS[role_code], source_ids=source_ids)


def refresh_principal_source_ids(principal: Principal, store: GovernanceStore) -> Principal:
    """Re-resolves a NEXUS principal's CURRENT role/source authorization
    against live governance state.

    Called by the agent tool-dispatch path (``backend/app/agent/tool_bridge.py``)
    immediately before each tool call in a long-running investigation. Without
    this, a grant revoked or expired *while a run is in progress* would stay
    usable for the rest of that run, since ``AgentExecutionContext`` is built
    once at run start — do not remove this call in favor of "just use
    exec_context.authorization directly" without re-introducing that gap.

    No-op (returns ``principal`` unchanged) for a non-NEXUS or already-ADMIN
    principal. A principal whose demo identity/role can no longer be resolved
    (e.g. deleted) loses all source access and permissions outright, rather
    than keeping its stale snapshot.
    """
    if Permission.ADMIN in principal.permissions or principal.tenant_id != NEXUS_TENANT_ID:
        return principal
    if not principal.subject.startswith("nexus:"):
        return principal
    employee_id = principal.subject.removeprefix("nexus:")
    rows = store.read("SELECT role_id FROM nexus_demo_identities WHERE employee_id=%s", (employee_id,))
    role_code = _role_code(rows[0]["role_id"], store) if rows else None
    if role_code not in ROLE_PERMISSIONS:
        return principal.model_copy(update={"source_ids": (), "permissions": frozenset()})
    source_ids = _effective_source_ids(role_code, employee_id, store)
    return principal.model_copy(update={"source_ids": source_ids, "permissions": ROLE_PERMISSIONS[role_code]})


def authorized_source_ids_for(principal: Principal) -> frozenset[str] | None:
    """The read-time source restriction to pass to ``GraphRepository`` for a
    given principal. ``None`` means "no additional restriction beyond
    acl_subjects" — the exact prior behavior — and is returned for every
    principal not positively identified as NEXUS-resolved (by tenant_id) or
    holding ADMIN (which already bypasses via ``allow_all``), so non-NEXUS
    callers (static env-JSON dev/test principals, other tenants) are
    completely unaffected."""
    if Permission.ADMIN in principal.permissions or principal.tenant_id != NEXUS_TENANT_ID:
        return None
    return frozenset(principal.source_ids)


def nexus_identity_summary(token: str, store: GovernanceStore, *, demo_mode_enabled: bool) -> dict | None:
    if not demo_mode_enabled:
        return None
    identity = _demo_identity_row(token, store)
    if identity is None:
        return None
    role_rows = store.read("SELECT role_code, data FROM nexus_roles WHERE role_id=%s", (identity["role_id"],))
    if not role_rows:
        return None
    role_code, role_data = role_rows[0]["role_code"], role_rows[0]["data"]
    employee_rows = store.read("SELECT display_name FROM nexus_employees WHERE employee_id=%s",
                               (identity["employee_id"],))
    display_name = employee_rows[0]["display_name"] if employee_rows else identity["employee_id"]
    project_ids = sorted(_assigned_project_ids(identity["employee_id"], store))
    source_ids = _effective_source_ids(role_code, identity["employee_id"], store)
    return {
        "employee_id": identity["employee_id"], "display_name": display_name, "role_code": role_code,
        "company_id": "nexus", "project_ids": project_ids, "source_ids": list(source_ids),
        "capabilities": role_data.get("capabilities", {}),
    }


def recipient_authorized_sources(authorization: AuthorizationContext) -> frozenset[str] | None:
    """ND-14B/14C shared helper (moved here from ``backend.app.agent.handoff`` to avoid a handoff<->
    freshness import cycle — this is fundamentally a governance/identity concern, not a handoff one).
    ``None`` means unrestricted (ADMIN or a non-NEXUS principal); otherwise the exact source set this
    principal may CURRENTLY read, same composition every Stage 12/04 tool handler already uses (see
    e.g. ``backend.app.tools.graph_tools._scoped_source_ids``)."""
    governance_scope = authorized_source_ids_for(authorization.principal)
    selection_scope = frozenset(authorization.principal.source_ids) if authorization.principal.source_ids else None
    if governance_scope is None:
        return selection_scope
    if selection_scope is None:
        return governance_scope
    return governance_scope & selection_scope


def principal_exists(subject: str, store: GovernanceStore) -> bool | None:
    """ND-14C section 8: whether ``subject`` (a ``nexus:<employee_id>``-shaped identity) is a real,
    known principal in the authoritative NEXUS employee registry. Returns ``None`` — explicitly
    "unknown, cannot validate" — for any subject not in that shape, or when the registry itself has no
    row for the implied employee_id in a way that would distinguish "doesn't exist" from "not NEXUS at
    all"; callers must treat ``None`` as "validation unavailable", never as "valid". There is
    deliberately no equivalent registry for non-NEXUS tenants in this codebase — this function does not
    invent one."""
    if not subject.startswith("nexus:"):
        return None
    employee_id = subject.removeprefix("nexus:")
    rows = store.read("SELECT 1 FROM nexus_employees WHERE employee_id=%s", (employee_id,))
    return bool(rows)
