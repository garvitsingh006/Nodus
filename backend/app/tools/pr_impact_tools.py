"""ND-12A: deterministic, evidence-based PR change-impact evaluation.

Reuses the existing ``GraphRepository`` traversal (``neighborhood``/
``reverse_dependencies``), the already-typed MODIFIES/PART_OF/REFERENCES/
DEPENDS_ON edges, and the same authorized-source-scoping pattern as
``graph_tools``/``evidence_tools`` (``_scoped_source_ids``). There is no new
graph write path and no second "effects" store: a finding is only ever as
current as the ``GraphRepository`` reads behind it, so a retracted or changed
dependency disappears from the next call's result automatically, the same way
``graph.reverse_dependencies`` already behaves (see module docstring on
``backend.app.graph.repository.GraphRepository.ingest`` for the active-flag
mechanism this relies on).

Change scope (ND-12A-1) is resolved from two source-backed paths:
* declared -- the PR's own ``REFERENCES`` edge to a service/contract it
  claims is affected (a source assertion, not proof);
* derived -- the PR's ``MODIFIES`` edge to a repository, followed by that
  repository's own ``PART_OF`` edge to the service it builds.
A changed file that resolves to neither is reported as an explicit
unmapped/uncertain file (ND-12A-1's "if the changed file cannot be reliably
associated with a service ... return an explicit uncertainty"), never
silently dropped.

Impact propagation (ND-12A-2/3) walks only ``DEPENDS_ON`` reverse edges
(``GraphRepository.reverse_dependencies``, already direction-correct:
consumer -DEPENDS_ON-> provider, so the reverse walk finds consumers of a
changed service) up to ``max_depth`` hops, with a visited set preventing
cycles and ``limit`` bounding fan-out/results. A finding never claims a
confirmed incompatibility -- only that a typed, evidence-backed dependency
path exists, which establishes potential exposure requiring verification.
"""
from __future__ import annotations

import json
from enum import StrEnum

from pydantic import BaseModel, Field

from backend.app.governance.identity import authorized_source_ids_for
from backend.app.graph.models import Entity, EntityType, Relationship
from backend.app.graph.repository import GraphRepository
from backend.app.security import AuthorizationContext, Permission
from backend.app.tools.contracts import ExecutionLimits
from backend.app.tools.registry import SideEffect, ToolDependencies, ToolRegistry, ToolSpec

PR_CHANGE_IMPACT_TOOL_ID = "pr.change_impact"
PR_CHANGE_IMPACT_VERSION = "1.0.0"

MAX_IMPACT_DEPTH = 3


class ImpactClassification(StrEnum):
    DIRECT_DEPENDENCY_POTENTIALLY_AFFECTED = "DIRECT_DEPENDENCY_POTENTIALLY_AFFECTED"
    TRANSITIVE_DEPENDENCY_POTENTIALLY_AFFECTED = "TRANSITIVE_DEPENDENCY_POTENTIALLY_AFFECTED"


class PrChangeState(StrEnum):
    """ND-12E: a deterministic distinction between a proposed change and an
    actually-deployed one. Priority order reflects what each status
    requires as evidence, not merely a self-reported flag:

    * ``PROPOSED``/``APPROVED``/``MERGED`` come directly from the PR's own
      source-asserted status fields.
    * ``DEPLOYED`` requires a real, currently-active evidence chain
      (commit -INCLUDED_IN-> PR, commit -TRIGGERED-> build, build
      -PRODUCED-> artifact, artifact -DEPLOYED_AS-> deployment) -- approval,
      merge, or a ``deployment_authorized`` flag alone are never sufficient.
    * ``UNKNOWN`` when the PR's own status fields are missing/unparseable.
    """
    PROPOSED = "PROPOSED"
    APPROVED = "APPROVED"
    MERGED = "MERGED"
    DEPLOYED = "DEPLOYED"
    UNKNOWN = "UNKNOWN"


class ChangeStateResult(BaseModel):
    state: PrChangeState
    deployment_authorized: bool | None
    deployment_evidence_refs: tuple[str, ...]
    uncertainty: str | None


