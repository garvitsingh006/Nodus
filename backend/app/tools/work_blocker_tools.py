"""ND-12B: deterministic, evidence-based engineering-issue blocker-chain
evaluation (E16 — directed cross-team blocker chains).

Reuses the exact traversal/authorization/finding-contract discipline
established by ND-12A (``backend.app.tools.pr_impact_tools``): no new graph
engine, no new authoritative "effects" store, and no model-asserted
conclusions. The only new graph primitive is
``GraphRepository.blockers_of`` (a one-line generalization of the existing
``reverse_dependencies`` traversal to the ``BLOCKS`` relation type).

Direction: the ontology's ``BLOCKS`` relation already reads "source work
item blocks target work item from proceeding" (``ISSUE_A -BLOCKS-> ISSUE_B``
means A must be addressed before B can proceed). ``blockers_of(ISSUE_B)``
therefore returns exactly the issues that block ``ISSUE_B`` -- the same
reverse-edge traversal ``reverse_dependencies`` already uses for
``DEPENDS_ON``, generalized rather than duplicated. A source that instead
asserts "blocked by" is simply the reverse of the same single relation type;
there is nothing to normalize at this ontology, since no separate
``BLOCKED_BY`` relation type exists (see ``backend.app.graph.ontology``).

A blocker finding never asserts that an engineering blocker implies a
runtime/deployment dependency -- it states only that an evidence-backed
BLOCKS edge exists and what its current/resolved/historical status is.
"""
from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field

from backend.app.governance.identity import authorized_source_ids_for
from backend.app.graph.models import Entity, EntityType, Relationship
from backend.app.graph.repository import GraphRepository
from backend.app.security import AuthorizationContext, Permission
from backend.app.tools.contracts import ExecutionLimits
from backend.app.tools.registry import SideEffect, ToolDependencies, ToolRegistry, ToolSpec

WORK_BLOCKER_CHAIN_TOOL_ID = "work.blocker_chain"
WORK_BLOCKER_CHAIN_VERSION = "1.0.0"

MAX_BLOCKER_DEPTH = 5
# The loader (ND-12B) projects an engineering issue's workflow status into
# its TASK entity's otherwise-unused `description`; "DONE" is the fixture's
# resolved-state value. A blocker whose source uses different status
# vocabulary simply never matches this and is conservatively treated as
# unresolved rather than guessed at.
_RESOLVED_STATUS_VALUES = frozenset({"DONE", "RESOLVED", "CLOSED"})


class BlockerStatus(StrEnum):
    UNRESOLVED = "UNRESOLVED"
    RESOLVED = "RESOLVED"
    HISTORICAL = "HISTORICAL"


class BlockerFinding(BaseModel):
    blocking_issue: str
    blocking_issue_status: str | None
    blocked_issue: str
    chain_path: list[str]
    chain_length: int
    status: BlockerStatus
    cross_team: bool | None = Field(
        default=None, description="True/False only when both issues' team ownership is evidence-backed; "
                                  "None when team ownership is not established for one or both sides.")
    evidence_refs: tuple[str, ...]
    source_revisions: tuple[str | None, ...]
    explanation: str
    uncertainty: str
    suggested_verification: str


class WorkBlockerChainInput(BaseModel):
    issue_id: str = Field(min_length=1, max_length=500)
    max_depth: int = Field(default=3, ge=1, le=MAX_BLOCKER_DEPTH)
    limit: int = Field(default=25, ge=1, le=100)
    include_history: bool = False


class WorkBlockerChainOutput(BaseModel):
    issue_id: str
    direct_blocker_ids: list[str]
    findings: list[BlockerFinding]
    unresolved_blocker_ids: list[str]
    coverage: str = "source_backed_only"


