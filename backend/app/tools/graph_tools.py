"""First operational tool: authorized entity lookup via the existing
``GraphRepository``. No graph query logic is duplicated here — the handler
only resolves a scoped ``GraphRepository`` from trusted context and calls
its existing read methods.
"""
from __future__ import annotations

from pydantic import BaseModel, Field

from backend.app.governance.identity import authorized_source_ids_for
from backend.app.graph.models import Entity, Relationship
from backend.app.graph.repository import GraphRepository
from backend.app.security import AuthorizationContext, Permission
from backend.app.tools.contracts import ExecutionLimits
from backend.app.tools.registry import SideEffect, ToolDependencies, ToolRegistry, ToolSpec

GRAPH_ENTITY_LOOKUP_TOOL_ID = "graph.entity_lookup"
GRAPH_ENTITY_LOOKUP_VERSION = "1.0.0"
GRAPH_NEIGHBORHOOD_TOOL_ID = "graph.neighborhood"
GRAPH_NEIGHBORHOOD_VERSION = "1.0.0"
GRAPH_FIND_PATHS_TOOL_ID = "graph.find_paths"
GRAPH_FIND_PATHS_VERSION = "1.0.0"
GRAPH_REVERSE_DEPENDENCIES_TOOL_ID = "graph.reverse_dependencies"
GRAPH_REVERSE_DEPENDENCIES_VERSION = "1.0.0"
GRAPH_RELATIONSHIP_LOOKUP_TOOL_ID = "graph.relationship_lookup"
GRAPH_RELATIONSHIP_LOOKUP_VERSION = "1.0.0"
GRAPH_REVISION_TOOL_ID = "graph.revision"
GRAPH_REVISION_VERSION = "1.0.0"


class GraphEntityLookupInput(BaseModel):
    entity_id: str = Field(min_length=1, max_length=500)
    include_history: bool = False


class GraphEntityLookupOutput(BaseModel):
    entity: Entity


def _entity_lookup_handler(context: AuthorizationContext, input_model: GraphEntityLookupInput,
                           dependencies: ToolDependencies) -> dict | None:
    repo = _repository(context, dependencies)
    entity = (repo.get_entity_history(input_model.entity_id) if input_model.include_history
              else repo.get_entity(input_model.entity_id))
    if entity is None:
        return None
    return {"entity": entity, "graph_revision": repo.graph_revision()}


class GraphNeighborhoodInput(BaseModel):
    entity_id: str = Field(min_length=1, max_length=500)
    limit: int = Field(default=50, ge=1, le=100)
    include_history: bool = False


class GraphNeighborhoodOutput(BaseModel):
    nodes: list[Entity]
    relationships: list[Relationship]
    coverage: str = "source_backed_only"


def _neighborhood_handler(context: AuthorizationContext, input_model: GraphNeighborhoodInput,
                          dependencies: ToolDependencies) -> dict | None:
    repo = _repository(context, dependencies)
    exists = (repo.get_entity_history(input_model.entity_id) if input_model.include_history
              else repo.get_entity(input_model.entity_id)) is not None
    if not exists:
        return None
    page = repo.neighborhood(input_model.entity_id, input_model.limit, input_model.include_history)
    return {"nodes": page.nodes, "relationships": page.relationships, "coverage": page.coverage,
            "truncated": page.truncated, "graph_revision": repo.graph_revision()}


class GraphReverseDependenciesInput(BaseModel):
    entity_id: str = Field(min_length=1, max_length=500)
    limit: int = Field(default=50, ge=1, le=100)


def _reverse_dependencies_handler(context: AuthorizationContext, input_model: GraphReverseDependenciesInput,
                                  dependencies: ToolDependencies) -> dict | None:
    repo = _repository(context, dependencies)
    if repo.get_entity(input_model.entity_id) is None:
        return None
    page = repo.reverse_dependencies(input_model.entity_id, input_model.limit)
    return {"nodes": page.nodes, "relationships": page.relationships, "coverage": page.coverage,
            "truncated": page.truncated, "graph_revision": repo.graph_revision()}