class ChangedResource(BaseModel):
    entity_id: str
    type: EntityType | None
    name: str | None
    via: str = Field(description="'declared' (PR REFERENCES service/contract) or "
                                 "'modifies_repository' (PR MODIFIES repo PART_OF service)")


class ImpactFinding(BaseModel):
    changed_resource: str
    affected_resource: str
    relationship_path: list[str]
    edge_types: list[str]
    evidence_refs: tuple[str, ...]
    source_revisions: tuple[str | None, ...]
    classification: ImpactClassification
    explanation: str
    uncertainty: str
    suggested_verification: str


class PrChangeImpactInput(BaseModel):
    pr_id: str = Field(min_length=1, max_length=500)
    max_depth: int = Field(default=2, ge=1, le=MAX_IMPACT_DEPTH)
    limit: int = Field(default=25, ge=1, le=100)


class PrChangeImpactOutput(BaseModel):
    pr_id: str
    change_state: PrChangeState
    deployment_authorized: bool | None
    deployment_evidence_refs: tuple[str, ...]
    change_state_uncertainty: str | None
    changed_resources: list[ChangedResource]
    findings: list[ImpactFinding]
    unmapped_changed_files: list[str]
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
    ND-11A). Unselected/unauthorized sources never reach a finding: the
    restriction is enforced inside every ``GraphRepository`` Cypher read this
    module calls, never applied after the fact by filtering returned data."""
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


def _node(page_nodes: list[Entity], entity_id: str) -> Entity | None:
    return next((node for node in page_nodes if node.id == entity_id), None)


def _changed_resources(repo: GraphRepository, pr_id: str, limit: int,
                       ) -> tuple[list[ChangedResource], list[str], list[Relationship]]:
    """ND-12A-1: resolve change scope from the PR's own one-hop neighborhood.
    Returns (changed resources, unmapped changed-file names, scope-evidence
    relationships)."""
    page = repo.neighborhood(pr_id, limit=limit)
    changed: dict[str, ChangedResource] = {}
    unmapped_files: list[str] = []
    scope_rels: list[Relationship] = []
    repos_touched: list[str] = []

    for rel in page.relationships:
        if rel.source_id != pr_id or not rel.active:
            continue
        target = _node(page.nodes, rel.target_id)
        if target is None:
            continue
        if rel.type == "REFERENCES" and target.type in (EntityType.SERVICE, EntityType.CONTRACT):
            changed.setdefault(target.id, ChangedResource(
                entity_id=target.id, type=target.type, name=target.name, via="declared"))
            scope_rels.append(rel)
        elif rel.type == "MODIFIES" and target.type == EntityType.REPOSITORY:
            repos_touched.append(target.id)
            scope_rels.append(rel)
        elif rel.type == "MODIFIES" and target.type == EntityType.FILE:
            unmapped_files.append(target.name or target.id)
            scope_rels.append(rel)

    for repository_id in repos_touched:
        repo_page = repo.neighborhood(repository_id, limit=limit)
        for rel in repo_page.relationships:
            if rel.source_id != repository_id or rel.type != "PART_OF" or not rel.active:
                continue
            target = _node(repo_page.nodes, rel.target_id)
            if target is None or target.type != EntityType.SERVICE:
                continue
            changed.setdefault(target.id, ChangedResource(
                entity_id=target.id, type=target.type, name=target.name, via="modifies_repository"))
            scope_rels.append(rel)

    return list(changed.values()), sorted(set(unmapped_files)), scope_rels


def _propagate_impact(repo: GraphRepository, changed_resources: list[ChangedResource],
                      max_depth: int, limit: int) -> list[ImpactFinding]:
    """ND-12A-2/3: bounded reverse-DEPENDS_ON propagation from each changed
    resource. Cycle-safe (a global visited set across the whole traversal)
    and bounded by both max_depth and limit, matching the same discipline as
    ``GraphRepository.paths``."""
    findings: list[ImpactFinding] = []
    visited: set[str] = {resource.entity_id for resource in changed_resources}
    frontier: list[tuple[str, ChangedResource, list[str], list[str], int]] = [
        (resource.entity_id, resource, [resource.entity_id], [], 1) for resource in changed_resources
    ]

    while frontier and len(findings) < limit:
        current_id, origin, path_ids, path_types, depth = frontier.pop(0)
        page = repo.reverse_dependencies(current_id, limit=limit)
        for rel in page.relationships:
            if rel.type != "DEPENDS_ON" or rel.target_id != current_id or not rel.active:
                continue
            consumer_id = rel.source_id
            if consumer_id in visited:
                continue  # cycle guard: never re-expand an already-visited node
            visited.add(consumer_id)
            new_path_ids = path_ids + [consumer_id]
            new_path_types = path_types + ["DEPENDS_ON"]
            evidence_ref = _evidence_ref(rel)
            classification = (ImpactClassification.DIRECT_DEPENDENCY_POTENTIALLY_AFFECTED if depth == 1
                              else ImpactClassification.TRANSITIVE_DEPENDENCY_POTENTIALLY_AFFECTED)
            findings.append(ImpactFinding(
                changed_resource=origin.entity_id,
                affected_resource=consumer_id,
                relationship_path=new_path_ids,
                edge_types=new_path_types,
                evidence_refs=tuple(ref for ref in (evidence_ref,) if ref),
                source_revisions=(rel.record_id,),
                classification=classification,
                explanation=(
                    f"{consumer_id} carries a {'direct' if depth == 1 else f'transitive (depth {depth})'} "
                    f"DEPENDS_ON edge on {origin.entity_id}, which this PR changes ({origin.via.replace('_', ' ')}). "
                    f"This establishes potential exposure only; it is not proof that the PR's specific change "
                    f"breaks {consumer_id}'s compatibility with {origin.entity_id}."),
                uncertainty=(
                    "A dependency path was found; whether the PR's actual code change is incompatible with this "
                    "consumer's usage is not established by graph traversal alone."),
                suggested_verification=(
                    f"Run {consumer_id}'s integration/contract tests against this PR's change to "
                    f"{origin.entity_id} before deployment."),
            ))
            if len(findings) >= limit:
                break
            if depth < max_depth:
                frontier.append((consumer_id, origin, new_path_ids, new_path_types, depth + 1))

    return findings


def _declared_fields(pr_entity: Entity) -> dict | None:
    if not pr_entity.description:
        return None
    try:
        payload = json.loads(pr_entity.description)
    except (TypeError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _deployment_chain_evidence(repo: GraphRepository, pr_id: str, limit: int) -> tuple[str, ...]:
    """ND-12E: walks the real commit -> build -> artifact -> deployment
    chain from the PR's own included commits. Only currently-active edges
    at every hop count -- a retracted/superseded link anywhere in the chain
    means this PR is not currently evidenced as deployed, regardless of its
    own merge/approval status or a ``deployment_authorized`` flag."""
    evidence: list[str] = []
    pr_page = repo.neighborhood(pr_id, limit=limit)
    commit_ids = [rel.source_id for rel in pr_page.relationships
                 if rel.type == "INCLUDED_IN" and rel.target_id == pr_id and rel.active]

    for commit_id in commit_ids:
        commit_page = repo.neighborhood(commit_id, limit=limit)
        build_ids = [rel.target_id for rel in commit_page.relationships
                    if rel.type == "TRIGGERED" and rel.source_id == commit_id and rel.active]
        for build_id in build_ids:
            build_page = repo.neighborhood(build_id, limit=limit)
            artifact_ids = [rel.target_id for rel in build_page.relationships
                           if rel.type == "PRODUCED" and rel.source_id == build_id and rel.active]
            for artifact_id in artifact_ids:
                artifact_page = repo.neighborhood(artifact_id, limit=limit)
                deployment_rels = [rel for rel in artifact_page.relationships
                                   if rel.type == "DEPLOYED_AS" and rel.source_id == artifact_id and rel.active]
                for rel in deployment_rels:
                    ref = _evidence_ref(rel)
                    if ref:
                        evidence.append(ref)
    return tuple(sorted(set(evidence)))


def _resolve_change_state(repo: GraphRepository, pr_entity: Entity, pr_id: str, limit: int) -> ChangeStateResult:
    fields = _declared_fields(pr_entity)
    deployment_authorized = fields.get("deployment_authorized") if fields else None
    deployment_evidence = _deployment_chain_evidence(repo, pr_id, limit)

    if deployment_evidence:
        return ChangeStateResult(
            state=PrChangeState.DEPLOYED, deployment_authorized=deployment_authorized,
            deployment_evidence_refs=deployment_evidence, uncertainty=None)

    if fields is None:
        return ChangeStateResult(
            state=PrChangeState.UNKNOWN, deployment_authorized=deployment_authorized,
            deployment_evidence_refs=(), uncertainty="No declared status fields are available for this PR.")

    status = (fields.get("status") or "").upper()
    approval_status = (fields.get("approval_status") or "").upper()
    merge_status = (fields.get("merge_status") or "").upper()

    if status == "MERGED" or merge_status == "MERGED":
        state = PrChangeState.MERGED
        uncertainty = ("This PR is merged but no currently-active build/artifact/deployment chain was found; "
                      "deployment status is not established by merge alone.")
    elif approval_status == "APPROVED":
        state = PrChangeState.APPROVED
        uncertainty = "Approval is not evidence of merge or deployment."
    elif status == "OPEN":
        state = PrChangeState.PROPOSED
        uncertainty = "This is a proposed change only; it has not been merged or deployed."
    else:
        state = PrChangeState.UNKNOWN
        uncertainty = f"Unrecognized declared status combination (status={status or 'unknown'!r})."

    if deployment_authorized and state != PrChangeState.DEPLOYED:
        uncertainty = (uncertainty + " A deployment_authorized flag is set, but authorization is not proof of "
                      "execution -- no deployment evidence chain was found.")

    return ChangeStateResult(state=state, deployment_authorized=deployment_authorized,
                             deployment_evidence_refs=(), uncertainty=uncertainty)


def _pr_change_impact_handler(context: AuthorizationContext, input_model: PrChangeImpactInput,
                              dependencies: ToolDependencies) -> dict | None:
    repo = _repository(context, dependencies)
    pr_entity = repo.get_entity(input_model.pr_id)
    if pr_entity is None:
        return None

    change_state = _resolve_change_state(repo, pr_entity, input_model.pr_id, input_model.limit)
    changed_resources, unmapped_files, scope_rels = _changed_resources(repo, input_model.pr_id, input_model.limit)
    findings = (_propagate_impact(repo, changed_resources, input_model.max_depth, input_model.limit)
               if changed_resources else [])

    evidence_refs = sorted({ref for rel in scope_rels if (ref := _evidence_ref(rel))}
                           | {ref for finding in findings for ref in finding.evidence_refs}
                           | set(change_state.deployment_evidence_refs))
    truncated = len(findings) >= input_model.limit

    return {
        "pr_id": input_model.pr_id,
        "change_state": change_state.state,
        "deployment_authorized": change_state.deployment_authorized,
        "deployment_evidence_refs": change_state.deployment_evidence_refs,
        "change_state_uncertainty": change_state.uncertainty,
        "changed_resources": changed_resources,
        "findings": findings,
        "unmapped_changed_files": unmapped_files,
        "evidence_refs": tuple(evidence_refs),
        "graph_revision": repo.graph_revision(),
        "truncated": truncated,
    }


def register_pr_impact_tools(registry: ToolRegistry) -> None:
    registry.register(ToolSpec(
        tool_id=PR_CHANGE_IMPACT_TOOL_ID,
        version=PR_CHANGE_IMPACT_VERSION,
        description="Deterministic PR change-impact: resolves a PR's changed files/services/contracts "
                    "(declared and repository-derived) and propagates typed DEPENDS_ON edges to potentially "
                    "affected downstream consumers, with evidence and explicit uncertainty for unmapped files.",
        input_model=PrChangeImpactInput,
        output_model=PrChangeImpactOutput,
        handler=_pr_change_impact_handler,
        required_permissions=frozenset({Permission.READ_GRAPH, Permission.READ_EVIDENCE}),
        side_effect=SideEffect.READ_ONLY,
        limits=ExecutionLimits(max_input_bytes=1024, max_output_bytes=262144,
                               max_result_count=100, timeout_seconds=15.0),
        capabilities=frozenset({"graph.read", "evidence.read"}),
        dependencies=frozenset({"graph_store"}),
    ))
