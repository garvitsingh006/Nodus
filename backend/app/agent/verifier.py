"""NODUS Stage 13 (ND-13B): independent, deterministic evidence verification.

Deliberately plain Python — no LLM call. ND-13A already showed a specialist
pass does not need to be model-driven (its own SYNTHESIS pass is plain
Python too); re-asking a model "does this look right" would not be
independent verification anyway (see module docstring section below), and
the existing Stage 12 tools are already deterministic enough to check
against directly.

Independence comes from WHERE the data verified here comes from: the
verifier never trusts the impact specialist's own ``SpecialistResult``
narrative as ground truth. It re-executes the same underlying Stage 12 tool
itself, through the same ``backend.app.tools.executor.ToolExecutor`` gateway,
under its OWN freshly-refreshed ``AuthorizationContext`` — never a client-
supplied or model-supplied one — and only then compares what it just
independently observed against what the impact pass claimed to have seen.
A tool call that fails closed (``FORBIDDEN``/``NOT_FOUND``) becomes
``NOT_CHECKED``/``UNSUPPORTED`` here, never a silently-promoted ``SUPPORTED``.

For a PR-impact finding's multi-hop ``relationship_path``, the verifier
independently re-derives the SAME path with a DIFFERENT tool
(``graph.find_paths``) rather than re-running ``pr.change_impact``'s own
internal traversal a second time — re-running the identical code path over
the identical data would not be an independent check, just a cached repeat.

Section 5 (evidence laundering): two findings citing the exact same
``(relationship_path, edge_types)`` are the same underlying claim restated,
not two independent confirmations — the second is reported as a duplicate,
never as additional corroboration.

Section 4 (semantic distinction): a ``DEPENDENCY_PATH`` claim verified
SUPPORTED only ever means the graph relationship chain itself currently
exists and is current — it is reported as a POTENTIAL effect in the final
synthesis (``backend/app/agent/coordinator.py``), never as a demonstrated
failure. No Stage 12 tool here asserts an actual technical failure, so this
module never produces one either.
"""
from __future__ import annotations

import time

from backend.app.agent.contracts import (
    ClaimType, ClaimVerificationResult, InvestigationStatus, SpecialistResult, SpecialistRole, VerificationStatus,
)
from backend.app.security import AuthorizationContext
from backend.app.tools.contracts import ToolInvocation, ToolStatus
from backend.app.tools.executor import ToolExecutor

# (tool_id, version, entity-id argument name, output field carrying explicit
# multi-hop relationship_path/edge_types findings — only pr-impact's shape
# has one today). Public/shared: ``backend.app.agent.coordinator``'s own
# deterministic-first IMPACT pass (ND-13E) uses the exact same mapping, so
# the "which tool backs which skill" decision is made in exactly one place.
IMPACT_TOOL_BY_SKILL: dict[str, tuple[str, str, str, str | None]] = {
    "pr-impact": ("pr.change_impact", "1.0.0", "pr_id", "findings"),
    "blocker-chain": ("work.blocker_chain", "1.0.0", "issue_id", None),
    "decision-genealogy": ("decision.genealogy", "1.0.0", "decision_id", None),
    "expert-finder": ("people.find_experts", "1.0.0", "target_id", None),
}

MAX_DEPENDENCY_CLAIMS_CHECKED = 3

# Generic, never-resource-naming explanations for an access-denied re-check —
# section 6: never reveal through an explanation whether restricted evidence
# exists.
_ACCESS_DENIED_EXPLANATION = "Independent re-check could not be completed with the current authorization."


def _not_checked(claim_id: str, claim_type: ClaimType, summary: str, *, reason: str) -> ClaimVerificationResult:
    return ClaimVerificationResult(claim_id=claim_id, claim_type=claim_type, claim_summary=summary,
                                   status=VerificationStatus.NOT_CHECKED, explanation=reason)


