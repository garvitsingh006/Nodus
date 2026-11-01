"""Durable, connector-independent Neo4j projector: claims bounded batches of
outbox events with a lease, applies them to the existing (already-idempotent)
``GraphRepository.ingest``/``retract_record``, and records progress in
``projection_watermarks``. Contains no connector-specific logic — it only
knows the two generic event types ``SyncCoordinator`` ever writes.

Ordering/crash-safety design (GH-004C section 3/4):

- Claiming uses ``FOR UPDATE SKIP LOCKED`` with a time-bounded lease, so
  concurrent workers never double-apply the same event and a crashed
  worker's claim is reclaimable once its lease expires — no in-memory lock.
- Claiming and applying are deliberately separate, short Postgres
  transactions: no transaction is held open across a Neo4j network call.
- Events are applied strictly in ascending ``(aggregate_id,
  aggregate_revision)`` order within a batch. If an aggregate's event fails
  retryably, no later revision for that same aggregate is attempted in this
  pass (never silently skip ahead past an unresolved earlier event) — other
  aggregates in the batch are unaffected.
- An event whose revision is already covered by that aggregate's watermark
  is a stale replay: it is marked delivered without touching Neo4j, so an
  older delayed/replayed event can never regress graph state.
- Retry exhaustion (or any permanent/validation error) quarantines the event
  — an explicit, observable terminal state — rather than dropping it.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from neo4j.exceptions import Neo4jError, ServiceUnavailable

from backend.app.graph.models import SourceRecordInput
from backend.app.graph.repository import GraphRepository
from backend.app.ingestion.postgres_store import PostgresStore

DEFAULT_LEASE_SECONDS = 60
DEFAULT_MAX_ATTEMPTS = 5


class Neo4jProjector:
    def __init__(self, store: PostgresStore, graph: GraphRepository, *, worker_id: str | None = None,
                lease_seconds: int = DEFAULT_LEASE_SECONDS, max_attempts: int = DEFAULT_MAX_ATTEMPTS):
        self.store = store
        self.graph = graph
        self.worker_id = worker_id or f"worker-{uuid.uuid4()}"
        self.lease_seconds = lease_seconds
        self.max_attempts = max_attempts

    def _claim_batch(self, connector_id: str, limit: int) -> list[dict]:
        with self.store.transaction() as cur:
            cur.execute("""
                WITH claimable AS (
                    SELECT event_id FROM outbox_events
                    WHERE tenant_id=%s AND workspace_id=%s AND connector_id=%s
                      AND (status='PENDING' OR (status='CLAIMED' AND lease_expires_at < now()))
                    ORDER BY aggregate_id, aggregate_revision, occurred_at
                    LIMIT %s
                    FOR UPDATE SKIP LOCKED
                )
                UPDATE outbox_events o
                SET status='CLAIMED', claimed_by=%s, claimed_at=now(),
                    lease_expires_at=now() + make_interval(secs => %s)
                FROM claimable
                WHERE o.event_id = claimable.event_id
                RETURNING o.event_id, o.aggregate_id, o.aggregate_revision, o.event_type,
                          o.payload, o.attempts
                """, (self.graph.tenant_id, self.graph.workspace_id, connector_id, limit,
                      self.worker_id, self.lease_seconds))
            return cur.fetchall()

    def _watermark(self, aggregate_id: str) -> int:
        rows = self.store.read(
            "SELECT revision FROM projection_watermarks WHERE projector='neo4j' AND tenant_id=%s AND aggregate_id=%s",
            (self.graph.tenant_id, aggregate_id))
        return rows[0]["revision"] if rows else 0

    def _mark_delivered(self, event_id: str, aggregate_id: str, revision: int) -> None:
        with self.store.transaction() as cur:
            cur.execute("""
                UPDATE outbox_events SET status='DELIVERED', delivered_at=now(),
                    claimed_by=NULL, lease_expires_at=NULL WHERE event_id=%s
                """, (event_id,))
            cur.execute("""
                INSERT INTO projection_watermarks (projector, tenant_id, aggregate_id, revision, last_event_id, updated_at)
                VALUES ('neo4j', %s, %s, %s, %s, now())
                ON CONFLICT (projector, tenant_id, aggregate_id) DO UPDATE SET
                    revision=EXCLUDED.revision, last_event_id=EXCLUDED.last_event_id, updated_at=EXCLUDED.updated_at
                WHERE projection_watermarks.revision < EXCLUDED.revision
                """, (self.graph.tenant_id, aggregate_id, revision, event_id))

    def _mark_superseded(self, event_id: str) -> None:
        with self.store.transaction() as cur:
            cur.execute("""
                UPDATE outbox_events SET status='DELIVERED', delivered_at=now(), claimed_by=NULL,
                    lease_expires_at=NULL, last_error='superseded: watermark already past this revision'
                WHERE event_id=%s
                """, (event_id,))

    def _release(self, event_id: str, error: str) -> None:
        """Retryable failure: return the event to PENDING for a later attempt
        (by this or another worker), recording the attempt and reason."""
        with self.store.transaction() as cur:
            cur.execute("""
                UPDATE outbox_events SET status='PENDING', claimed_by=NULL, claimed_at=NULL,
                    lease_expires_at=NULL, attempts=attempts+1, last_error=%s
                WHERE event_id=%s
                """, (error, event_id))

    def _quarantine(self, event_id: str, error: str) -> None:
        """Terminal, observable failure state: retries exhausted or a
        non-retryable (e.g. ontology/validation) error. Never silently
        dropped — an operator can inspect and clear it."""
        with self.store.transaction() as cur:
            cur.execute("""
                UPDATE outbox_events SET status='QUARANTINED', claimed_by=NULL, claimed_at=NULL,
                    lease_expires_at=NULL, attempts=attempts+1, last_error=%s
                WHERE event_id=%s
                """, (error, event_id))

    def run_once(self, connector_id: str, *, limit: int = 100) -> dict:
        claimed = self._claim_batch(connector_id, limit)
        by_aggregate: dict[str, list[dict]] = {}
        for event in claimed:
            by_aggregate.setdefault(event["aggregate_id"], []).append(event)
        delivered = superseded = quarantined = retried = 0
        last_error: str | None = None
        for aggregate_id, events in by_aggregate.items():
            events.sort(key=lambda e: e["aggregate_revision"])
            watermark = self._watermark(aggregate_id)
            for event in events:
                if event["aggregate_revision"] <= watermark:
                    self._mark_superseded(event["event_id"])
                    superseded += 1
                    continue
                try:
                    if event["event_type"] == "source_record.upserted":
                        self.graph.ingest(SourceRecordInput.model_validate(event["payload"]))
                    elif event["event_type"] == "source_record.retracted":
                        self.graph.retract_record(event["payload"]["source_id"], event["payload"]["record_id"])
                    self._mark_delivered(event["event_id"], aggregate_id, event["aggregate_revision"])
                    watermark = event["aggregate_revision"]
                    delivered += 1
                except (ServiceUnavailable, Neo4jError) as exc:
                    last_error = str(exc)
                    if event["attempts"] + 1 >= self.max_attempts:
                        self._quarantine(event["event_id"], last_error)
                        quarantined += 1
                    else:
                        self._release(event["event_id"], last_error)
                        retried += 1
                    break  # never skip ahead to a later revision while this one is unresolved
                except ValueError as exc:
                    # Ontology/model validation failures are not transient —
                    # retrying the identical payload will never succeed.
                    last_error = str(exc)
                    self._quarantine(event["event_id"], last_error)
                    quarantined += 1
                    break
        return {"claimed": len(claimed), "delivered": delivered, "superseded": superseded,
                "quarantined": quarantined, "retried": retried, "last_error": last_error}
