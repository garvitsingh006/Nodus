"""ND-12C: deterministic, evidence-based architecture decision genealogy
(E14).

Reuses the exact discipline established by ND-12A/B (``pr_impact_tools``,
``work_blocker_tools``): no new graph engine, no new authoritative "effects"
store, and no model-asserted conclusions. The only new graph primitive is
the narrowly scoped ``SUPERSEDES`` relation type (DOCUMENT -> DOCUMENT,
source-asserted/observed only, never inferred) -- added because no existing
relation type can correctly express "this decision formally replaces that
one" (``REFERENCES`` deliberately disclaims structural/causal meaning;
``RELATED_TO`` is the untyped fallback reserved for connections a connector
cannot yet characterize, which a supersession is not).

An architecture decision's rich fields (rationale, alternatives considered,
context, selected decision, expected consequences) have no structured home
in the current ``Entity`` model (name/description only). Rather than
broadening that model, the loader (``backend.app.governance.loader``)
already preserves them verbatim as a JSON payload in the DOCUMENT entity's
otherwise-unused ``description``; this tool reads that payload back
structurally. A decision whose description is absent, non-JSON, or nulled
out by a genuine conflicting claim (``GraphRepository._resolve``) is
reported as having no structured rationale available -- never guessed at.

Genealogy is traced only through explicit ``SUPERSEDES`` edges; chronology
(``decision_date``) is reported as part of the timeline, never used to
infer a predecessor/successor relationship on its own.
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

DECISION_GENEALOGY_TOOL_ID = "decision.genealogy"
DECISION_GENEALOGY_VERSION = "1.0.0"

MAX_GENEALOGY_DEPTH = 5


class GenealogyRelation(StrEnum):
    PREDECESSOR = "PREDECESSOR"  # an earlier decision this one supersedes
    SUCCESSOR = "SUCCESSOR"      # a later decision that supersedes this one


class GenealogyStatus(StrEnum):
    CURRENT = "CURRENT"
    HISTORICAL = "HISTORICAL"  # the SUPERSEDES edge itself is no longer current


class RelatedResource(BaseModel):
    entity_id: str
    type: EntityType | None
    name: str | None
    relation_type: str
    evidence_refs: tuple[str, ...]


class GenealogyLink(BaseModel):
    decision_id: str
    relation: GenealogyRelation
    status: GenealogyStatus
    chain_path: list[str]
    chain_length: int
    evidence_refs: tuple[str, ...]
    source_revisions: tuple[str | None, ...]
    explanation: str


class DecisionGenealogyInput(BaseModel):
    decision_id: str = Field(min_length=1, max_length=500)
    max_depth: int = Field(default=3, ge=1, le=MAX_GENEALOGY_DEPTH)
    limit: int = Field(default=25, ge=1, le=100)


class DecisionGenealogyOutput(BaseModel):
    decision_id: str
    title: str | None
    rationale: str | None
    alternatives_considered: list | None
    context: str | None
    selected_decision: str | None
    expected_consequences: list | None
    decision_status: str | None
    decision_date: str | None
    structured_fields_available: bool
    uncertainty: str | None
    related_resources: list[RelatedResource]
    genealogy: list[GenealogyLink]
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
    ND-11A). A restricted decision, its rationale (carried in the same
    ACL-protected entity assertion as its name), and anything it links to
    never resolve through ``GraphRepository`` for an unauthorized caller --
    nothing is fetched first and redacted afterward."""
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


def _structured_fields(entity: Entity) -> tuple[dict | None, str | None]:
    """Decodes the loader's JSON payload from the entity's description.
    Returns (fields, uncertainty) -- fields is None, with an explanatory
    uncertainty string, whenever nothing reliable can be decoded (missing,
    conflicting, or not JSON -- never guessed at)."""
    if not entity.description:
        if "description" in entity.conflicts:
            return None, ("This decision's structured rationale is unavailable: conflicting, unreconciled "
                          "claims exist across sources for its description field.")
        return None, "No structured rationale/alternatives payload was found for this decision."
    try:
        payload = json.loads(entity.description)
    except (TypeError, ValueError):
        return None, "This decision's description is not a structured payload; only its name is available."
    if not isinstance(payload, dict):
        return None, "This decision's description did not decode to a structured record."
    return payload, None


def _related_resources(repo: GraphRepository, decision_id: str, limit: int) -> list[RelatedResource]:
    """ND-12C-2/5: evidence-backed related engineering resources only --
    whatever REFERENCES/RELATED_TO/AUTHORED/REVIEWED edges the decision's
    own one-hop neighborhood actually carries, nothing inferred from text
    similarity."""
    page = repo.neighborhood(decision_id, limit=limit)
    related: list[RelatedResource] = []
    for rel in page.relationships:
        if rel.type not in ("REFERENCES", "RELATED_TO", "AUTHORED", "REVIEWED") or not rel.active:
            continue
        other_id = rel.target_id if rel.source_id == decision_id else rel.source_id
        if other_id == decision_id:
            continue
        other = next((node for node in page.nodes if node.id == other_id), None)
        if other is None:
            continue
        evidence_ref = _evidence_ref(rel)
        related.append(RelatedResource(
            entity_id=other.id, type=other.type, name=other.name, relation_type=rel.type,
            evidence_refs=tuple(ref for ref in (evidence_ref,) if ref),
        ))
    return related