def _verify_dependency_path(tool_executor: ToolExecutor, authorization: AuthorizationContext, run_id: str,
                            finding: dict) -> ClaimVerificationResult:
    changed = finding.get("changed_resource", "?")
    affected = finding.get("affected_resource", "?")
    classification = finding.get("classification", "?")
    path = tuple(finding.get("relationship_path") or ())
    edge_types = tuple(finding.get("edge_types") or ())
    claim_id = f"{changed}->{affected}:{classification}"
    summary = (f"Potential impact path {changed} -> {affected} ({classification}) — a verified dependency "
              f"chain, never a guarantee that a change will actually break {affected}.")

    if len(path) < 2:
        return _not_checked(claim_id, ClaimType.DEPENDENCY_PATH, summary,
                            reason="finding did not carry a checkable relationship path")

    result = tool_executor.execute(ToolInvocation(
        tool_id="graph.find_paths", version="1.0.0",
        arguments={"source_id": path[0], "target_id": path[-1], "max_depth": max(1, len(path) - 1), "limit": 10},
        context=authorization, correlation_id=f"{run_id}:verify:path"))

    if result.status == ToolStatus.FORBIDDEN:
        return ClaimVerificationResult(claim_id=claim_id, claim_type=ClaimType.DEPENDENCY_PATH, claim_summary=summary,
                                       status=VerificationStatus.NOT_CHECKED, explanation=_ACCESS_DENIED_EXPLANATION)
    if result.status not in (ToolStatus.SUCCESS, ToolStatus.INCOMPLETE):
        return ClaimVerificationResult(
            claim_id=claim_id, claim_type=ClaimType.DEPENDENCY_PATH, claim_summary=summary,
            status=VerificationStatus.UNSUPPORTED,
            explanation="The relationship path could not be independently re-derived via graph.find_paths.")

    for candidate in (result.data or {}).get("paths") or []:
        node_ids = [node.get("id") for node in candidate.get("nodes") or []]
        rel_types = [rel.get("type") for rel in candidate.get("relationships") or []]
        if node_ids == list(path) and rel_types == list(edge_types):
            return ClaimVerificationResult(
                claim_id=claim_id, claim_type=ClaimType.DEPENDENCY_PATH, claim_summary=summary,
                status=VerificationStatus.SUPPORTED, graph_revision=result.graph_revision,
                explanation="Independently re-derived the same relationship chain via graph.find_paths; this "
                           "confirms the dependency exists, not that a failure will occur.")

    return ClaimVerificationResult(
        claim_id=claim_id, claim_type=ClaimType.DEPENDENCY_PATH, claim_summary=summary,
        status=VerificationStatus.UNSUPPORTED, graph_revision=result.graph_revision,
        explanation="graph.find_paths no longer reproduces this exact relationship chain; it may have changed "
                   "or never matched the claimed direction/type.")