class GraphFindPathsInput(BaseModel):
    source_id: str = Field(min_length=1, max_length=500)
    target_id: str = Field(min_length=1, max_length=500)
    max_depth: int = Field(default=3, ge=1, le=4)
    limit: int = Field(default=10, ge=1, le=20)


class GraphPathResult(BaseModel):
    nodes: list[Entity]
    relationships: list[Relationship]


class GraphFindPathsOutput(BaseModel):
    paths: list[GraphPathResult]
    coverage: str = "source_backed_only"


def _find_paths_handler(context: AuthorizationContext, input_model: GraphFindPathsInput,
                        dependencies: ToolDependencies) -> dict | None:
    repo = _repository(context, dependencies)
    if repo.get_entity(input_model.source_id) is None or repo.get_entity(input_model.target_id) is None:
        return None
    result = repo.paths(input_model.source_id, input_model.target_id, input_model.max_depth, input_model.limit)
    return {"paths": result["paths"], "coverage": result["coverage"], "truncated": result["truncated"],
            "graph_revision": repo.graph_revision()}


class GraphRelationshipLookupInput(BaseModel):
    relation_id: str = Field(min_length=1, max_length=500)


class GraphRelationshipLookupOutput(BaseModel):
    relationship: Relationship


def _relationship_lookup_handler(context: AuthorizationContext, input_model: GraphRelationshipLookupInput,
                                 dependencies: ToolDependencies) -> dict | None:
    repo = _repository(context, dependencies)
    relationship = repo.relationship(input_model.relation_id)
    if relationship is None:
        return None
    return {"relationship": relationship, "graph_revision": repo.graph_revision()}


class GraphRevisionInput(BaseModel):
    pass


class GraphRevisionOutput(BaseModel):
    revision: int


def _graph_revision_handler(context: AuthorizationContext, input_model: GraphRevisionInput,
                            dependencies: ToolDependencies) -> dict:
    repo = _repository(context, dependencies)
    revision = repo.graph_revision()
    return {"revision": revision, "graph_revision": revision}


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
    """Stage 11 (ND-11A): ``authorized_source_ids_for`` alone only restricts a
    NEXUS-resolved principal. ``AgentExecutionContext.authorization.principal``
    may additionally carry an explicit, caller-selected ``source_ids`` set
    narrowed to ``selected source_ids ∩ principal's authorized source_ids``
    (see ``backend/app/agent/discovery.py``'s ``effective_source_scope``) even
    for a non-NEXUS principal; when present, this intersects that selection
    into the read scope too, so an unselected source never reaches a tool
    result regardless of tenant. A principal with no explicit ``source_ids``
    keeps exactly its prior behavior (``authorized_source_ids_for`` alone)."""
    governance_scope = authorized_source_ids_for(context.principal)
    selection_scope = frozenset(context.principal.source_ids) if context.principal.source_ids else None
    if governance_scope is None:
        return selection_scope
    if selection_scope is None:
        return governance_scope
    return governance_scope & selection_scope


