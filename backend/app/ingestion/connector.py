"""Reusable connector contract: fetching is deliberately separate from
normalization (``backend.app.ingestion.*`` normalizers), persistence
(``backend.app.ingestion.postgres_store``) and graph projection
(``backend.app.graph.repository.GraphRepository``). A connector only knows how
to authenticate, paginate and classify its own errors; it never writes to
Postgres or Neo4j directly.
"""
from __future__ import annotations

from enum import StrEnum
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field

from backend.app.graph.models import SourceRecordInput


class SourceCoverage(StrEnum):
    """How completely a fetched page represents the source's current state.

    Only ``COMPLETE`` would ever justify inferring a deletion from absence,
    and this framework never does that inference anyway (see
    ``SyncCoordinator`` — retraction is always explicit, never diffed).
    """
    COMPLETE = "complete"
    PARTIAL = "partial"
    UNKNOWN = "unknown"


class ErrorClass(StrEnum):
    RETRYABLE = "retryable"
    PERMANENT = "permanent"


class ConnectorError(Exception):
    """Raised by a connector (or the coordinator wrapping one) with an
    explicit retryable/permanent classification, so the caller never has to
    guess from an arbitrary exception type."""

    def __init__(self, message: str, classification: ErrorClass):
        super().__init__(message)
        self.classification = classification


def retryable(message: str) -> ConnectorError:
    return ConnectorError(message, ErrorClass.RETRYABLE)


def permanent(message: str) -> ConnectorError:
    return ConnectorError(message, ErrorClass.PERMANENT)


class ConnectorIdentity(BaseModel):
    model_config = ConfigDict(frozen=True)
    connector_id: str = Field(min_length=1, max_length=200)
    source_id: str = Field(min_length=1, max_length=200)
    kind: str = Field(min_length=1, max_length=100)


class ConnectorCapabilities(BaseModel):
    model_config = ConfigDict(frozen=True)
    supports_incremental: bool = False
    supports_deletion_detection: bool = False
    paginated: bool = True


class ConnectorConfig(BaseModel):
    """Tenant/workspace-specific configuration for one connector instance.
    ``settings`` is connector-specific (API base URL, scopes, etc.) and is
    never interpreted by the graph core."""
    model_config = ConfigDict(frozen=True)
    tenant_id: str = Field(min_length=1)
    workspace_id: str = Field(min_length=1)
    identity: ConnectorIdentity
    settings: dict = Field(default_factory=dict)
    enabled: bool = True


class FetchPage(BaseModel):
    """One bounded page of a connector's output.

    ``retracted_record_ids`` is the only sanctioned way to withdraw a source
    record — the coordinator never infers a deletion merely because a record
    is absent from a page (partial/incomplete enumerations must never be
    treated as evidence of deletion).
    """
    model_config = ConfigDict(frozen=True)
    records: tuple[SourceRecordInput, ...] = ()
    retracted_record_ids: tuple[str, ...] = ()
    next_cursor: str | None = None
    has_more: bool = False
    coverage: SourceCoverage = SourceCoverage.PARTIAL


class Connector(Protocol):
    """A connector fetches; it never persists or projects."""

    def identity(self) -> ConnectorIdentity: ...

    def capabilities(self) -> ConnectorCapabilities: ...

    def fetch(self, config: ConnectorConfig, cursor: str | None) -> FetchPage:
        """Fetch one bounded page starting at ``cursor`` (``None`` means
        start of the incremental stream). Raise ``ConnectorError`` with an
        explicit classification for any failure; never raise a bare
        exception for an expected source-side condition (rate limit, auth
        failure, invalid/expired cursor, source unavailable)."""
        ...