def run_verification_pass(*, impact: SpecialistResult, impact_skill_id: str | None,
                          impact_entity_id: str | None, authorization: AuthorizationContext,
                          tool_executor: ToolExecutor, run_id: str, max_tool_calls: int,
                          deadline: float) -> SpecialistResult:
    """ND-13B's VERIFICATION pass. Never raises; a failure to even start
    becomes a single skipped ``SpecialistResult``.

    ``authorization`` must already be the freshly-refreshed context for THIS
    pass (see ``backend.app.agent.tool_bridge.refresh_authorization``) —
    never a stale snapshot carried over from the impact pass.
    """
    if max_tool_calls <= 0 or time.monotonic() >= deadline:
        return SpecialistResult(role=SpecialistRole.VERIFICATION, status=InvestigationStatus.INCOMPLETE,
                                skipped_reason="shared budget exhausted before verification could start")

    mapping = IMPACT_TOOL_BY_SKILL.get(impact_skill_id or "")
    if mapping is None or impact_entity_id is None:
        return SpecialistResult(role=SpecialistRole.VERIFICATION, status=InvestigationStatus.INCOMPLETE,
                                skipped_reason="no verifiable Stage 12 finding to check for this question")

    tool_id, version, arg_name, findings_field = mapping
    claims: list[ClaimVerificationResult] = []
    tool_calls_made = 0

    fresh = tool_executor.execute(ToolInvocation(
        tool_id=tool_id, version=version, arguments={arg_name: impact_entity_id}, context=authorization,
        correlation_id=f"{run_id}:verify:{tool_id}"))
    tool_calls_made += 1
    base_claim_id = f"{tool_id}:{impact_entity_id}"
    base_summary = f"The {tool_id} result for {impact_entity_id} is still current and accessible."

    if fresh.status == ToolStatus.FORBIDDEN:
        claims.append(ClaimVerificationResult(claim_id=base_claim_id, claim_type=ClaimType.ENTITY_ASSERTION,
                                              claim_summary=base_summary, status=VerificationStatus.NOT_CHECKED,
                                              explanation=_ACCESS_DENIED_EXPLANATION))
    elif fresh.status == ToolStatus.NOT_FOUND:
        claims.append(ClaimVerificationResult(
            claim_id=base_claim_id, claim_type=ClaimType.ENTITY_ASSERTION, claim_summary=base_summary,
            status=VerificationStatus.UNSUPPORTED,
            explanation="The underlying entity could not be independently re-resolved."))
    elif fresh.status not in (ToolStatus.SUCCESS, ToolStatus.INCOMPLETE):
        claims.append(_not_checked(base_claim_id, ClaimType.ENTITY_ASSERTION, base_summary,
                                   reason=f"independent re-check returned {fresh.status.value}"))
    else:
        prior_refs = set(impact.evidence_refs)
        fresh_refs = set(fresh.evidence_refs)
        stale = bool(prior_refs) and bool(prior_refs - fresh_refs) and \
            impact.graph_revision is not None and fresh.graph_revision is not None and \
            fresh.graph_revision > impact.graph_revision
        if stale:
            claims.append(ClaimVerificationResult(
                claim_id=base_claim_id, claim_type=ClaimType.ENTITY_ASSERTION, claim_summary=base_summary,
                status=VerificationStatus.STALE, supporting_evidence_refs=tuple(sorted(fresh_refs)),
                graph_revision=fresh.graph_revision,
                explanation="The graph changed since the impact pass; some previously-cited evidence is no "
                           "longer present."))
        else:
            claims.append(ClaimVerificationResult(
                claim_id=base_claim_id, claim_type=ClaimType.ENTITY_ASSERTION, claim_summary=base_summary,
                status=VerificationStatus.SUPPORTED, supporting_evidence_refs=tuple(sorted(fresh_refs)),
                graph_revision=fresh.graph_revision,
                explanation="Independently re-fetched via the same Stage 12 tool, under the verifier's own "
                           "freshly-refreshed authorization."))

        if findings_field is not None:
            findings = (fresh.data or {}).get(findings_field) or []
            seen: set[tuple] = set()
            for finding in findings[:MAX_DEPENDENCY_CLAIMS_CHECKED]:
                path = tuple(finding.get("relationship_path") or ())
                edge_types = tuple(finding.get("edge_types") or ())
                dedup_key = (path, edge_types)
                changed = finding.get("changed_resource", "?")
                affected = finding.get("affected_resource", "?")
                classification = finding.get("classification", "?")
                claim_id = f"{changed}->{affected}:{classification}"
                if dedup_key in seen:
                    # Section 5: a repeated citation of the same path is never counted as a second,
                    # independent confirmation.
                    claims.append(ClaimVerificationResult(
                        claim_id=f"{claim_id}#dup", claim_type=ClaimType.DEPENDENCY_PATH,
                        claim_summary=f"Duplicate restatement of {claim_id}", status=VerificationStatus.NOT_CHECKED,
                        explanation="Identical relationship path already checked for this investigation; not "
                                   "treated as additional independent corroboration.",
                        limitations=("duplicate_of_prior_claim",)))
                    continue
                seen.add(dedup_key)
                if tool_calls_made >= max_tool_calls or time.monotonic() >= deadline:
                    claims.append(_not_checked(claim_id, ClaimType.DEPENDENCY_PATH,
                                               f"Potential impact path {changed} -> {affected}",
                                               reason="shared verification budget exhausted"))
                    continue
                claims.append(_verify_dependency_path(tool_executor, authorization, run_id, finding))
                tool_calls_made += 1

    statuses = {c.status for c in claims}
    if VerificationStatus.CONTRADICTED in statuses:
        status = InvestigationStatus.PARTIAL
    elif statuses <= {VerificationStatus.SUPPORTED}:
        status = InvestigationStatus.COMPLETE
    elif statuses & {VerificationStatus.NOT_CHECKED}:
        status = InvestigationStatus.PARTIAL if statuses - {VerificationStatus.NOT_CHECKED} else \
            InvestigationStatus.INCOMPLETE
    else:
        status = InvestigationStatus.PARTIAL

    summary = f"Independently checked {len(claims)} claim(s) from the {tool_id} finding(s): " + \
        ", ".join(f"{c.claim_id}={c.status.value}" for c in claims)

    # entity_id is echoed back only once the base re-check actually reached the tool (even a NOT_FOUND
    # confirms the id was usable) -- never on FORBIDDEN/NOT_CHECKED, where confirming or denying its
    # existence at all would itself be a disclosure beyond what the caller's own input already was.
    confirmed_entity_ids = (impact_entity_id,) if fresh.status != ToolStatus.FORBIDDEN else ()
    return SpecialistResult(
        role=SpecialistRole.VERIFICATION, status=status, entity_ids=confirmed_entity_ids,
        evidence_refs=tuple(sorted({ref for c in claims for ref in c.supporting_evidence_refs})),
        graph_revision=fresh.graph_revision if fresh.status in (ToolStatus.SUCCESS, ToolStatus.INCOMPLETE) else None,
        summary=summary[:4000], tool_calls_made=tool_calls_made, tool_ids_called=(tool_id,),
        claim_verifications=tuple(claims),
    )