def _repository(context: AuthorizationContext, dependencies: ToolDependencies) -> GraphRepository:
    if dependencies.graph_store is None:
        raise RuntimeError("Graph store dependency is not configured")
    allow_all = Permission.ADMIN in context.principal.permissions
    can_evidence = Permission.READ_EVIDENCE in context.permissions
    return GraphRepository(
        dependencies.graph_store, context.tenant_id, context.workspace_id, context.principal.subject,
        allow_all=allow_all, can_evidence=can_evidence, authority_rules=dependencies.authority_rules,
        authorized_source_ids=_scoped_source_ids(context),
    )


def _scoped_source_ids(context: AuthorizationContext) -> frozenset[str] | None:
    """See ``backend.app.tools.graph_tools._scoped_source_ids`` (Stage 11
    ND-11A). A restricted blocker's own entity never resolves through
    ``GraphRepository.neighborhood`` for an unauthorized/unselected caller
    (it is excluded at the node-resolution step, not redacted afterward), so
    a hidden blocker is never revealed merely by participating in another
    issue's chain."""
    governance_scope = authorized_source_ids_for(context.principal)
    selection_scope = frozenset(context.principal.source_ids) if context.principal.source_ids else None
    if governance_scope is None:
        return selection_scope
    if selection_scope is None:
        return governance_scope
    return governance_scope & selection_scope


def _evidence_ref(rel: Relationship) -> str | None:
    if rel.source_id_ref and rel.record_id:
        return f"{rel.source_id_ref}:{rel.record_id}"
    return None


def _team_of(repo: GraphRepository, issue_id: str, limit: int) -> str | None:
    """The issue's authorized, evidence-backed owning team, if any -- never
    guessed from a restricted or unresolved edge."""
    page = repo.neighborhood(issue_id, limit=limit)
    for rel in page.relationships:
        if rel.source_id != issue_id or rel.type != "ASSIGNED_TO" or not rel.active:
            continue
        target = next((node for node in page.nodes if node.id == rel.target_id), None)
        if target is not None and target.type == EntityType.TEAM:
            return target.id
    return None


def _blocker_status(blocker_entity: Entity, rel: Relationship) -> BlockerStatus:
    if not rel.active:
        return BlockerStatus.HISTORICAL
    status_text = (blocker_entity.description or "").strip().upper()
    return BlockerStatus.RESOLVED if status_text in _RESOLVED_STATUS_VALUES else BlockerStatus.UNRESOLVED