def _trace_genealogy(repo: GraphRepository, decision_id: str, max_depth: int, limit: int) -> list[GenealogyLink]:
    """ND-12C-3/4: bounded, cycle-safe traversal of explicit SUPERSEDES
    edges only, both directions (predecessors this decision supersedes, and
    successors that supersede it), following the same discipline as
    ``pr_impact_tools``/``work_blocker_tools``."""
    links: list[GenealogyLink] = []
    visited: set[str] = {decision_id}

    def walk(relation: GenealogyRelation) -> None:
        frontier: list[tuple[str, list[str], int]] = [(decision_id, [decision_id], 1)]
        local_visited = set(visited)
        while frontier and len(links) < limit:
            current_id, path_ids, depth = frontier.pop(0)
            page = repo.neighborhood(current_id, limit=limit, include_history=True)
            for rel in page.relationships:
                if rel.type != "SUPERSEDES":
                    continue
                if relation == GenealogyRelation.PREDECESSOR and rel.source_id != current_id:
                    continue  # current_id SUPERSEDES other -> other is a predecessor
                if relation == GenealogyRelation.SUCCESSOR and rel.target_id != current_id:
                    continue  # other SUPERSEDES current_id -> other is a successor
                other_id = rel.target_id if relation == GenealogyRelation.PREDECESSOR else rel.source_id
                if other_id in local_visited:
                    continue  # cycle guard
                local_visited.add(other_id)
                new_path = path_ids + [other_id]
                status = GenealogyStatus.CURRENT if rel.active else GenealogyStatus.HISTORICAL
                evidence_ref = _evidence_ref(rel)
                verb = "supersedes" if relation == GenealogyRelation.PREDECESSOR else "is superseded by"
                links.append(GenealogyLink(
                    decision_id=other_id, relation=relation, status=status, chain_path=new_path,
                    chain_length=depth, evidence_refs=tuple(ref for ref in (evidence_ref,) if ref),
                    source_revisions=(rel.record_id,),
                    explanation=(f"{current_id} {verb} {other_id} "
                                f"({'a currently declared' if rel.active else 'a historical, no-longer-current'} "
                                f"SUPERSEDES relationship)."),
                ))
                if len(links) >= limit:
                    return
                if depth < max_depth:
                    frontier.append((other_id, new_path, depth + 1))

    walk(GenealogyRelation.PREDECESSOR)
    walk(GenealogyRelation.SUCCESSOR)
    return links


def _decision_genealogy_handler(context: AuthorizationContext, input_model: DecisionGenealogyInput,
                                dependencies: ToolDependencies) -> dict | None:
    repo = _repository(context, dependencies)
    decision_entity = repo.get_entity(input_model.decision_id)
    if decision_entity is None:
        return None

    fields, uncertainty = _structured_fields(decision_entity)
    related = _related_resources(repo, input_model.decision_id, input_model.limit)
    genealogy = _trace_genealogy(repo, input_model.decision_id, input_model.max_depth, input_model.limit)

    evidence_refs = sorted({ref for item in related for ref in item.evidence_refs}
                           | {ref for link in genealogy for ref in link.evidence_refs})
    truncated = len(genealogy) >= input_model.limit or len(related) >= input_model.limit

    return {
        "decision_id": input_model.decision_id,
        "title": decision_entity.name,
        "rationale": fields.get("rationale") if fields else None,
        "alternatives_considered": fields.get("alternatives_considered") if fields else None,
        "context": fields.get("context") if fields else None,
        "selected_decision": fields.get("selected_decision") if fields else None,
        "expected_consequences": fields.get("expected_consequences") if fields else None,
        "decision_status": fields.get("decision_status") if fields else None,
        "decision_date": fields.get("decision_date") if fields else None,
        "structured_fields_available": fields is not None,
        "uncertainty": uncertainty,
        "related_resources": related,
        "genealogy": genealogy,
        "evidence_refs": tuple(evidence_refs),
        "graph_revision": repo.graph_revision(),
        "truncated": truncated,
    }


def register_decision_genealogy_tools(registry: ToolRegistry) -> None:
    registry.register(ToolSpec(
        tool_id=DECISION_GENEALOGY_TOOL_ID,
        version=DECISION_GENEALOGY_VERSION,
        description="Deterministic architecture decision genealogy: documented rationale/alternatives, "
                    "evidence-backed related resources, and explicit predecessor/successor SUPERSEDES chains "
                    "-- never a fabricated or chronology-inferred genealogy.",
        input_model=DecisionGenealogyInput,
        output_model=DecisionGenealogyOutput,
        handler=_decision_genealogy_handler,
        required_permissions=frozenset({Permission.READ_GRAPH, Permission.READ_EVIDENCE}),
        side_effect=SideEffect.READ_ONLY,
        limits=ExecutionLimits(max_input_bytes=1024, max_output_bytes=262144,
                               max_result_count=100, timeout_seconds=15.0),
        capabilities=frozenset({"graph.read", "evidence.read"}),
        dependencies=frozenset({"graph_store"}),
    ))
