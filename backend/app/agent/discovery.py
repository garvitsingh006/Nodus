"""NODUS Stage 11: objective-driven incremental relationship discovery.

Builds on the GH-007A/B general investigator (``backend/app/agent/investigator.py``)
rather than forking a second agent loop. The model only ever calls the same
read-only tools that investigator already exposes (Step A — "examine existing
knowledge"); everything below is plain, deterministic orchestrator Python that
never lets the model write to the graph directly:

* ``effective_source_scope`` (ND-11A) computes ``selected_source_ids ∩
  principal's currently authorized source_ids`` — never trusting the caller's
  selection alone, and never silently running unscoped if the intersection is
  empty.
* ``run_scoped_investigation`` (ND-11A) is the scoped entry point: it builds
  an ``AgentExecutionContext`` whose principal's ``source_ids`` are narrowed
  to the effective scope (``backend/app/tools/graph_tools.py`` and friends'
  ``_scoped_source_ids`` additionally intersect this for every read, on top
  of whatever NEXUS-governance scoping already applied), runs Step A via the
  existing ``run_investigation``, and then drives Step B/C below over exactly
  what that run actually, authorizedly observed.
* ``RelationshipCandidate`` (ND-11B) is the typed, orchestrator-constructed
  candidate contract. Candidates here are built heuristically from the run's
  own observed evidence (same-source-record co-occurrence of two entities the
  run actually looked up, with no existing direct edge between them) rather
  than parsed out of the model's free text — satisfying "never invent
  evidence, entity IDs, or certainty" by construction rather than by trusting
  the model's own JSON.
* ``validate_and_commit`` (ND-11C) independently re-checks every candidate
  (endpoints observed, evidence observed + in-scope + still current, ontology
  validity, non-empty evidence) before calling the existing
  ``GraphRepository.ingest`` — the same idempotent, content-hash write path
  Stage 10's connectors use, so re-running the same objective over unchanged
  evidence reuses the same relationship id rather than duplicating it.
"""
from __future__ import annotations

import hashlib
from typing import Sequence

from pydantic import Field

from backend.app.agent.contracts import (
    AgentExecutionContext, ApprovedSkill, InvestigationRequest, InvestigationResult, InvestigationStatus,
    RuntimeLimits,
)
from backend.app.agent.investigator import run_investigation
from backend.app.contracts import Record
from backend.app.gateway.gateway import ModelGateway
from backend.app.governance.identity import authorized_source_ids_for
from backend.app.graph.authority import AuthorityRule
from backend.app.graph.models import AssertionOrigin, EntityInput, RelationInput, RelationType, SourceRecordInput
from backend.app.graph.ontology import validate_relationship
from backend.app.graph.repository import GraphRepository
from backend.app.security import AuthorizationContext, Permission, Principal
from backend.app.skills.registry import SkillRegistry
from backend.app.tools.executor import ToolExecutor
from backend.app.tools.registry import ToolRegistry

# A dedicated, internal pseudo-source namespace for orchestrator-discovered
# relationships. Never a real connector source — discovered facts are kept
# additive and separate from every externally-sourced record, so committing
# one never touches (supersedes/retracts) the original evidence record that
# justified it. Readable only by a repository constructed with
# ``authorized_source_ids=None``/``allow_all`` (mirrors how Stage 10
# connectors write without being subject to a particular reading principal's
# source restriction).
DISCOVERY_SOURCE_ID = "nodus.discovery"

# Bounds Step B's own fan-out: at most this many of the run's observed
# entities are considered for candidate co-occurrence, keeping expansion
# bounded exactly like the agent's own tool-call/turn limits.
MAX_CANDIDATE_ENTITIES = 25


class RelationshipCandidate(Record):
    """One orchestrator-proposed, not-yet-authoritative relationship. Never
    constructed from the model's free text — see module docstring."""
    source_entity_id: str = Field(min_length=1, max_length=500)
    target_entity_id: str = Field(min_length=1, max_length=500)
    relation_type: RelationType
    origin: AssertionOrigin = AssertionOrigin.OBSERVED
    evidence_refs: tuple[str, ...] = Field(min_length=1)
    supporting_source_ids: tuple[str, ...] = Field(min_length=1)
    confidence_note: str | None = Field(default=None, max_length=500)


class CandidateRejection(Record):
    source_entity_id: str
    target_entity_id: str
    relation_type: RelationType | None = None
    reason: str = Field(min_length=1, max_length=500)


class DiscoveryOutcome(Record):
    candidates_considered: int = Field(default=0, ge=0)
    committed_relationship_ids: tuple[str, ...] = ()
    reused_relationship_ids: tuple[str, ...] = ()
    rejected: tuple[CandidateRejection, ...] = ()


