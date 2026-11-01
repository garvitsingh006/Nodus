"""ND-12D: deterministic, evidence-based engineering expert finder (E02).

Reuses the exact discipline established by ND-12A/B/C: no new graph engine,
no fabricated confidence/score, no model-asserted expertise. All candidates
come from edges the loader already projects from real NEXUS source records:

* ``OWNS`` (PERSON -> SERVICE, from ``service_owner_assignments.json`` and
  ``services.json``'s primary/secondary engineering owner fields) -- formal
  ownership.
* ``AUTHORED``/``REVIEWED`` on a PULL_REQUEST that reaches the target
  (directly via its declared ``REFERENCES`` to the resource, or via its
  ``MODIFIES`` repository's own ``PART_OF`` service) -- contribution/review.
* ``ASSIGNED_TO`` on an engineering issue that ``REFERENCES`` the target
  (ND-12D projects ``related_service_ids``, previously dropped) --
  contribution.
* ``AUTHORED``/``REVIEWED`` on an architecture decision (DOCUMENT) that
  ``REFERENCES`` the target -- architecture-level contribution/review.

No incident-remediation signal exists in the current NEXUS dataset (no
incident record carries a remediator field), so that evidence type is never
fabricated here -- it is simply absent when unsupported.

Classification is a plain priority rule over distinct, deduplicated evidence
(never an opaque numeric score or a fabricated confidence percentage):
FORMAL_OWNER (any OWNS edge) > POTENTIAL_SME (contributor and reviewer
evidence, or multiple distinct contributions) > CONTRIBUTOR > REVIEWER.
Evidence is deduplicated by (employee, source_id, record_id) so the same
underlying source record reached via two graph paths counts once.
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

PEOPLE_FIND_EXPERTS_TOOL_ID = "people.find_experts"
PEOPLE_FIND_EXPERTS_VERSION = "1.0.0"

_CONTRIBUTOR_ROLES = frozenset({"AUTHORED_PR", "ASSIGNED_ISSUE", "AUTHORED_DECISION"})
_REVIEWER_ROLES = frozenset({"REVIEWED_PR", "REVIEWED_DECISION"})


class ExpertClassification(StrEnum):
    FORMAL_OWNER = "FORMAL_OWNER"
    POTENTIAL_SME = "POTENTIAL_SME"
    CONTRIBUTOR = "CONTRIBUTOR"
    REVIEWER = "REVIEWER"


class ExpertEvidenceItem(BaseModel):
    role: str
    resource_id: str
    record_id: str | None
    evidence_refs: tuple[str, ...]


class ExpertCandidate(BaseModel):
    employee_id: str
    name: str | None
    classification: ExpertClassification
    formal_owner: bool
    distinct_evidence_count: int
    evidence: list[ExpertEvidenceItem]
    explanation: str
    uncertainty: str | None


class PeopleFindExpertsInput(BaseModel):
    target_id: str = Field(min_length=1, max_length=500)
    limit: int = Field(default=10, ge=1, le=50)
    evidence_limit: int = Field(default=50, ge=1, le=100)


class PeopleFindExpertsOutput(BaseModel):
    target_id: str
    candidates: list[ExpertCandidate]
    insufficient_evidence: bool
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
    ND-11A). A restricted contribution's PR/issue/decision entity never
    resolves through ``GraphRepository.neighborhood`` for an unauthorized
    caller (excluded at node-resolution, not redacted afterward), so a
    hidden contributor's identity, counts and paths never surface."""
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


def _node(nodes: list[Entity], entity_id: str) -> Entity | None:
    return next((node for node in nodes if node.id == entity_id), None)


def _resource_scope(repo: GraphRepository, target_id: str, limit: int) -> set[str]:
    """Target plus, when the target is a REPOSITORY or CONTRACT, the
    SERVICE(s) it resolves to (same pattern as ``pr_impact_tools``'s change
    scope) -- a service is where ownership/REFERENCES evidence actually
    lives, so a repository/contract target is expanded to reach it."""
    target = repo.get_entity(target_id)
    scope = {target_id}
    if target is None:
        return scope
    page = repo.neighborhood(target_id, limit=limit)
    if target.type == EntityType.REPOSITORY:
        for rel in page.relationships:
            if rel.source_id == target_id and rel.type == "PART_OF" and rel.active:
                other = _node(page.nodes, rel.target_id)
                if other is not None and other.type == EntityType.SERVICE:
                    scope.add(other.id)
    elif target.type == EntityType.CONTRACT:
        for rel in page.relationships:
            if rel.target_id == target_id and rel.type in ("PROVIDES", "CONSUMES") and rel.active:
                other = _node(page.nodes, rel.source_id)
                if other is not None and other.type == EntityType.SERVICE:
                    scope.add(other.id)
    return scope


