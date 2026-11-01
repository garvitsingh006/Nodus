"""Connector-driven synchronization: fetch -> durable Postgres state+outbox
(atomic per record) -> deliver pending outbox events through the durable
``Neo4jProjector`` (see ``backend.app.ingestion.projector``), which itself
calls the existing, already-idempotent Neo4j graph API (``GraphRepository``).

The outbox is the durable record of ingestion intent and the boundary
``Neo4jProjector``/``backend.app.ingestion.reconciliation`` are built
against; this coordinator drives delivery inline after each page so the
existing direct graph APIs keep working unchanged, but the same projector can
also run standalone for crash recovery. Delivery is at-least-once and safe to
repeat: the outbox dedup (by idempotency key), ``GraphRepository.ingest``'s
own content-hash check, and the projector's per-aggregate watermark all make
replays a no-op.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

from backend.app.contracts import OutboxEvent
from backend.app.graph.models import SourceRecordInput
from backend.app.graph.repository import GraphRepository, _record_fingerprint, scoped_key
from backend.app.ingestion.connector import (
    Connector, ConnectorConfig, ConnectorError, ConnectorIdentity, ErrorClass, permanent, retryable,
)
from backend.app.ingestion.postgres_store import PostgresStore
from backend.app.ingestion.projector import Neo4jProjector


class SyncCoordinator:
    def __init__(self, store: PostgresStore, graph: GraphRepository):
        self.store = store
        self.graph = graph

    def save_config(self, config: ConnectorConfig) -> None:
        with self.store.transaction() as cur:
            cur.execute("""
                INSERT INTO connector_configs
                    (tenant_id, workspace_id, connector_id, source_id, kind, settings, enabled, updated_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (tenant_id, workspace_id, connector_id) DO UPDATE SET
                    source_id=EXCLUDED.source_id, kind=EXCLUDED.kind, settings=EXCLUDED.settings,
                    enabled=EXCLUDED.enabled, updated_at=EXCLUDED.updated_at
                """, (config.tenant_id, config.workspace_id, config.identity.connector_id,
                      config.identity.source_id, config.identity.kind, json.dumps(config.settings),
                      config.enabled, datetime.now(timezone.utc)))

    def load_config(self, tenant_id: str, workspace_id: str, connector_id: str) -> ConnectorConfig | None:
        rows = self.store.read("""
            SELECT source_id, kind, settings, enabled FROM connector_configs
            WHERE tenant_id=%s AND workspace_id=%s AND connector_id=%s
            """, (tenant_id, workspace_id, connector_id))
        if not rows:
            return None
        row = rows[0]
        return ConnectorConfig(
            tenant_id=tenant_id, workspace_id=workspace_id, enabled=row["enabled"],
            settings=row["settings"],
            identity=ConnectorIdentity(connector_id=connector_id, source_id=row["source_id"], kind=row["kind"]))

    def _checkpoint(self, config: ConnectorConfig) -> str | None:
        rows = self.store.read(
            "SELECT cursor FROM sync_checkpoints WHERE tenant_id=%s AND workspace_id=%s AND connector_id=%s",
            (config.tenant_id, config.workspace_id, config.identity.connector_id))
        return rows[0]["cursor"] if rows else None

    def reset_checkpoint(self, config: ConnectorConfig) -> None:
        """Operator action for an expired/invalid stored cursor: forces the
        next run to restart the incremental stream from scratch."""
        self._save_checkpoint(config, None)

    def _save_checkpoint(self, config: ConnectorConfig, cursor: str | None) -> None:
        with self.store.transaction() as cur:
            cur.execute("""
                INSERT INTO sync_checkpoints (tenant_id, workspace_id, connector_id, cursor, updated_at)
                VALUES (%s,%s,%s,%s,%s)
                ON CONFLICT (tenant_id, workspace_id, connector_id)
                DO UPDATE SET cursor=EXCLUDED.cursor, updated_at=EXCLUDED.updated_at
                """, (config.tenant_id, config.workspace_id, config.identity.connector_id,
                      cursor, datetime.now(timezone.utc)))

    def _start_run(self, run_id: str, config: ConnectorConfig, started_at: datetime) -> None:
        with self.store.transaction() as cur:
            cur.execute("""
                INSERT INTO sync_runs (run_id, tenant_id, workspace_id, connector_id, started_at, status)
                VALUES (%s,%s,%s,%s,%s,'RUNNING')
                """, (run_id, config.tenant_id, config.workspace_id, config.identity.connector_id, started_at))

    def _finish_run(self, run_id: str, status: str, seen: int, changed: int,
                    error: str | None, is_retryable: bool | None) -> None:
        with self.store.transaction() as cur:
            cur.execute("""
                UPDATE sync_runs SET finished_at=%s, status=%s, records_seen=%s, records_changed=%s,
                    error=%s, retryable=%s WHERE run_id=%s
                """, (datetime.now(timezone.utc), status, seen, changed, error, is_retryable, run_id))

    def _persist_upsert(self, config: ConnectorConfig, record: SourceRecordInput) -> bool:
        """Atomically upserts the source-record fingerprint and appends its
        outbox event in one Postgres transaction; returns False for a
        byte-identical replay (no new event, no revision bump)."""
        aggregate_id = scoped_key(config.tenant_id, config.workspace_id, record.source_id, record.record_id)
        content_hash = _record_fingerprint(record)
        with self.store.transaction() as cur:
            cur.execute("""
                SELECT content_hash, revision, active FROM source_record_state
                WHERE tenant_id=%s AND workspace_id=%s AND source_id=%s AND record_id=%s FOR UPDATE
                """, (config.tenant_id, config.workspace_id, record.source_id, record.record_id))
            row = cur.fetchone()
            if row and row["active"] and row["content_hash"] == content_hash:
                return False
            revision = (row["revision"] + 1) if row else 1
            now = datetime.now(timezone.utc)
            cur.execute("""
                INSERT INTO source_record_state
                    (tenant_id, workspace_id, source_id, record_id, content_hash, revision, active, updated_at)
                VALUES (%s,%s,%s,%s,%s,%s,true,%s)
                ON CONFLICT (tenant_id, workspace_id, source_id, record_id) DO UPDATE SET
                    content_hash=EXCLUDED.content_hash, revision=EXCLUDED.revision,
                    active=true, updated_at=EXCLUDED.updated_at
                """, (config.tenant_id, config.workspace_id, record.source_id, record.record_id,
                      content_hash, revision, now))
            event = OutboxEvent(
                event_id=str(uuid.uuid4()), tenant_id=config.tenant_id, aggregate_id=aggregate_id,
                aggregate_revision=revision, event_type="source_record.upserted",
                idempotency_key=scoped_key(aggregate_id, content_hash), occurred_at=now)
            cur.execute("""
                INSERT INTO outbox_events
                    (event_id, tenant_id, workspace_id, connector_id, aggregate_id, aggregate_revision,
                     event_type, idempotency_key, occurred_at, payload, status)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'PENDING')
                ON CONFLICT (tenant_id, idempotency_key) DO NOTHING
                """, (event.event_id, event.tenant_id, config.workspace_id, config.identity.connector_id,
                      event.aggregate_id, event.aggregate_revision, event.event_type, event.idempotency_key,
                      event.occurred_at, json.dumps(record.model_dump(mode="json"))))
        return True

    def _persist_retraction(self, config: ConnectorConfig, record_id: str) -> bool:
        aggregate_id = scoped_key(config.tenant_id, config.workspace_id, config.identity.source_id, record_id)
        with self.store.transaction() as cur:
            cur.execute("""
                SELECT revision, active FROM source_record_state
                WHERE tenant_id=%s AND workspace_id=%s AND source_id=%s AND record_id=%s FOR UPDATE
                """, (config.tenant_id, config.workspace_id, config.identity.source_id, record_id))
            row = cur.fetchone()
            if not row or not row["active"]:
                return False
            revision = row["revision"] + 1
            now = datetime.now(timezone.utc)
            cur.execute("""
                UPDATE source_record_state SET active=false, revision=%s, updated_at=%s
                WHERE tenant_id=%s AND workspace_id=%s AND source_id=%s AND record_id=%s
                """, (revision, now, config.tenant_id, config.workspace_id,
                      config.identity.source_id, record_id))
            event = OutboxEvent(
                event_id=str(uuid.uuid4()), tenant_id=config.tenant_id, aggregate_id=aggregate_id,
                aggregate_revision=revision, event_type="source_record.retracted",
                idempotency_key=scoped_key(aggregate_id, "retracted", str(revision)), occurred_at=now)
            cur.execute("""
                INSERT INTO outbox_events
                    (event_id, tenant_id, workspace_id, connector_id, aggregate_id, aggregate_revision,
                     event_type, idempotency_key, occurred_at, payload, status)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'PENDING')
                ON CONFLICT (tenant_id, idempotency_key) DO NOTHING
                """, (event.event_id, event.tenant_id, config.workspace_id, config.identity.connector_id,
                      event.aggregate_id, event.aggregate_revision, event.event_type, event.idempotency_key,
                      event.occurred_at,
                      json.dumps({"source_id": config.identity.source_id, "record_id": record_id})))
        return True

    def _deliver_pending(self, config: ConnectorConfig) -> None:
        """Drains this connector's pending/reclaimable outbox events through
        the durable ``Neo4jProjector`` (claim-lease, per-aggregate ordering,
        watermark tracking, quarantine on exhausted/permanent failure — see
        ``backend.app.ingestion.projector``). A failure surfacing *during
        this same run* still fails the run immediately (unchanged contract);
        the projector itself may also be run standalone for recovery."""
        report = Neo4jProjector(self.store, self.graph).run_once(config.identity.connector_id, limit=1000)
        if report["quarantined"]:
            raise permanent(f"Graph projection permanently failed for "
                           f"{report['quarantined']} event(s): {report['last_error']}")
        if report["retried"]:
            raise retryable(f"Graph projection failed for {report['retried']} event(s) "
                           f"and will be retried: {report['last_error']}")

    def run(self, connector: Connector, config: ConnectorConfig, *, max_pages: int = 100) -> dict:
        if not config.enabled:
            raise permanent(f"Connector {config.identity.connector_id!r} is disabled")
        run_id = str(uuid.uuid4())
        self._start_run(run_id, config, datetime.now(timezone.utc))
        cursor = self._checkpoint(config)
        seen = changed = 0
        try:
            for _ in range(max_pages):
                page = connector.fetch(config, cursor)
                for record in page.records:
                    if record.source_id != config.identity.source_id:
                        raise permanent(
                            f"Connector {config.identity.connector_id!r} returned a record for "
                            f"unauthorized source {record.source_id!r}")
                for record in page.records:
                    seen += 1
                    if self._persist_upsert(config, record):
                        changed += 1
                for record_id in page.retracted_record_ids:
                    if self._persist_retraction(config, record_id):
                        changed += 1
                self._deliver_pending(config)
                cursor = page.next_cursor
                self._save_checkpoint(config, cursor)
                if not page.has_more:
                    break
            self._finish_run(run_id, "SUCCEEDED", seen, changed, None, None)
            return {"run_id": run_id, "records_seen": seen, "records_changed": changed, "status": "SUCCEEDED"}
        except ConnectorError as exc:
            self._finish_run(run_id, "FAILED", seen, changed, str(exc), exc.classification == ErrorClass.RETRYABLE)
            raise
        except ValueError as exc:
            self._finish_run(run_id, "FAILED", seen, changed, str(exc), False)
            raise permanent(str(exc)) from exc
