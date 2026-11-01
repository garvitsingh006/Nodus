"""NODUS Stage 14 (ND-14C): persistent verification-freshness tracking and
authorized re-verification.

No second verification engine: ``reverify`` calls the existing, unmodified
``backend.app.agent.verifier.run_verification_pass`` — the exact function
ND-14A's resume path already uses — and records its result as a new,
preserved ``nodus_verification_revisions`` row via
``backend.app.agent.persistence.InvestigationStore``. Because
``run_verification_pass`` always re-fetches the underlying Stage 12 tool
fresh and iterates THAT fresh result's own findings (never the stale
checkpoint's), a newly introduced relationship the original investigation
never saw is discovered here automatically, the same way it already is on
an ordinary ND-14A resume — this module does not add a second
change-detection mechanism, only the durable history and diffing around it.
"""
from __future__ import annotations

import time

from backend.app.agent.contracts import FreshnessStatus, SpecialistRole
from backend.app.agent.persistence import InvestigationStore
from backend.app.agent.tool_bridge import refresh_authorization
from backend.app.agent.verifier import run_verification_pass
from backend.app.governance.identity import recipient_authorized_sources
from backend.app.security import AuthorizationContext
from backend.app.tools.contracts import ToolInvocation, ToolStatus
from backend.app.tools.executor import ToolExecutor

_REVERIFY_MAX_TOOL_CALLS = 8
_REVERIFY_TIMEOUT_SECONDS = 30.0


def _current_graph_revision(authorization: AuthorizationContext, tool_executor: ToolExecutor,
                            run_id: str) -> int | None:
    result = tool_executor.execute(ToolInvocation(
        tool_id="graph.revision", version="1.0.0", arguments={}, context=authorization,
        correlation_id=f"{run_id}:freshness-check"))
    if result.status != ToolStatus.SUCCESS:
        return None
    return (result.data or {}).get("revision")


def check_freshness(run_id: str, authorization: AuthorizationContext, tool_executor: ToolExecutor,
                    persistence: InvestigationStore) -> FreshnessStatus:
    """Cheap, non-disclosing check: does the current graph revision differ from the investigation's
    latest recorded verification? Never returns evidence content, only a status — safe to call before
    any authorization-sensitive decision (ND-14C section 5: a revision number alone is not protected
    source content)."""
    row = persistence.get_run(run_id)
    if row is None:
        return FreshnessStatus.UNAVAILABLE
    latest = persistence.latest_verification_revision(run_id)
    recorded_revision = latest["graph_revision"] if latest is not None else row.get("graph_revision")
    checkpoints = persistence.load_checkpoints(run_id)
    if SpecialistRole.VERIFICATION not in checkpoints or recorded_revision is None:
        return FreshnessStatus.UNAVAILABLE
    current = _current_graph_revision(authorization, tool_executor, run_id)
    if current is None:
        return FreshnessStatus.UNAVAILABLE
    return FreshnessStatus.CURRENT if current == recorded_revision else FreshnessStatus.REVERIFICATION_REQUIRED


def reverify(run_id: str, authorization: AuthorizationContext, tool_executor: ToolExecutor,
            persistence: InvestigationStore, *, actor_subject: str | None = None,
            impact_skill_id: str | None = None) -> dict:
    """ND-14C sections 3/4/5/7: authorized re-verification producing one new, preserved revision.

    ``authorization`` must be the CURRENT run assignee's own freshly-authenticated context — never a
    cached snapshot, and never an ADMIN/unattended substitute (section 5). If the current authorization
    does not cover evidence this investigation already touched, no detailed re-check is performed at
    all; the investigation is reported as requiring re-verification without disclosing what changed.
    """
    row = persistence.get_run(run_id)
    if row is None:
        return {"status": FreshnessStatus.UNAVAILABLE.value, "reason": "run not found"}

    refreshed = refresh_authorization(authorization, tool_executor)
    checkpoints = persistence.load_checkpoints(run_id)
    impact = checkpoints.get(SpecialistRole.IMPACT)
    verification = checkpoints.get(SpecialistRole.VERIFICATION)
    if impact is None or verification is None:
        return {"status": FreshnessStatus.UNAVAILABLE.value, "reason": "no verification to refresh"}

    # Section 5: the CURRENT assignee's own access, rechecked now -- never the original investigator's
    # or a prior owner's permission snapshot (handoff ownership is respected automatically: the caller
    # is expected to pass the current assignee's own authorization, same contract as
    # resume_coordinated_investigation).
    authorized_sources = recipient_authorized_sources(refreshed)
    observed_sources = {ref.split(":", 1)[0] for ref in impact.evidence_refs}
    if authorized_sources is not None and observed_sources - authorized_sources:
        return {"status": FreshnessStatus.REVERIFICATION_REQUIRED.value,
               "reason": "current authorization does not cover this investigation's evidence"}

    entity_id = row.get("entity_id")
    # The caller (coordinator.resume_coordinated_investigation) already derives this deterministically
    # from the stored objective via select_impact_skill -- that function lives in coordinator.py, and
    # importing it here would create a coordinator<->freshness import cycle, so it is passed in. The
    # IMPACT checkpoint's own recorded skill is only a fallback for a caller that has nothing else.
    if impact_skill_id is None:
        impact_skill_id = impact.skill_used.skill_id if impact.skill_used else None
    if impact_skill_id is None or entity_id is None:
        return {"status": FreshnessStatus.UNAVAILABLE.value, "reason": "no re-verifiable finding"}

    new_verification = run_verification_pass(
        impact=impact, impact_skill_id=impact_skill_id, impact_entity_id=entity_id, authorization=refreshed,
        tool_executor=tool_executor, run_id=run_id, max_tool_calls=_REVERIFY_MAX_TOOL_CALLS,
        deadline=time.monotonic() + _REVERIFY_TIMEOUT_SECONDS)

    old_ids = {c.claim_id for c in verification.claim_verifications}
    new_ids = {c.claim_id for c in new_verification.claim_verifications}
    added = sorted(new_ids - old_ids)
    removed = sorted(old_ids - new_ids)

    inserted = persistence.record_verification_revision(
        run_id, new_verification, new_verification.graph_revision, freshness_status=FreshnessStatus.REVERIFIED.value,
        trigger_reason="graph revision changed", actor_subject=actor_subject)
    if inserted:
        persistence.save_checkpoint(run_id, SpecialistRole.VERIFICATION, new_verification,
                                    new_verification.graph_revision)
        persistence.record_event(run_id, "REVERIFICATION_COMPLETED", actor_subject or row["assignee_subject"],
                                 resulting_state=FreshnessStatus.REVERIFIED.value)

    return {
        "status": FreshnessStatus.REVERIFIED.value if inserted else FreshnessStatus.CURRENT.value,
        "added_claim_ids": added, "removed_claim_ids": removed,
        "previous_graph_revision": verification.graph_revision,
        "new_graph_revision": new_verification.graph_revision,
        "result": new_verification,
    }