def effective_source_scope(selected_source_ids: Sequence[str], principal: Principal) -> frozenset[str]:
    """``selected_source_ids ∩ principal's currently authorized source_ids``.

    ADMIN and a principal with no explicit ``source_ids`` restriction of its
    own are bounded by the caller's selection alone (there is nothing else to
    intersect against); every other principal is also bounded by
    ``authorized_source_ids_for`` (NEXUS governance-derived access) and its
    own ``source_ids``. Anything the caller selects outside this is silently
    dropped, never promoted to "authorized by asking for it".
    """
    selected = frozenset(source_id for source_id in selected_source_ids if source_id)
    if not selected:
        return frozenset()
    governance_scope = authorized_source_ids_for(principal)
    own_scope = frozenset(principal.source_ids) if principal.source_ids else None
    if governance_scope is None and own_scope is None:
        return selected
    if governance_scope is None:
        return selected & own_scope
    if own_scope is None:
        return selected & governance_scope
    return selected & governance_scope & own_scope


async def run_scoped_investigation(
    *,
    objective: str,
    selected_source_ids: Sequence[str],
    principal: Principal,
    workspace_id: str,
    run_id: str,
    graph_store: object,
    model_gateway: ModelGateway,
    tool_registry: ToolRegistry,
    tool_executor: ToolExecutor,
    skill_registry: SkillRegistry,
    authority_rules: tuple[AuthorityRule, ...] = (),
    approved_skills: tuple[ApprovedSkill, ...] = (ApprovedSkill(skill_id="graph-trace", version="1.0.0"),),
    limits: RuntimeLimits | None = None,
    entity_id: str | None = None,
) -> InvestigationResult:
    """ND-11A scoped entry point.

    Only after the effective scope is computed do any of ``selected_source_ids``
    enter run scope; an empty effective scope returns a ``BLOCKED`` result
    without starting an agent run at all (never silently runs unscoped).
    """
    scope = effective_source_scope(selected_source_ids, principal)
    if not scope:
        return InvestigationResult(
            run_id=run_id, status=InvestigationStatus.BLOCKED,
            summary="No authorized, selected sources are in scope for this objective; the investigation "
                    "was blocked before any tool call was made.",
            source_coverage_limitations=("effective source scope is empty (selected ∩ authorized = ∅)",),
        )

    # Narrow the run's principal to exactly the effective scope. This is the
    # one new trust decision ND-11A makes; every tool call in Step A is then
    # additionally bounded to it by backend.app.tools.*._scoped_source_ids.
    scoped_principal = principal.model_copy(update={"source_ids": tuple(sorted(scope))})
    permissions = scoped_principal.permissions
    auth = AuthorizationContext(
        principal=scoped_principal, tenant_id=scoped_principal.tenant_id, workspace_id=workspace_id,
        task_id=run_id, permissions=permissions)
    exec_context = AgentExecutionContext(
        authorization=auth, run_id=run_id, approved_skills=approved_skills, limits=limits or RuntimeLimits())
    request = InvestigationRequest(question=objective, entity_id=entity_id)

    result = await run_investigation(request, exec_context, model_gateway, tool_registry, tool_executor,
                                     skill_registry)

    observed_entities = frozenset(result.entity_ids)
    if not observed_entities or not (Permission.READ_GRAPH in permissions):
        return result  # nothing Step A actually observed to expand from

    can_evidence = Permission.READ_EVIDENCE in permissions
    allow_all = Permission.ADMIN in permissions
    read_repo = GraphRepository(
        graph_store, scoped_principal.tenant_id, workspace_id, scoped_principal.subject,
        allow_all=allow_all, can_evidence=can_evidence, authority_rules=authority_rules,
        authorized_source_ids=None if allow_all else scope)
    if not read_repo.can_evidence:
        return result  # no evidence permission -> no evidence-backed candidates can ever be built

    observed_evidence_refs: set[str] = set(result.evidence_refs)
    candidates = build_candidates(read_repo, observed_entities, scope, observed_evidence_refs)

    write_repo = GraphRepository(
        graph_store, scoped_principal.tenant_id, workspace_id, "nodus-discovery",
        allow_all=True, can_evidence=True, authority_rules=authority_rules)
    outcome = validate_and_commit(read_repo, write_repo, candidates, observed_entities, scope,
                                  frozenset(observed_evidence_refs))

    relationship_ids = tuple(sorted(
        set(result.relationship_ids) | set(outcome.committed_relationship_ids) | set(outcome.reused_relationship_ids)
    ))
    limitations = result.source_coverage_limitations + tuple(
        f"candidate {rej.source_entity_id}->{rej.target_entity_id}"
        f"{f' ({rej.relation_type.value})' if rej.relation_type else ''}: {rej.reason}"
        for rej in outcome.rejected)
    status = result.status
    if outcome.rejected and status == InvestigationStatus.COMPLETE:
        status = InvestigationStatus.PARTIAL

    return result.model_copy(update={
        "relationship_ids": relationship_ids,
        "newly_discovered_relationship_ids": outcome.committed_relationship_ids,
        "reused_relationship_ids": outcome.reused_relationship_ids,
        "source_coverage_limitations": limitations,
        "status": status,
    })


