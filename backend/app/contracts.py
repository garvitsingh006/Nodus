"""Cross-component records and ports; Temporal owns durable workflow lifecycle.

Neo4j owns graph facts and source-backed evidence metadata. PostgreSQL owns
transactional task metadata and the outbox. Object storage owns evidence bytes.
Temporal alone owns durable workflow state, retries and completion. PostgreSQL
task rows are projections, never a second workflow scheduler.
"""
from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from typing import Protocol
from pydantic import BaseModel, ConfigDict, Field, model_validator

from backend.app.graph.models import SourceRecordInput, Evidence
from backend.app.security import AuthorizationContext, ToolPolicy


class Record(BaseModel):
    model_config = ConfigDict(frozen=True)


class Identity(Record):
    tenant_id: str = Field(min_length=1)
    workspace_id: str = Field(min_length=1)
    principal_id: str = Field(min_length=1)
    task_id: str = Field(min_length=1)
    workflow_id: str = Field(min_length=1)
    agent_run_id: str = Field(min_length=1)


class ExecutionBudget(Record):
    remaining_steps: int = Field(ge=0)
    remaining_tool_calls: int = Field(ge=0)
    deadline: datetime

    @model_validator(mode="after")
    def aware_deadline(self):
        if self.deadline.tzinfo is None:
            raise ValueError("Deadline must have a timezone")
        return self