def _collect_resources(repo: GraphRepository, scope: set[str], limit: int) -> dict[str, set[str]]:
    """Resources reaching the scope: repositories PART_OF a scoped service,
    pull requests/issues/decisions that REFERENCES a scoped service or
    MODIFIES a scoped repository. Returns {"repositories": {...},
    "pull_requests": {...}, "issues": {...}, "decisions": {...},
    "owners_source": {...} (resources to check OWNS on, i.e. the scope
    itself)}."""
    repositories: set[str] = set()
    pull_requests: set[str] = set()
    issues: set[str] = set()
    decisions: set[str] = set()

    for resource_id in scope:
        page = repo.neighborhood(resource_id, limit=limit)
        for rel in page.relationships:
            if not rel.active:
                continue
            if rel.type == "PART_OF" and rel.target_id == resource_id:
                other = _node(page.nodes, rel.source_id)
                if other is not None and other.type == EntityType.REPOSITORY:
                    repositories.add(other.id)
            elif rel.type == "REFERENCES" and rel.target_id == resource_id:
                other = _node(page.nodes, rel.source_id)
                if other is None:
                    continue
                if other.type == EntityType.PULL_REQUEST:
                    pull_requests.add(other.id)
                elif other.type == EntityType.TASK:
                    issues.add(other.id)
                elif other.type == EntityType.DOCUMENT:
                    decisions.add(other.id)

    for repository_id in repositories:
        page = repo.neighborhood(repository_id, limit=limit)
        for rel in page.relationships:
            if rel.type == "MODIFIES" and rel.target_id == repository_id and rel.active:
                other = _node(page.nodes, rel.source_id)
                if other is not None and other.type == EntityType.PULL_REQUEST:
                    pull_requests.add(other.id)

    return {"repositories": repositories, "pull_requests": pull_requests, "issues": issues, "decisions": decisions}


def _gather_evidence(repo: GraphRepository, scope: set[str], resources: dict[str, set[str]],
                     limit: int) -> dict[str, dict[tuple[str, str, str], ExpertEvidenceItem]]:
    """Per-employee evidence, deduplicated by (employee_id, source_id,
    record_id) -- the same underlying source record reached through two
    different graph paths (e.g. a PR both MODIFIES a scoped repository and
    REFERENCES a scoped service directly) is never double-counted."""
    evidence: dict[str, dict[tuple[str, str, str], ExpertEvidenceItem]] = {}

    def add(employee_id: str, role: str, resource_id: str, rel: Relationship) -> None:
        ref = _evidence_ref(rel)
        key = (employee_id, ref or "", rel.record_id or rel.id)
        bucket = evidence.setdefault(employee_id, {})
        if key in bucket:
            return
        bucket[key] = ExpertEvidenceItem(role=role, resource_id=resource_id, record_id=rel.record_id,
                                         evidence_refs=tuple(r for r in (ref,) if r))

    for resource_id in scope:
        page = repo.neighborhood(resource_id, limit=limit)
        for rel in page.relationships:
            if rel.type == "OWNS" and rel.target_id == resource_id and rel.active:
                other = _node(page.nodes, rel.source_id)
                if other is not None and other.type == EntityType.PERSON:
                    add(other.id, "OWNS", resource_id, rel)

    for pr_id in resources["pull_requests"]:
        page = repo.neighborhood(pr_id, limit=limit)
        for rel in page.relationships:
            if not rel.active:
                continue
            other = _node(page.nodes, rel.source_id)
            if other is None or other.type != EntityType.PERSON:
                continue
            if rel.type == "AUTHORED" and rel.target_id == pr_id:
                add(other.id, "AUTHORED_PR", pr_id, rel)
            elif rel.type == "REVIEWED" and rel.target_id == pr_id:
                add(other.id, "REVIEWED_PR", pr_id, rel)

    for issue_id in resources["issues"]:
        page = repo.neighborhood(issue_id, limit=limit)
        for rel in page.relationships:
            if not rel.active or rel.type != "ASSIGNED_TO" or rel.source_id != issue_id:
                continue
            other = _node(page.nodes, rel.target_id)
            if other is not None and other.type == EntityType.PERSON:
                add(other.id, "ASSIGNED_ISSUE", issue_id, rel)

    for decision_id in resources["decisions"]:
        page = repo.neighborhood(decision_id, limit=limit)
        for rel in page.relationships:
            if not rel.active:
                continue
            other = _node(page.nodes, rel.source_id)
            if other is None or other.type != EntityType.PERSON:
                continue
            if rel.type == "AUTHORED" and rel.target_id == decision_id:
                add(other.id, "AUTHORED_DECISION", decision_id, rel)
            elif rel.type == "REVIEWED" and rel.target_id == decision_id:
                add(other.id, "REVIEWED_DECISION", decision_id, rel)

    return evidence


def _classify(items: list[ExpertEvidenceItem]) -> ExpertClassification:
    has_owner = any(item.role == "OWNS" for item in items)
    if has_owner:
        return ExpertClassification.FORMAL_OWNER
    contributor_count = sum(1 for item in items if item.role in _CONTRIBUTOR_ROLES)
    reviewer_count = sum(1 for item in items if item.role in _REVIEWER_ROLES)
    if (contributor_count and reviewer_count) or contributor_count >= 2:
        return ExpertClassification.POTENTIAL_SME
    if contributor_count:
        return ExpertClassification.CONTRIBUTOR
    return ExpertClassification.REVIEWER