def build_candidates(repo: GraphRepository, observed_entity_ids: frozenset[str], effective_source_ids: frozenset[str],
                     observed_evidence_refs: set[str]) -> list[RelationshipCandidate]:
    """ND-11B: heuristic, deterministic candidate construction — two entities
    the run actually observed, co-occurring in the same authorized, in-scope
    source record, with no existing direct edge between them already.

    Mutates ``observed_evidence_refs`` in place to add every evidence
    reference this bounded, authorized fetch actually returned, so
    ``validate_and_commit`` can confirm each candidate's evidence was really
    observed during this run (Step B counts as part of the run's own
    observation, exactly like an agent tool call would).
    """
    entities = sorted(observed_entity_ids)[:MAX_CANDIDATE_ENTITIES]
    by_record: dict[tuple[str, str], set[str]] = {}
    for entity_id in entities:
        try:
            page = repo.evidence(entity_id, limit=50)
        except PermissionError:
            return []
        for item in page["items"]:
            if item.source_id not in effective_source_ids:
                continue  # never let an unselected/unauthorized source contribute a candidate
            observed_evidence_refs.add(f"{item.source_id}:{item.record_id}")
            by_record.setdefault((item.source_id, item.record_id), set()).add(entity_id)

    candidates: list[RelationshipCandidate] = []
    seen_pairs: set[tuple[str, str]] = set()
    for (source_id, record_id), members in sorted(by_record.items()):
        ordered = sorted(members)
        for i in range(len(ordered)):
            for j in range(i + 1, len(ordered)):
                a, b = ordered[i], ordered[j]
                if (a, b) in seen_pairs:
                    continue
                seen_pairs.add((a, b))
                existing = repo.paths(a, b, max_depth=1, limit=1)
                if existing["paths"]:
                    continue  # already directly connected -- not a new discovery
                candidates.append(RelationshipCandidate(
                    source_entity_id=a, target_entity_id=b, relation_type=RelationType.RELATED_TO,
                    origin=AssertionOrigin.OBSERVED, evidence_refs=(f"{source_id}:{record_id}",),
                    supporting_source_ids=(source_id,),
                    confidence_note=f"Co-occurrence of two run-observed entities in {source_id}:{record_id}; "
                                    f"no existing direct relationship found.",
                ))
    return candidates


def _validate_candidate(repo: GraphRepository, candidate: RelationshipCandidate,
                        observed_entity_ids: frozenset[str], effective_source_ids: frozenset[str],
                        observed_evidence_refs: frozenset[str]) -> str | None:
    """ND-11C: plain-Python validation, independent of any model confidence.
    Returns ``None`` if the candidate passes every check, else a human-
    readable rejection reason. A candidate failing ANY check is reported as a
    coverage limitation / unresolved question, never silently dropped."""
    if candidate.source_entity_id not in observed_entity_ids or candidate.target_entity_id not in observed_entity_ids:
        return "one or both endpoint entities were never observed via an authorized tool call during this run"
    if not candidate.evidence_refs:
        return "no supporting evidence_refs -- a hypothesis is never promoted to the factual graph"
    for ref in candidate.evidence_refs:
        if ":" not in ref:
            return f"malformed evidence reference {ref!r}"
        source_id, _, record_id = ref.partition(":")
        if source_id not in effective_source_ids:
            return f"evidence source {source_id!r} is outside the effective authorized+selected scope for this run"
        if ref not in observed_evidence_refs:
            return f"evidence reference {ref!r} was never actually observed during this run"
        if repo.source_record(source_id, record_id) is None:
            return f"evidence record {ref!r} is no longer current (retracted, inactive, or inaccessible)"

    source_entity = repo.get_entity(candidate.source_entity_id)
    target_entity = repo.get_entity(candidate.target_entity_id)
    if source_entity is None or target_entity is None:
        return "one or both endpoint entities could not be re-resolved at validation time"
    if source_entity.type is None or target_entity.type is None:
        return "one or both endpoint entities have a conflicting/unresolved type -- cannot validate ontology"
    try:
        validate_relationship(candidate.relation_type, source_entity.type, target_entity.type, candidate.origin)
    except ValueError as exc:
        return str(exc)
    return None


