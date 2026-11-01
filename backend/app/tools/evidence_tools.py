"""Evidence lookup and deterministic verification tools.

Verification never fabricates a confidence score and never promotes an
inferred/conflicting claim into a settled fact — it only surfaces the
existing, already-deterministic conflict/authority resolution
(``GraphRepository._resolve`` / ``backend.app.graph.authority``) and the raw
per-source assertion history (``GraphRepository.assertions``). No graph
mutation happens here.
"""
from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field

from backend.app.governance.identity import authorized_source_ids_for
from backend.app.graph.models import Entity, Evidence
from backend.app.graph.repository import GraphRepository
from backend.app.security import AuthorizationContext, Permission
from backend.app.tools.contracts import ExecutionLimits
from backend.app.tools.registry import SideEffect, ToolDependencies, ToolRegistry, ToolSpec

EVIDENCE_LOOKUP_TOOL_ID = "evidence.lookup"
EVIDENCE_LOOKUP_VERSION = "1.0.0"
EVIDENCE_VERIFY_TOOL_ID = "evidence.verify"
EVIDENCE_VERIFY_VERSION = "1.0.0"
EVIDENCE_CONFLICTS_TOOL_ID = "evidence.conflicts"
EVIDENCE_CONFLICTS_VERSION = "1.0.0"


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
    ND-11A): additionally intersects an explicit, caller-selected
    ``principal.source_ids`` set into the read scope, for any tenant, on top
    of whatever ``authorized_source_ids_for`` already restricts."""
    governance_scope = authorized_source_ids_for(context.principal)
    selection_scope = frozenset(context.principal.source_ids) if context.principal.source_ids else None
    if governance_scope is None:
        return selection_scope
    if selection_scope is None:
        return governance_scope
    return governance_scope & selection_scope


class EvidenceLookupInput(BaseModel):
    entity_id: str = Field(min_length=1, max_length=500)
    limit: int = Field(default=50, ge=1, le=100)


class EvidenceLookupOutput(BaseModel):
    items: list[Evidence]


def _evidence_lookup_handler(context: AuthorizationContext, input_model: EvidenceLookupInput,
                             dependencies: ToolDependencies) -> dict | None:
    repo = _repository(context, dependencies)
    if repo.get_entity(input_model.entity_id) is None:
        return None
    page = repo.evidence(input_model.entity_id, input_model.limit)
    return {"items": page["items"], "truncated": page["truncated"],
            "evidence_refs": tuple(f"{item.source_id}:{item.record_id}" for item in page["items"]),
            "graph_revision": repo.graph_revision()}


class EvidenceConflictsInput(BaseModel):
    entity_id: str = Field(min_length=1, max_length=500)


class EvidenceConflictsOutput(BaseModel):
    entity_id: str
    conflicts: dict[str, list[str]]
    authority: dict[str, dict]


def _evidence_conflicts_handler(context: AuthorizationContext, input_model: EvidenceConflictsInput,
                                dependencies: ToolDependencies) -> dict | None:
    repo = _repository(context, dependencies)
    entity = repo.get_entity(input_model.entity_id)
    if entity is None:
        return None
    return {"entity_id": entity.id, "conflicts": entity.conflicts, "authority": entity.authority,
            "graph_revision": repo.graph_revision()}


class VerificationOutcome(StrEnum):
    SUPPORTED = "SUPPORTED"
    UNSUPPORTED = "UNSUPPORTED"
    CONFLICTING = "CONFLICTING"
    INCOMPLETE = "INCOMPLETE"
    INACCESSIBLE = "INACCESSIBLE"


class EvidenceVerifyInput(BaseModel):
    entity_id: str = Field(min_length=1, max_length=500)
    limit: int = Field(default=50, ge=1, le=100)


class EvidenceVerifyOutput(BaseModel):
    entity: Entity
    outcome: VerificationOutcome
    assertions: list[dict] | None = None
    evidence: list[Evidence] | None = None


def _evidence_verify_handler(context: AuthorizationContext, input_model: EvidenceVerifyInput,
                             dependencies: ToolDependencies) -> dict | None:
    """Entity-granular verification: ``origin``/``derivation_rule_version``
    only exist on relationships in the current data model (``RelationInput``),
    not on entity assertions, so they are not part of this entity-scoped
    outcome — only what the ontology/model actually tracks is reported."""
    repo = _repository(context, dependencies)
    entity = repo.get_entity(input_model.entity_id)
    if entity is None:
        return None

    if not repo.can_evidence:
        # Conflicts/authority come from plain READ_GRAPH-scoped get_entity, so
        # reporting them here never discloses anything the caller could not
        # already see; only the per-source assertion/evidence detail is
        # unavailable without READ_EVIDENCE.
        outcome = VerificationOutcome.CONFLICTING if entity.conflicts else VerificationOutcome.INACCESSIBLE
        return {"entity": entity, "outcome": outcome, "assertions": None, "evidence": None,
                "graph_revision": repo.graph_revision()}

    assertions_page = repo.assertions(input_model.entity_id, input_model.limit)
    evidence_page = repo.evidence(input_model.entity_id, input_model.limit)
    current = [item for item in assertions_page["items"] if item["current"]]
    if entity.conflicts:
        outcome = VerificationOutcome.CONFLICTING
    elif current:
        outcome = VerificationOutcome.SUPPORTED
    elif assertions_page["items"]:
        outcome = VerificationOutcome.UNSUPPORTED
    else:
        outcome = VerificationOutcome.INCOMPLETE

    return {
        "entity": entity, "outcome": outcome, "assertions": assertions_page["items"],
        "evidence": evidence_page["items"],
        "truncated": assertions_page["truncated"] or evidence_page["truncated"],
        "evidence_refs": tuple(f"{item.source_id}:{item.record_id}" for item in evidence_page["items"]),
        "graph_revision": repo.graph_revision(),
    }


def register_evidence_tools(registry: ToolRegistry) -> None:
    registry.register(ToolSpec(
        tool_id=EVIDENCE_LOOKUP_TOOL_ID,
        version=EVIDENCE_LOOKUP_VERSION,
        description="Authorized source-record evidence backing an entity.",
        input_model=EvidenceLookupInput,
        output_model=EvidenceLookupOutput,
        handler=_evidence_lookup_handler,
        required_permissions=frozenset({Permission.READ_GRAPH, Permission.READ_EVIDENCE}),
        side_effect=SideEffect.READ_ONLY,
        limits=ExecutionLimits(max_input_bytes=1024, max_output_bytes=131072,
                               max_result_count=100, timeout_seconds=10.0),
        capabilities=frozenset({"evidence.read"}),
        dependencies=frozenset({"graph_store"}),
    ))
    registry.register(ToolSpec(
        tool_id=EVIDENCE_CONFLICTS_TOOL_ID,
        version=EVIDENCE_CONFLICTS_VERSION,
        description="Authorized conflicting-field claims and their (unresolved or ruled) authority verdicts.",
        input_model=EvidenceConflictsInput,
        output_model=EvidenceConflictsOutput,
        handler=_evidence_conflicts_handler,
        required_permissions=frozenset({Permission.READ_GRAPH}),
        side_effect=SideEffect.READ_ONLY,
        limits=ExecutionLimits(max_input_bytes=1024, max_output_bytes=32768,
                               max_result_count=1, timeout_seconds=10.0),
        capabilities=frozenset({"graph.read"}),
        dependencies=frozenset({"graph_store"}),
    ))
    registry.register(ToolSpec(
        tool_id=EVIDENCE_VERIFY_TOOL_ID,
        version=EVIDENCE_VERIFY_VERSION,
        description="Deterministic entity evidence verification: SUPPORTED/UNSUPPORTED/CONFLICTING/"
                    "INCOMPLETE/INACCESSIBLE, never a fabricated confidence score.",
        input_model=EvidenceVerifyInput,
        output_model=EvidenceVerifyOutput,
        handler=_evidence_verify_handler,
        required_permissions=frozenset({Permission.READ_GRAPH}),
        side_effect=SideEffect.READ_ONLY,
        limits=ExecutionLimits(max_input_bytes=1024, max_output_bytes=131072,
                               max_result_count=100, timeout_seconds=10.0),
        capabilities=frozenset({"evidence.read"}),
        dependencies=frozenset({"graph_store"}),
    ))