class TaskState(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class TaskSnapshot(Record):
    identity: Identity
    state: TaskState
    budget: ExecutionBudget
    workflow_run_id: str = Field(min_length=1)
    revision: int = Field(ge=0)
    owner_fence: int = Field(ge=1)


class Completion(Record):
    identity: Identity
    state: TaskState
    completed_at: datetime
    idempotency_key: str = Field(min_length=1)

    @model_validator(mode="after")
    def terminal(self):
        if self.state not in {TaskState.COMPLETED, TaskState.FAILED, TaskState.CANCELLED}:
            raise ValueError("Completion requires a terminal state")
        if self.completed_at.tzinfo is None:
            raise ValueError("Completion time must have a timezone")
        return self


class Checkpoint(Record):
    identity: Identity
    version: int = Field(ge=1)
    task_revision: int = Field(ge=0)
    graph_revision: int = Field(ge=0)
    evidence_refs: tuple[str, ...] = ()
    budget: ExecutionBudget
    expires_at: datetime
    idempotency_scope: str = Field(min_length=1)
    owner_fence: int = Field(ge=1)

    @model_validator(mode="after")
    def valid_expiry(self):
        if self.expires_at.tzinfo is None or self.expires_at <= datetime.now(timezone.utc):
            raise ValueError("Checkpoint must have a future timezone-aware expiry")
        if self.budget.deadline <= datetime.now(timezone.utc) or self.budget.deadline > self.expires_at:
            raise ValueError("Budget deadline must be in the future and within checkpoint expiry")
        if any(not ref for ref in self.evidence_refs):
            raise ValueError("Evidence references cannot be empty")
        return self


class Handoff(Record):
    checkpoint: Checkpoint
    from_agent_run_id: str = Field(min_length=1)
    to_agent_run_id: str = Field(min_length=1)
    expected_task_revision: int = Field(ge=0)
    expected_owner_fence: int = Field(ge=1)
    new_owner_fence: int = Field(ge=2)

    @model_validator(mode="after")
    def fenced(self):
        if self.from_agent_run_id != self.checkpoint.identity.agent_run_id:
            raise ValueError("Handoff source must own checkpoint")
        if self.from_agent_run_id == self.to_agent_run_id:
            raise ValueError("Handoff target must differ")
        if self.expected_owner_fence != self.checkpoint.owner_fence or self.new_owner_fence != self.expected_owner_fence + 1:
            raise ValueError("Handoff requires compare-and-swap ownership fencing")
        if self.expected_task_revision != self.checkpoint.task_revision:
            raise ValueError("Handoff requires compare-and-swap task revision")
        return self


class OutboxEvent(Record):
    event_id: str = Field(min_length=1)
    tenant_id: str = Field(min_length=1)
    aggregate_id: str = Field(min_length=1)
    aggregate_revision: int = Field(ge=1)
    event_type: str = Field(min_length=1)
    idempotency_key: str = Field(min_length=1)
    occurred_at: datetime

    @model_validator(mode="after")
    def aware(self):
        if self.occurred_at.tzinfo is None:
            raise ValueError("Event time must have a timezone")
        return self

    @property
    def deduplication_key(self) -> tuple[str, str]:
        """Outbox enforces unique (tenant, event ID) and (tenant, idempotency key)."""
        return self.tenant_id, self.idempotency_key


class ProjectionWatermark(Record):
    projector: str = Field(min_length=1)
    tenant_id: str = Field(min_length=1)
    aggregate_id: str = Field(min_length=1)
    revision: int = Field(ge=0)
    last_event_id: str | None = None

    def accepts(self, event: OutboxEvent) -> bool:
        """Duplicates are ignored; gaps trigger replay or reconciliation."""
        if (event.tenant_id, event.aggregate_id) != (self.tenant_id, self.aggregate_id):
            raise ValueError("Event is outside projector scope")
        return event.aggregate_revision == self.revision + 1

    def needs_replay(self, event: OutboxEvent) -> bool:
        return event.aggregate_revision > self.revision + 1


class BaselineMetrics(Record):
    """Stage 01 reference measurements for the current Neo4j-only slice.

    Postgres/outbox are contracts only (see StorageAdoptionCriteria); only
    metrics observable from the live graph API are recorded here.
    """
    tenant_id: str = Field(min_length=1)
    measured_at: datetime
    ingest_p95_ms: float = Field(ge=0)
    query_p95_ms: float = Field(ge=0)
    connector_count: int = Field(ge=0)
    cross_store_conflicts_observed: int = Field(ge=0)

    @model_validator(mode="after")
    def aware(self):
        if self.measured_at.tzinfo is None:
            raise ValueError("Baseline measurement time must have a timezone")
        return self


class StorageAdoptionCriteria(Record):
    """Explicit gate for moving the Postgres outbox/projector from contract to
    implementation. Do not build OutboxPort/ProjectorPort adapters until this
    is satisfied by an observed BaselineMetrics sample.
    """
    min_connector_count: int = Field(default=2, ge=1)
    min_cross_store_conflicts: int = Field(default=1, ge=0)

    def should_adopt(self, metrics: BaselineMetrics) -> bool:
        return (metrics.connector_count >= self.min_connector_count
                or metrics.cross_store_conflicts_observed >= self.min_cross_store_conflicts)


class GraphPort(Protocol):
    def ingest(self, record: SourceRecordInput) -> dict: ...


class EvidencePort(Protocol):
    def evidence(self, entity_id: str, limit: int) -> dict: ...


class ConnectorPort(Protocol):
    def fetch(self, cursor: str | None) -> tuple[list[SourceRecordInput], str | None]: ...


class WorkflowPort(Protocol):
    """Implemented only by a Temporal-backed adapter; no database scheduler."""
    def start(self, identity: Identity, budget: ExecutionBudget) -> str: ...
    def snapshot(self, workflow_id: str) -> TaskSnapshot: ...


class AgentPort(Protocol):
    def run(self, checkpoint: Checkpoint, context: AuthorizationContext) -> Completion | Handoff: ...


class ToolPort(Protocol):
    policy: ToolPolicy
    def invoke(self, context: AuthorizationContext, arguments: dict) -> object: ...


class OutboxPort(Protocol):
    """Append atomically with PostgreSQL change; enforce event and dedup keys."""
    def append(self, event: OutboxEvent) -> None: ...


class ProjectorPort(Protocol):
    """Apply by (tenant, aggregate, revision); persist watermark atomically."""
    def apply(self, event: OutboxEvent, watermark: ProjectionWatermark) -> ProjectionWatermark: ...
    def replay(self, tenant_id: str, aggregate_id: str, from_revision: int) -> None: ...
    def reconcile(self, tenant_id: str, aggregate_id: str) -> None: ...