def _candidate_record_id(candidate: RelationshipCandidate) -> str:
    """Deterministic, evidence-keyed id: the SAME real-world candidate
    (same endpoints/type/evidence) always maps to the same discovery
    SourceRecord, independent of which objective or run found it first --
    this is what makes a repeat investigation over unchanged evidence reuse
    the existing relationship rather than duplicating it (``GraphRepository
    .ingest``'s own content-hash idempotency then does the rest)."""
    digest = hashlib.sha256("\x1f".join([
        candidate.source_entity_id, candidate.target_entity_id, candidate.relation_type.value,
        candidate.origin.value, *sorted(candidate.evidence_refs),
    ]).encode("utf-8")).hexdigest()
    return f"discovered-{digest[:40]}"


def _commit_candidate(write_repo: GraphRepository, candidate: RelationshipCandidate,
                      source_entity, target_entity) -> tuple[str | None, bool]:
    """Commits via the existing ``GraphRepository.ingest`` write path only
    (never a second write mechanism). Returns ``(relationship_id, changed)``;
    ``changed`` is ``False`` exactly when ``ingest`` recognized a byte-
    identical replay (idempotent reuse)."""
    record_id = _candidate_record_id(candidate)
    record = SourceRecordInput(
        source_id=DISCOVERY_SOURCE_ID, record_id=record_id,
        title=f"Discovered: {candidate.source_entity_id} {candidate.relation_type.value} "
             f"{candidate.target_entity_id}",
        acl_subjects=[],
        entities=[
            EntityInput(external_id=candidate.source_entity_id, canonical_id=candidate.source_entity_id,
                       type=source_entity.type, name=source_entity.name or candidate.source_entity_id),
            EntityInput(external_id=candidate.target_entity_id, canonical_id=candidate.target_entity_id,
                       type=target_entity.type, name=target_entity.name or candidate.target_entity_id),
        ],
        relationships=[RelationInput(
            source_external_id=candidate.source_entity_id, target_external_id=candidate.target_entity_id,
            type=candidate.relation_type, origin=candidate.origin)],
    )
    outcome = write_repo.ingest(record)
    found = write_repo.paths(candidate.source_entity_id, candidate.target_entity_id, max_depth=1, limit=10)
    relationship_id = None
    for path in found["paths"]:
        for rel in path["relationships"]:
            if (rel.type == candidate.relation_type and rel.source_id == candidate.source_entity_id
                    and rel.target_id == candidate.target_entity_id):
                relationship_id = rel.id
                break
        if relationship_id:
            break
    return relationship_id, bool(outcome["changed"])


def validate_and_commit(read_repo: GraphRepository, write_repo: GraphRepository,
                        candidates: list[RelationshipCandidate], observed_entity_ids: frozenset[str],
                        effective_source_ids: frozenset[str],
                        observed_evidence_refs: frozenset[str]) -> DiscoveryOutcome:
    """ND-11C: independently validates every candidate and commits only the
    ones that pass every check, via the existing idempotent ``ingest`` path."""
    rejected: list[CandidateRejection] = []
    accepted: list[RelationshipCandidate] = []
    for candidate in candidates:
        reason = _validate_candidate(read_repo, candidate, observed_entity_ids, effective_source_ids,
                                     observed_evidence_refs)
        if reason is not None:
            rejected.append(CandidateRejection(
                source_entity_id=candidate.source_entity_id, target_entity_id=candidate.target_entity_id,
                relation_type=candidate.relation_type, reason=reason))
            continue
        accepted.append(candidate)

    committed: list[str] = []
    reused: list[str] = []
    for candidate in accepted:
        source_entity = read_repo.get_entity(candidate.source_entity_id)
        target_entity = read_repo.get_entity(candidate.target_entity_id)
        relationship_id, changed = _commit_candidate(write_repo, candidate, source_entity, target_entity)
        if relationship_id is None:
            rejected.append(CandidateRejection(
                source_entity_id=candidate.source_entity_id, target_entity_id=candidate.target_entity_id,
                relation_type=candidate.relation_type,
                reason="ingest succeeded but the committed relationship could not be re-resolved"))
            continue
        (committed if changed else reused).append(relationship_id)

    return DiscoveryOutcome(
        candidates_considered=len(candidates), committed_relationship_ids=tuple(sorted(set(committed))),
        reused_relationship_ids=tuple(sorted(set(reused))), rejected=tuple(rejected))
