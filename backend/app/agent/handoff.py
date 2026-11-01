"""NODUS Stage 14 (ND-14B): native, permission-aware Investigation Handoff
Packet assembly.

Deterministic only — no LLM call, no second evidence store, no raw model
conversation ever included. Built entirely from
``backend.app.agent.persistence.InvestigationStore``'s already-persisted,
already-authorized run/checkpoint records (the same ``SpecialistResult``
dumps ND-14A commits as checkpoints).

The whole point of this module: a packet is generated according to the
RECIPIENT's current permissions, never the original investigator's. Section
6's fail-closed policy is enforced here, not left to the caller — if the
recipient cannot access every source backing the investigation's evidence
references, ``build_handoff_packet`` returns ``None`` outright. There is no
partial-redaction mode; a caller that gets ``None`` may show a generic
handoff status, never a trimmed-down version of this packet's contents.
"""
from __future__ import annotations

from backend.app.agent.contracts import FreshnessStatus, InvestigationHandoffPacket, SpecialistRole
from backend.app.agent.freshness import check_freshness
from backend.app.agent.persistence import InvestigationStore
from backend.app.agent.tool_bridge import refresh_authorization
from backend.app.governance.identity import principal_exists, recipient_authorized_sources
from backend.app.security import AuthorizationContext
from backend.app.tools.executor import ToolExecutor

# Re-exported for backward compatibility: backend.app.agent.coordinator (and anything else written
# against ND-14B) imports this name from here. The actual implementation now lives in
# backend.app.governance.identity (ND-14C), since backend.app.agent.freshness needs it too and importing
# it from here would create a handoff<->freshness import cycle.
__all__ = ["build_handoff_packet", "recipient_authorized_sources"]

_PHASE_ORDER = (SpecialistRole.DISCOVERY, SpecialistRole.IMPACT, SpecialistRole.VERIFICATION,
               SpecialistRole.SYNTHESIS)


def build_handoff_packet(
    run_id: str, recipient_authorization: AuthorizationContext, tool_executor: ToolExecutor,
    persistence: InvestigationStore, *, intended_recipient: str | None = None,
) -> InvestigationHandoffPacket | None:
    """Returns ``None`` for every denial reason — wrong tenant, run not found, or (section 6's core rule)
    the recipient's current authorized source scope does not cover every evidence reference the
    investigation touched. ``recipient_authorization`` must be the recipient's OWN freshly-authenticated
    context, re-checked here via the same ``refresh_authorization`` every other pass uses — never a
    cached snapshot, and never the sender's or original creator's authorization."""
    row = persistence.get_run(run_id)
    if row is None or row["tenant_id"] != recipient_authorization.tenant_id:
        return None

    # ND-14C section 8: reject a recipient the authoritative registry positively knows does NOT exist.
    # ``None`` (no registry applies, e.g. a non-NEXUS subject) is left unvalidated rather than guessed.
    governance_store = getattr(tool_executor.dependencies, "governance_store", None)
    recipient_identity_validated: bool | None = None
    if governance_store is not None and intended_recipient is not None:
        recipient_identity_validated = principal_exists(intended_recipient, governance_store)
        if recipient_identity_validated is False:
            return None

    refreshed = refresh_authorization(recipient_authorization, tool_executor)
    authorized_sources = recipient_authorized_sources(refreshed)

    checkpoints = persistence.load_checkpoints(run_id)
    evidence_refs: set[str] = set()
    entity_ids: set[str] = set()
    for result in checkpoints.values():
        evidence_refs.update(result.evidence_refs)
        entity_ids.update(result.entity_ids)

    if authorized_sources is not None:
        for ref in evidence_refs:
            source_id = ref.split(":", 1)[0]
            if source_id not in authorized_sources:
                return None  # fail closed: no partial packet, no disclosed source name or count

    completed_phases = tuple(role.value for role in _PHASE_ORDER if role in checkpoints)
    remaining = tuple(role.value for role in _PHASE_ORDER if role not in checkpoints)
    current_phase = remaining[0] if remaining else None

    discovery = checkpoints.get(SpecialistRole.DISCOVERY)
    impact = checkpoints.get(SpecialistRole.IMPACT)
    verification = checkpoints.get(SpecialistRole.VERIFICATION)

    supported: list[str] = []
    potential: list[str] = []
    unverified: list[str] = []
    contradictions: list[str] = []
    stale: list[str] = []
    if verification is not None:
        for claim in verification.claim_verifications:
            label = f"{claim.claim_id}: {claim.claim_summary}"
            if claim.status.value == "CONTRADICTED":
                contradictions.append(label)
            elif claim.status.value == "STALE":
                stale.append(label)
            elif claim.status.value in ("UNSUPPORTED", "NOT_CHECKED", "PARTIALLY_SUPPORTED"):
                unverified.append(label)
            elif claim.status.value == "SUPPORTED":
                (potential if claim.claim_type.value == "DEPENDENCY_PATH" else supported).append(label)

    uncertainties = tuple(dict.fromkeys(
        (discovery.source_coverage_limitations if discovery else ())
        + (impact.source_coverage_limitations if impact else ())
        + (discovery.unresolved_conflicts if discovery else ())))

    graph_revision = max((r.graph_revision for r in checkpoints.values() if r.graph_revision is not None),
                        default=row.get("graph_revision"))

    authorization_status = "PAUSED_ACCESS" if row["status"] == "PAUSED_ACCESS" else "AUTHORIZED"
    next_point = f"Resume at {current_phase}" if current_phase else "All phases complete; no further action"

    # ND-14C section 6: the freshness check happens AT GENERATION TIME, every time -- a packet built
    # yesterday is never treated as permanently current just because it was authorized then.
    freshness = check_freshness(run_id, refreshed, tool_executor, persistence) \
        if SpecialistRole.VERIFICATION in checkpoints else FreshnessStatus.UNAVAILABLE

    return InvestigationHandoffPacket(
        run_id=run_id, objective=row["objective"], original_creator=row["principal_subject"],
        current_owner=row["assignee_subject"], intended_recipient=intended_recipient, status=row["status"],
        completed_phases=completed_phases, current_phase=current_phase, remaining_work=remaining,
        checkpoint_version=len(checkpoints), budget_usage=row["usage_json"] or {},
        entity_ids=tuple(sorted(entity_ids)), evidence_backed_findings=tuple(supported),
        potential_impacts=tuple(potential), uncertainties=uncertainties,
        supported_findings=tuple(supported), unverified_findings=tuple(unverified),
        contradictions=tuple(contradictions), stale_findings=tuple(stale),
        evidence_refs=tuple(sorted(evidence_refs)), graph_revision=graph_revision,
        outstanding_tasks=remaining, authorization_status=authorization_status,
        next_continuation_point=next_point, verification_freshness=freshness.value,
        recipient_identity_validated=recipient_identity_validated,
    )