def register_graph_tools(registry: ToolRegistry) -> None:
    registry.register(ToolSpec(
        tool_id=GRAPH_ENTITY_LOOKUP_TOOL_ID,
        version=GRAPH_ENTITY_LOOKUP_VERSION,
        description="Look up a single authorized graph entity by id, optionally including inactive history.",
        input_model=GraphEntityLookupInput,
        output_model=GraphEntityLookupOutput,
        handler=_entity_lookup_handler,
        required_permissions=frozenset({Permission.READ_GRAPH}),
        side_effect=SideEffect.READ_ONLY,
        limits=ExecutionLimits(max_input_bytes=1024, max_output_bytes=32768,
                               max_result_count=1, timeout_seconds=10.0),
        requires_approval=False,
        capabilities=frozenset({"graph.read"}),
        dependencies=frozenset({"graph_store"}),
    ))
    registry.register(ToolSpec(
        tool_id=GRAPH_NEIGHBORHOOD_TOOL_ID,
        version=GRAPH_NEIGHBORHOOD_VERSION,
        description="Authorized one-hop neighborhood of an entity, current or including history.",
        input_model=GraphNeighborhoodInput,
        output_model=GraphNeighborhoodOutput,
        handler=_neighborhood_handler,
        required_permissions=frozenset({Permission.READ_GRAPH}),
        side_effect=SideEffect.READ_ONLY,
        limits=ExecutionLimits(max_input_bytes=1024, max_output_bytes=131072,
                               max_result_count=100, timeout_seconds=10.0),
        capabilities=frozenset({"graph.read"}),
        dependencies=frozenset({"graph_store"}),
    ))
    registry.register(ToolSpec(
        tool_id=GRAPH_REVERSE_DEPENDENCIES_TOOL_ID,
        version=GRAPH_REVERSE_DEPENDENCIES_VERSION,
        description="Authorized reverse DEPENDS_ON lookup: entities that depend on the given entity.",
        input_model=GraphReverseDependenciesInput,
        output_model=GraphNeighborhoodOutput,
        handler=_reverse_dependencies_handler,
        required_permissions=frozenset({Permission.READ_GRAPH}),
        side_effect=SideEffect.READ_ONLY,
        limits=ExecutionLimits(max_input_bytes=1024, max_output_bytes=131072,
                               max_result_count=100, timeout_seconds=10.0),
        capabilities=frozenset({"graph.read"}),
        dependencies=frozenset({"graph_store"}),
    ))
    registry.register(ToolSpec(
        tool_id=GRAPH_FIND_PATHS_TOOL_ID,
        version=GRAPH_FIND_PATHS_VERSION,
        description="Authorized bounded path search between two entities (source-backed edges only).",
        input_model=GraphFindPathsInput,
        output_model=GraphFindPathsOutput,
        handler=_find_paths_handler,
        required_permissions=frozenset({Permission.READ_GRAPH}),
        side_effect=SideEffect.READ_ONLY,
        limits=ExecutionLimits(max_input_bytes=1024, max_output_bytes=131072,
                               max_result_count=20, timeout_seconds=12.0),
        capabilities=frozenset({"graph.read"}),
        dependencies=frozenset({"graph_store"}),
    ))
    registry.register(ToolSpec(
        tool_id=GRAPH_RELATIONSHIP_LOOKUP_TOOL_ID,
        version=GRAPH_RELATIONSHIP_LOOKUP_VERSION,
        description="Look up a single authorized relationship by id.",
        input_model=GraphRelationshipLookupInput,
        output_model=GraphRelationshipLookupOutput,
        handler=_relationship_lookup_handler,
        required_permissions=frozenset({Permission.READ_GRAPH}),
        side_effect=SideEffect.READ_ONLY,
        limits=ExecutionLimits(max_input_bytes=1024, max_output_bytes=32768,
                               max_result_count=1, timeout_seconds=10.0),
        capabilities=frozenset({"graph.read"}),
        dependencies=frozenset({"graph_store"}),
    ))
    registry.register(ToolSpec(
        tool_id=GRAPH_REVISION_TOOL_ID,
        version=GRAPH_REVISION_VERSION,
        description="Current workspace-scoped graph revision counter.",
        input_model=GraphRevisionInput,
        output_model=GraphRevisionOutput,
        handler=_graph_revision_handler,
        required_permissions=frozenset({Permission.READ_GRAPH}),
        side_effect=SideEffect.READ_ONLY,
        limits=ExecutionLimits(max_input_bytes=256, max_output_bytes=1024,
                               max_result_count=1, timeout_seconds=10.0),
        capabilities=frozenset({"graph.read"}),
        dependencies=frozenset({"graph_store"}),
    ))
