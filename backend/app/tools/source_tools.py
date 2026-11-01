"""Deterministic source-coverage status, built from existing persisted
metadata only (``connector_configs``/``sync_runs`` in PostgreSQL, plus
``GraphRepository.sources``). Never infers that a relationship is false from
absent data, and never fabricates a status the persisted metadata cannot
substantiate — ambiguous cases resolve to UNKNOWN_UNVERIFIED or
PARTIALLY_AVAILABLE rather than a confident guess.
"""
from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field

from backend.app.governance.identity import authorized_source_ids_for
from backend.app.graph.repository import GraphRepository
from backend.app.security import AuthorizationContext, Permission
from backend.app.tools.contracts import ExecutionLimits
from backend.app.tools.registry import SideEffect, ToolDependencies, ToolRegistry, ToolSpec

SOURCE_COVERAGE_TOOL_ID = "source.coverage"
SOURCE_COVERAGE_VERSION = "1.0.0"


class SourceCoverageStatus(StrEnum):
    AVAILABLE = "AVAILABLE"
    PARTIALLY_AVAILABLE = "PARTIALLY_AVAILABLE"
    UNKNOWN_UNVERIFIED = "UNKNOWN_UNVERIFIED"
    UNSUPPORTED_NOT_CONFIGURED = "UNSUPPORTED_NOT_CONFIGURED"
    TEMPORARILY_UNAVAILABLE = "TEMPORARILY_UNAVAILABLE"


class SourceCoverageInput(BaseModel):
    source_id: str = Field(min_length=1, max_length=200)


class SourceCoverageOutput(BaseModel):
    source_id: str
    status: SourceCoverageStatus
    graph_record_count: int
    connector_configured: bool | None = None


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


def _coverage_handler(context: AuthorizationContext, input_model: SourceCoverageInput,
                      dependencies: ToolDependencies) -> dict:
    repo = _repository(context, dependencies)
    graph_sources = repo.sources(limit=100)["items"]
    graph_entry = next((item for item in graph_sources if item["id"] == input_model.source_id), None)
    graph_record_count = graph_entry["records"] if graph_entry else 0
    has_graph_data = graph_record_count > 0

    if dependencies.postgres_store is None:
        status = (SourceCoverageStatus.AVAILABLE if has_graph_data
                  else SourceCoverageStatus.UNKNOWN_UNVERIFIED)
        return {"source_id": input_model.source_id, "status": status,
                "graph_record_count": graph_record_count, "connector_configured": None}

    configs = dependencies.postgres_store.read(
        "SELECT connector_id, enabled FROM connector_configs WHERE tenant_id=%s AND workspace_id=%s AND source_id=%s",
        (context.tenant_id, context.workspace_id, input_model.source_id))

    if not configs:
        status = (SourceCoverageStatus.PARTIALLY_AVAILABLE if has_graph_data
                  else SourceCoverageStatus.UNSUPPORTED_NOT_CONFIGURED)
        return {"source_id": input_model.source_id, "status": status,
                "graph_record_count": graph_record_count, "connector_configured": False}

    latest_runs = []
    for config in configs:
        rows = dependencies.postgres_store.read(
            "SELECT status, retryable FROM sync_runs WHERE tenant_id=%s AND workspace_id=%s "
            "AND connector_id=%s ORDER BY started_at DESC LIMIT 1",
            (context.tenant_id, context.workspace_id, config["connector_id"]))
        latest_runs.append(rows[0] if rows else None)

    enabled_any = any(config["enabled"] for config in configs)
    succeeded_any = any(run and run["status"] == "SUCCEEDED" for run in latest_runs)
    failed_retryable_any = any(run and run["status"] == "FAILED" and run["retryable"] for run in latest_runs)
    never_run = all(run is None for run in latest_runs)

    if not enabled_any:
        status = SourceCoverageStatus.UNSUPPORTED_NOT_CONFIGURED
    elif failed_retryable_any:
        status = SourceCoverageStatus.TEMPORARILY_UNAVAILABLE
    elif succeeded_any and has_graph_data:
        status = SourceCoverageStatus.AVAILABLE
    elif never_run:
        status = SourceCoverageStatus.UNKNOWN_UNVERIFIED
    elif has_graph_data:
        status = SourceCoverageStatus.PARTIALLY_AVAILABLE
    else:
        status = SourceCoverageStatus.UNKNOWN_UNVERIFIED

    return {"source_id": input_model.source_id, "status": status,
            "graph_record_count": graph_record_count, "connector_configured": True}


def register_source_tools(registry: ToolRegistry) -> None:
    registry.register(ToolSpec(
        tool_id=SOURCE_COVERAGE_TOOL_ID,
        version=SOURCE_COVERAGE_VERSION,
        description="Deterministic coverage status for one source, from persisted connector/sync metadata.",
        input_model=SourceCoverageInput,
        output_model=SourceCoverageOutput,
        handler=_coverage_handler,
        required_permissions=frozenset({Permission.READ_EVIDENCE}),
        side_effect=SideEffect.READ_ONLY,
        limits=ExecutionLimits(max_input_bytes=512, max_output_bytes=4096,
                               max_result_count=1, timeout_seconds=15.0),
        capabilities=frozenset({"source.read"}),
        dependencies=frozenset({"graph_store", "postgres_store"}),
    ))
