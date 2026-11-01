"""Bounded reconciliation between PostgreSQL source state, the outbox and
Neo4j applied state (GH-004C section 6).

This never rebuilds a whole tenant/workspace graph and never fabricates a
lost event's payload: an outbox event is the only durable record of *what*
to project, and the raw source content is not retained anywhere else by
design (the connector only ever holds it transiently during a fetch). When
an aggregate is ahead of its projection watermark with no outbox event left
to deliver it, that is unrecoverable from inside this system and is reported
as an explicit, unresolved divergence rather than silently re-derived from
the mutable ``source_record_state`` row.
"""
from __future__ import annotations

from backend.app.graph.repository import GraphRepository, scoped_key
from backend.app.ingestion.postgres_store import PostgresStore
from backend.app.ingestion.projector import Neo4jProjector


def reconcile(store: PostgresStore, graph: GraphRepository, connector_id: str, *,
             worker_id: str | None = None, max_batches: int = 10, batch_size: int = 100) -> dict:
    """Drains any safely-recoverable pending/reclaimable work for one
    connector (abandoned leases are reclaimed automatically by the
    projector's own claim query), then reports any divergence it could not
    repair. Never touches a record outside this connector's own aggregates.
    """
    projector = Neo4jProjector(store, graph, worker_id=worker_id)
    delivered = superseded = quarantined = retried = 0
    for _ in range(max_batches):
        report = projector.run_once(connector_id, limit=batch_size)
        delivered += report["delivered"]
        superseded += report["superseded"]
        quarantined += report["quarantined"]
        retried += report["retried"]
        if report["claimed"] == 0:
            break

    inconsistent_claims = store.read("""
        SELECT event_id FROM outbox_events
        WHERE tenant_id=%s AND workspace_id=%s AND connector_id=%s
          AND status='DELIVERED' AND claimed_by IS NOT NULL
        """, (graph.tenant_id, graph.workspace_id, connector_id))
    if inconsistent_claims:
        with store.transaction() as cur:
            cur.execute("""
                UPDATE outbox_events SET claimed_by=NULL, claimed_at=NULL, lease_expires_at=NULL
                WHERE tenant_id=%s AND workspace_id=%s AND connector_id=%s
                  AND status='DELIVERED' AND claimed_by IS NOT NULL
                """, (graph.tenant_id, graph.workspace_id, connector_id))

    quarantined_events = store.read("""
        SELECT event_id, aggregate_id, aggregate_revision, event_type, attempts, last_error
        FROM outbox_events
        WHERE tenant_id=%s AND workspace_id=%s AND connector_id=%s AND status='QUARANTINED'
        ORDER BY aggregate_id, aggregate_revision
        """, (graph.tenant_id, graph.workspace_id, connector_id))

    # An aggregate whose Postgres-recorded revision is ahead of the Neo4j
    # projection watermark, with nothing left pending/claimed/quarantined to
    # explain the gap, is unresolvable divergence: the event that would have
    # carried that change is simply gone.
    state_rows = store.read("""
        SELECT source_id, record_id, revision FROM source_record_state s
        WHERE tenant_id=%s AND workspace_id=%s
          AND EXISTS (SELECT 1 FROM connector_configs c
                      WHERE c.tenant_id=s.tenant_id AND c.workspace_id=s.workspace_id
                        AND c.connector_id=%s AND c.source_id=s.source_id)
        """, (graph.tenant_id, graph.workspace_id, connector_id))
    unresolved: list[dict] = []
    for row in state_rows:
        aggregate_id = scoped_key(graph.tenant_id, graph.workspace_id, row["source_id"], row["record_id"])
        watermark_rows = store.read(
            "SELECT revision FROM projection_watermarks WHERE projector='neo4j' AND tenant_id=%s AND aggregate_id=%s",
            (graph.tenant_id, aggregate_id))
        watermark = watermark_rows[0]["revision"] if watermark_rows else 0
        if row["revision"] <= watermark:
            continue
        pending_rows = store.read("""
            SELECT 1 FROM outbox_events
            WHERE tenant_id=%s AND aggregate_id=%s AND status IN ('PENDING', 'CLAIMED', 'QUARANTINED')
              AND aggregate_revision > %s
            """, (graph.tenant_id, aggregate_id, watermark))
        if not pending_rows:
            unresolved.append({"source_id": row["source_id"], "record_id": row["record_id"],
                              "source_revision": row["revision"], "projected_revision": watermark,
                              "reason": "No outbox event remains to carry this aggregate to its current "
                                        "source revision; cannot be safely repaired without inventing data."})

    return {
        "delivered": delivered, "superseded": superseded, "quarantined": quarantined, "retried": retried,
        "cleaned_inconsistent_claims": len(inconsistent_claims),
        "quarantined_events": quarantined_events,
        "unresolved_divergence": unresolved,
    }
