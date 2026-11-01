from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from backend.app.config import Settings
from backend.app.contracts import (
    BaselineMetrics, Checkpoint, Completion, ExecutionBudget, Handoff, Identity,
    OutboxEvent, ProjectionWatermark, StorageAdoptionCriteria, TaskState,
)
from backend.app.security import AuthorizationContext, Permission, Principal, ToolPolicy


NOW = datetime.now(timezone.utc)


def identity():
    return Identity(tenant_id="tenant", workspace_id="space", principal_id="user",
                    task_id="task", workflow_id="workflow", agent_run_id="run-1")


def principal():
    return Principal(subject="user", tenant_id="tenant", workspaces=("space",),
                     permissions=frozenset({Permission.READ_GRAPH, Permission.EXECUTE_TOOL}))


def budget():
    return ExecutionBudget(remaining_steps=2, remaining_tool_calls=1,
                           deadline=NOW + timedelta(minutes=5))


def test_identity_budget_and_completion_validation():
    with pytest.raises(ValidationError):
        Identity(tenant_id="", workspace_id="space", principal_id="user", task_id="task",
                 workflow_id="workflow", agent_run_id="run")
    with pytest.raises(ValidationError):
        ExecutionBudget(remaining_steps=-1, remaining_tool_calls=1, deadline=NOW)
    with pytest.raises(ValidationError):
        Completion(identity=identity(), state=TaskState.RUNNING, completed_at=NOW,
                   idempotency_key="key")


def test_authorization_scope_delegation_and_tool_approval():
    user = principal()
    with pytest.raises(ValidationError):
        AuthorizationContext(principal=user, tenant_id="other", workspace_id="space", task_id="task")
    with pytest.raises(ValidationError):
        AuthorizationContext(principal=user, tenant_id="tenant", workspace_id="other", task_id="task")
    with pytest.raises(ValidationError):
        AuthorizationContext(principal=user, tenant_id="tenant", workspace_id="space", task_id="task",
                             permissions={Permission.ADMIN})
    context = AuthorizationContext(principal=user, tenant_id="tenant", workspace_id="space",
                                   task_id="task", permissions={Permission.READ_GRAPH, Permission.EXECUTE_TOOL})
    assert ToolPolicy(name="graph_search").required_permission == Permission.READ_GRAPH
    with pytest.raises(PermissionError):
        ToolPolicy(name="send", required_permission=Permission.EXECUTE_TOOL,
                   requires_approval=True).authorize(context)
    with pytest.raises(ValueError):
        context.delegate(frozenset({Permission.ADMIN}))
    child = context.delegate(frozenset({Permission.READ_GRAPH}))
    with pytest.raises(PermissionError):
        ToolPolicy(name="execute", required_permission=Permission.EXECUTE_TOOL).authorize(child)


def test_principal_cannot_invent_permissions():
    with pytest.raises(ValidationError):
        Principal(subject="u", tenant_id="t", workspaces=("w",), permissions={"SUPERUSER"})


def test_checkpoint_and_handoff_fencing():
    checkpoint = Checkpoint(identity=identity(), version=1, task_revision=0, graph_revision=0,
                            evidence_refs=("evidence:1",), budget=budget(),
                            expires_at=NOW + timedelta(minutes=10),
                            idempotency_scope="task:1", owner_fence=1)
    assert Handoff(checkpoint=checkpoint, from_agent_run_id="run-1", to_agent_run_id="run-2",
                   expected_task_revision=0, expected_owner_fence=1, new_owner_fence=2)
    with pytest.raises(ValidationError):
        Checkpoint(identity=identity(), version=0, task_revision=0, graph_revision=0, budget=budget(),
                   expires_at=NOW + timedelta(minutes=10), idempotency_scope="task:1", owner_fence=1)
    with pytest.raises(ValidationError):
        Checkpoint(identity=identity(), version=1, task_revision=0, graph_revision=0, budget=budget(),
                   expires_at=NOW - timedelta(minutes=1), idempotency_scope="task:1", owner_fence=1)
    with pytest.raises(ValidationError):
        Handoff(checkpoint=checkpoint, from_agent_run_id="run-1", to_agent_run_id="run-2",
                expected_task_revision=0, expected_owner_fence=2, new_owner_fence=3)
    with pytest.raises(ValidationError):
        Handoff(checkpoint=checkpoint, from_agent_run_id="run-1", to_agent_run_id="run-2",
                expected_task_revision=1, expected_owner_fence=1, new_owner_fence=2)


def test_event_revision_and_replay_contract():
    event = OutboxEvent(event_id="event-1", tenant_id="tenant", aggregate_id="task",
                        aggregate_revision=2, event_type="TaskChanged", idempotency_key="key-1",
                        occurred_at=NOW)
    watermark = ProjectionWatermark(projector="graph", tenant_id="tenant", aggregate_id="task", revision=1)
    assert watermark.accepts(event)
    assert event.deduplication_key == ("tenant", "key-1")
    assert not watermark.needs_replay(event)
    assert ProjectionWatermark(projector="graph", tenant_id="tenant", aggregate_id="task",
                               revision=0).needs_replay(event)
    with pytest.raises(ValueError):
        watermark.accepts(event.model_copy(update={"tenant_id": "other"}))
    with pytest.raises(ValidationError):
        OutboxEvent(event_id="", tenant_id="tenant", aggregate_id="task",
                    aggregate_revision=0, event_type="TaskChanged", idempotency_key="",
                    occurred_at=NOW)


def test_baseline_metrics_and_storage_adoption_criteria():
    with pytest.raises(ValidationError):
        BaselineMetrics(tenant_id="tenant", measured_at=NOW.replace(tzinfo=None),
                        ingest_p95_ms=10, query_p95_ms=5, connector_count=1,
                        cross_store_conflicts_observed=0)
    quiet = BaselineMetrics(tenant_id="tenant", measured_at=NOW, ingest_p95_ms=10,
                            query_p95_ms=5, connector_count=1, cross_store_conflicts_observed=0)
    busy = BaselineMetrics(tenant_id="tenant", measured_at=NOW, ingest_p95_ms=10,
                           query_p95_ms=5, connector_count=2, cross_store_conflicts_observed=0)
    criteria = StorageAdoptionCriteria()
    assert not criteria.should_adopt(quiet)
    assert criteria.should_adopt(busy)


def test_config_urls():
    valid = dict(neo4j_uri="bolt://localhost:7687", neo4j_user="neo4j", neo4j_password="secret")
    assert Settings(**valid, postgres_dsn="postgresql://user:secret@localhost:5432/graphhacks")
    with pytest.raises(ValidationError):
        Settings(**{**valid, "neo4j_uri": "http://localhost:7474"})
    with pytest.raises(ValidationError):
        Settings(**valid, postgres_dsn="sqlite:///graphhacks")
