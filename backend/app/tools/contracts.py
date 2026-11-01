"""Structured tool execution contracts shared by registry and executor.

Reuses ``backend.app.contracts.Record`` (frozen Pydantic base) and the
Stage 01 security contracts (``Permission``, ``AuthorizationContext``,
``ToolPolicy``) rather than introducing parallel definitions.
"""
from __future__ import annotations

from enum import StrEnum
from pydantic import Field, model_validator

from backend.app.contracts import Record
from backend.app.security import AuthorizationContext, Permission


class ToolStatus(StrEnum):
    SUCCESS = "SUCCESS"
    INVALID_INPUT = "INVALID_INPUT"
    FORBIDDEN = "FORBIDDEN"
    NOT_FOUND = "NOT_FOUND"
    UNAVAILABLE = "UNAVAILABLE"
    INCOMPLETE = "INCOMPLETE"
    EXECUTION_FAILED = "EXECUTION_FAILED"


class SideEffect(StrEnum):
    READ_ONLY = "READ_ONLY"
    MUTATION = "MUTATION"


class ExecutionLimits(Record):
    max_input_bytes: int = Field(default=4096, ge=1)
    max_output_bytes: int = Field(default=65536, ge=1)
    max_result_count: int = Field(default=100, ge=1)
    timeout_seconds: float = Field(default=10.0, gt=0)


class ToolInvocation(Record):
    """A typed tool call. ``context`` must originate from trusted server-side
    construction (never deserialized from caller-supplied tenant/principal
    fields) — see ``repository()``/``principal()`` in ``backend/app/main.py``
    for the pattern this reuses."""
    tool_id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    arguments: dict = Field(default_factory=dict)
    context: AuthorizationContext
    correlation_id: str | None = None


class ToolResult(Record):
    tool_id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    status: ToolStatus
    correlation_id: str = Field(min_length=1)
    data: dict | None = None
    error_category: str | None = None
    evidence_refs: tuple[str, ...] = ()
    graph_revision: int | None = None
    truncated: bool = False

    @model_validator(mode="after")
    def consistent_payload(self):
        if self.status in (ToolStatus.SUCCESS, ToolStatus.INCOMPLETE):
            if self.data is None:
                raise ValueError("Successful/incomplete results must carry a data payload")
        elif self.data is not None:
            raise ValueError("Non-success results must not carry a data payload")
        return self