def _explain(employee_id: str, target_id: str, classification: ExpertClassification,
            items: list[ExpertEvidenceItem]) -> tuple[str, str | None]:
    role_counts: dict[str, int] = {}
    for item in items:
        role_counts[item.role] = role_counts.get(item.role, 0) + 1
    parts = []
    if role_counts.get("OWNS"):
        parts.append(f"{role_counts['OWNS']} formal OWNS record(s)")
    if role_counts.get("AUTHORED_PR"):
        parts.append(f"authored {role_counts['AUTHORED_PR']} relevant pull request(s)")
    if role_counts.get("REVIEWED_PR"):
        parts.append(f"reviewed {role_counts['REVIEWED_PR']} relevant pull request(s)")
    if role_counts.get("ASSIGNED_ISSUE"):
        parts.append(f"assigned to {role_counts['ASSIGNED_ISSUE']} relevant issue(s)")
    if role_counts.get("AUTHORED_DECISION"):
        parts.append(f"authored {role_counts['AUTHORED_DECISION']} relevant architecture decision(s)")
    if role_counts.get("REVIEWED_DECISION"):
        parts.append(f"reviewed {role_counts['REVIEWED_DECISION']} relevant architecture decision(s)")
    explanation = f"{employee_id} relative to {target_id}: " + "; ".join(parts) + "."
    uncertainty = None
    if classification == ExpertClassification.CONTRIBUTOR:
        uncertainty = "Single contribution signal only; not a declared formal owner."
    elif classification == ExpertClassification.REVIEWER:
        uncertainty = "Review evidence only; no authored contribution or formal ownership found."
    return explanation, uncertainty


def _find_experts_handler(context: AuthorizationContext, input_model: PeopleFindExpertsInput,
                          dependencies: ToolDependencies) -> dict | None:
    repo = _repository(context, dependencies)
    target = repo.get_entity(input_model.target_id)
    if target is None:
        return None

    scope = _resource_scope(repo, input_model.target_id, input_model.evidence_limit)
    resources = _collect_resources(repo, scope, input_model.evidence_limit)
    evidence_by_employee = _gather_evidence(repo, scope, resources, input_model.evidence_limit)

    candidates: list[ExpertCandidate] = []
    for employee_id, bucket in evidence_by_employee.items():
        items = list(bucket.values())
        classification = _classify(items)
        explanation, uncertainty = _explain(employee_id, input_model.target_id, classification, items)
        employee_entity = repo.get_entity(employee_id)
        candidates.append(ExpertCandidate(
            employee_id=employee_id, name=employee_entity.name if employee_entity else None,
            classification=classification, formal_owner=classification == ExpertClassification.FORMAL_OWNER,
            distinct_evidence_count=len(items), evidence=items, explanation=explanation, uncertainty=uncertainty,
        ))

    priority = {ExpertClassification.FORMAL_OWNER: 0, ExpertClassification.POTENTIAL_SME: 1,
               ExpertClassification.CONTRIBUTOR: 2, ExpertClassification.REVIEWER: 3}
    candidates.sort(key=lambda c: (priority[c.classification], -c.distinct_evidence_count, c.employee_id))
    truncated = len(candidates) > input_model.limit
    candidates = candidates[:input_model.limit]

    evidence_refs = sorted({ref for candidate in candidates for item in candidate.evidence
                            for ref in item.evidence_refs})

    return {
        "target_id": input_model.target_id,
        "candidates": candidates,
        "insufficient_evidence": not candidates,
        "evidence_refs": tuple(evidence_refs),
        "graph_revision": repo.graph_revision(),
        "truncated": truncated,
    }


def register_people_expert_tools(registry: ToolRegistry) -> None:
    registry.register(ToolSpec(
        tool_id=PEOPLE_FIND_EXPERTS_TOOL_ID,
        version=PEOPLE_FIND_EXPERTS_VERSION,
        description="Deterministic engineering expert finder: formal owners, contributors, reviewers and "
                    "potential subject-matter experts for a service/repository/interface, from real "
                    "source-backed evidence only -- never an opaque score or fabricated expertise.",
        input_model=PeopleFindExpertsInput,
        output_model=PeopleFindExpertsOutput,
        handler=_find_experts_handler,
        required_permissions=frozenset({Permission.READ_GRAPH, Permission.READ_EVIDENCE}),
        side_effect=SideEffect.READ_ONLY,
        limits=ExecutionLimits(max_input_bytes=1024, max_output_bytes=262144,
                               max_result_count=50, timeout_seconds=15.0),
        capabilities=frozenset({"graph.read", "evidence.read"}),
        dependencies=frozenset({"graph_store"}),
    ))