def _trace_blocker_chain(repo: GraphRepository, issue_id: str, max_depth: int, limit: int,
                         include_history: bool) -> tuple[list[str], list[BlockerFinding]]:
    """ND-12B: bounded, cycle-safe reverse-BLOCKS traversal, following the
    same discipline as ``pr_impact_tools._propagate_impact``."""
    direct_blocker_ids: list[str] = []
    findings: list[BlockerFinding] = []
    visited: set[str] = {issue_id}
    frontier: list[tuple[str, list[str], int]] = [(issue_id, [issue_id], 1)]
    target_team = _team_of(repo, issue_id, limit)

    while frontier and len(findings) < limit:
        current_id, path_ids, depth = frontier.pop(0)
        page = repo.blockers_of(current_id, limit=limit, include_history=include_history)
        for rel in page.relationships:
            if rel.type != "BLOCKS" or rel.target_id != current_id:
                continue
            blocker_id = rel.source_id
            if blocker_id in visited:
                continue  # cycle guard: never re-expand an already-visited node
            visited.add(blocker_id)
            blocker_entity = next((node for node in page.nodes if node.id == blocker_id), None)
            if blocker_entity is None:
                continue
            if depth == 1:
                direct_blocker_ids.append(blocker_id)

            status = _blocker_status(blocker_entity, rel)
            blocker_team = _team_of(repo, blocker_id, limit)
            cross_team = (blocker_team != target_team) if (blocker_team and target_team) else None
            new_path = path_ids + [blocker_id]
            evidence_ref = _evidence_ref(rel)

            if status == BlockerStatus.HISTORICAL:
                explanation = (f"{blocker_id} previously blocked {current_id} (BLOCKS), but that relationship "
                              f"is no longer current -- retracted or superseded in a later revision.")
                uncertainty = "This blocker is historical only; it does not affect the current unresolved chain."
                suggested = f"No action needed for {blocker_id}; confirm {current_id}'s current blockers separately."
            elif status == BlockerStatus.RESOLVED:
                explanation = (f"{blocker_id} has a BLOCKS edge on {current_id}, but {blocker_id}'s own tracked "
                              f"status is resolved ({blocker_entity.description}); it is structurally still a "
                              f"declared blocker but no longer an active obstacle.")
                uncertainty = "Resolution is based on this issue's own tracked status, not a re-verified handoff."
                suggested = f"Confirm {blocker_id}'s resolution was fully delivered before treating {current_id} as unblocked."
            else:
                explanation = (f"{blocker_id} carries an active BLOCKS edge on {current_id} and is not tracked as "
                              f"resolved; {current_id} cannot proceed until {blocker_id} is addressed.")
                uncertainty = ("This records a declared engineering blocker only; it is not evidence of a "
                              "runtime dependency or deployment blocker unless a separate relationship "
                              "establishes that.")
                suggested = f"Prioritize or escalate {blocker_id}; it is an unresolved upstream blocker of {current_id}."

            findings.append(BlockerFinding(
                blocking_issue=blocker_id, blocking_issue_status=blocker_entity.description or None,
                blocked_issue=current_id, chain_path=list(reversed(new_path)), chain_length=depth,
                status=status, cross_team=cross_team,
                evidence_refs=tuple(ref for ref in (evidence_ref,) if ref),
                source_revisions=(rel.record_id,), explanation=explanation, uncertainty=uncertainty,
                suggested_verification=suggested,
            ))
            if len(findings) >= limit:
                break
            if depth < max_depth:
                frontier.append((blocker_id, new_path, depth + 1))

    return direct_blocker_ids, findings


def _work_blocker_chain_handler(context: AuthorizationContext, input_model: WorkBlockerChainInput,
                                dependencies: ToolDependencies) -> dict | None:
    repo = _repository(context, dependencies)
    issue_entity = repo.get_entity(input_model.issue_id)
    if issue_entity is None:
        return None

    direct_blocker_ids, findings = _trace_blocker_chain(
        repo, input_model.issue_id, input_model.max_depth, input_model.limit, input_model.include_history)

    unresolved_ids = sorted({f.blocking_issue for f in findings if f.status == BlockerStatus.UNRESOLVED})
    evidence_refs = sorted({ref for finding in findings for ref in finding.evidence_refs})
    truncated = len(findings) >= input_model.limit

    return {
        "issue_id": input_model.issue_id,
        "direct_blocker_ids": direct_blocker_ids,
        "findings": findings,
        "unresolved_blocker_ids": unresolved_ids,
        "evidence_refs": tuple(evidence_refs),
        "graph_revision": repo.graph_revision(),
        "truncated": truncated,
    }


def register_work_blocker_tools(registry: ToolRegistry) -> None:
    registry.register(ToolSpec(
        tool_id=WORK_BLOCKER_CHAIN_TOOL_ID,
        version=WORK_BLOCKER_CHAIN_VERSION,
        description="Deterministic engineering-issue blocker chain: direct and transitive BLOCKS predecessors "
                    "of an issue, distinguishing unresolved/resolved/historical blockers, cross-team "
                    "involvement where evidence supports it, with evidence refs and explicit uncertainty.",
        input_model=WorkBlockerChainInput,
        output_model=WorkBlockerChainOutput,
        handler=_work_blocker_chain_handler,
        required_permissions=frozenset({Permission.READ_GRAPH, Permission.READ_EVIDENCE}),
        side_effect=SideEffect.READ_ONLY,
        limits=ExecutionLimits(max_input_bytes=1024, max_output_bytes=262144,
                               max_result_count=100, timeout_seconds=15.0),
        capabilities=frozenset({"graph.read", "evidence.read"}),
        dependencies=frozenset({"graph_store"}),
    ))
