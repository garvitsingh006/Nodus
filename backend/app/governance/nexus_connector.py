"""Connector adapter: exposes the pre-built NEXUS ``ImportPlan`` (see
``backend.app.governance.loader``) through the existing ``Connector``
protocol, so ``SyncCoordinator`` can drive it through the same
idempotent/crash-safe outbox+projector pipeline every other connector uses.
"""
from __future__ import annotations

from pathlib import Path

from backend.app.governance.loader import ImportPlan, build_import_plan
from backend.app.graph.models import SourceRecordInput
from backend.app.ingestion.connector import (
    Connector, ConnectorCapabilities, ConnectorConfig, ConnectorIdentity, FetchPage, SourceCoverage,
)

CONNECTOR_KIND = "nexus_file"


class NexusFileConnector(Connector):
    """One instance serves every NEXUS source: the dataset is parsed and
    transformed once (on first ``fetch``) and cached, then filtered by
    ``config.identity.source_id`` on every call."""

    def __init__(self, stage_7a: Path, stage_7b: Path, stage_7c: Path, stage_7d: Path, stage_7e: Path):
        self._paths = (stage_7a, stage_7b, stage_7c, stage_7d, stage_7e)
        self._plan: ImportPlan | None = None

    def _ensure_plan(self) -> ImportPlan:
        if self._plan is None:
            self._plan = build_import_plan(*self._paths)
        return self._plan

    def identity(self) -> ConnectorIdentity:
        return ConnectorIdentity(connector_id="nexus-file-connector", source_id="*", kind=CONNECTOR_KIND)

    def capabilities(self) -> ConnectorCapabilities:
        return ConnectorCapabilities(supports_incremental=False, supports_deletion_detection=False, paginated=False)

    def records_for(self, source_id: str) -> list[SourceRecordInput]:
        return self._ensure_plan().source_records.get(source_id, [])

    def fetch(self, config: ConnectorConfig, cursor: str | None) -> FetchPage:
        records = tuple(self.records_for(config.identity.source_id))
        return FetchPage(records=records, retracted_record_ids=(), next_cursor=None,
                         has_more=False, coverage=SourceCoverage.COMPLETE)
