from __future__ import annotations

import json
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest
from neo4j.exceptions import ServiceUnavailable

from backend.app.graph.models import EntityInput, EntityType, SourceRecordInput
from backend.app.graph.repository import scoped_key
from backend.app.ingestion.projector import Neo4jProjector
from backend.app.ingestion.sync import SyncCoordinator
from backend.app.ingestion.connector import ConnectorConfig, ConnectorIdentity
from backend.tests.test_ingestion_sync import graph, isolated_neo4j, isolated_postgres, store  # noqa: F401


def _name() -> str:
    return uuid.uuid4().hex


def config(tenant: str, workspace: str, connector_id: str) -> ConnectorConfig:
    return ConnectorConfig(tenant_id=tenant, workspace_id=workspace,
                           identity=ConnectorIdentity(connector_id=connector_id, source_id="proj-src", kind="test"))


def record(entity_id: str, name: str) -> SourceRecordInput:
    return SourceRecordInput(source_id="proj-src", record_id="r1", title="r1",
                             entities=[EntityInput(external_id=entity_id, type=EntityType.SERVICE, name=name)])


def outbox_count(store, connector_id: str, status: str | None = None) -> int:
    sql = "SELECT count(*) AS n FROM outbox_events WHERE connector_id=%s"
    params: tuple = (connector_id,)
    if status:
        sql += " AND status=%s"
        params = (connector_id, status)
    return store.read(sql, params)[0]["n"]


def test_basic_delivery_advances_watermark(store, graph):
    cfg = config(graph.tenant_id, graph.workspace_id, "proj-basic")
    coordinator = SyncCoordinator(store, graph)
    entity_id = _name()
    coordinator._persist_upsert(cfg, record(entity_id, "First"))
    projector = Neo4jProjector(store, graph)
    report = projector.run_once(cfg.identity.connector_id)
    assert report == {"claimed": 1, "delivered": 1, "superseded": 0, "quarantined": 0,
                      "retried": 0, "last_error": None}
    assert graph.get_entity(f"proj-src::{entity_id}").name == "First"
    aggregate_id = scoped_key(graph.tenant_id, graph.workspace_id, "proj-src", "r1")
    watermark = store.read(
        "SELECT revision FROM projection_watermarks WHERE projector='neo4j' AND tenant_id=%s AND aggregate_id=%s",
        (graph.tenant_id, aggregate_id))
    assert watermark[0]["revision"] == 1


def test_expired_claim_is_reclaimed_after_simulated_worker_crash(store, graph):
    cfg = config(graph.tenant_id, graph.workspace_id, "proj-crash")
    coordinator = SyncCoordinator(store, graph)
    entity_id = _name()
    coordinator._persist_upsert(cfg, record(entity_id, "Crashed"))

    dead_worker = Neo4jProjector(store, graph, worker_id="dead-worker", lease_seconds=60)
    claimed = dead_worker._claim_batch(cfg.identity.connector_id, 10)
    assert len(claimed) == 1
    assert outbox_count(store, cfg.identity.connector_id, "CLAIMED") == 1
    # Simulate the worker dying mid-processing: force its lease into the past.
    with store.transaction() as cur:
        cur.execute("UPDATE outbox_events SET lease_expires_at=%s WHERE connector_id=%s",
                   (datetime.now(timezone.utc) - timedelta(seconds=1), cfg.identity.connector_id))

    recovering_worker = Neo4jProjector(store, graph, worker_id="recovering-worker")
    report = recovering_worker.run_once(cfg.identity.connector_id)
    assert report["delivered"] == 1
    assert graph.get_entity(f"proj-src::{entity_id}").name == "Crashed"


def test_retry_then_quarantine_on_persistent_neo4j_failure(store, graph, monkeypatch):
    cfg = config(graph.tenant_id, graph.workspace_id, "proj-quarantine")
    coordinator = SyncCoordinator(store, graph)
    entity_id = _name()
    coordinator._persist_upsert(cfg, record(entity_id, "Flaky"))

    def always_fails(*_args, **_kwargs):
        raise ServiceUnavailable("down")
    monkeypatch.setattr(graph, "ingest", always_fails)

    projector = Neo4jProjector(store, graph, max_attempts=3)
    for _ in range(2):
        report = projector.run_once(cfg.identity.connector_id)
        assert report["retried"] == 1 and report["quarantined"] == 0
    final = projector.run_once(cfg.identity.connector_id)
    assert final["quarantined"] == 1
    assert outbox_count(store, cfg.identity.connector_id, "QUARANTINED") == 1
    assert outbox_count(store, cfg.identity.connector_id, "PENDING") == 0


def test_permanent_validation_error_quarantines_immediately(store, graph):
    cfg = config(graph.tenant_id, graph.workspace_id, "proj-invalid")
    bad_payload = {"source_id": "proj-src", "record_id": "bad", "title": "bad",
                   "entities": [{"external_id": "dup", "type": "SERVICE", "name": "A"},
                               {"external_id": "dup", "type": "TASK", "name": "B"}],
                   "relationships": []}
    with store.transaction() as cur:
        cur.execute("""
            INSERT INTO outbox_events (event_id, tenant_id, workspace_id, connector_id, aggregate_id,
                aggregate_revision, event_type, idempotency_key, occurred_at, payload, status)
            VALUES (%s,%s,%s,%s,%s,1,'source_record.upserted',%s,%s,%s,'PENDING')
            """, (_name(), graph.tenant_id, graph.workspace_id, cfg.identity.connector_id, _name(),
                  _name(), datetime.now(timezone.utc), json.dumps(bad_payload)))
    report = Neo4jProjector(store, graph).run_once(cfg.identity.connector_id)
    assert report["quarantined"] == 1 and report["retried"] == 0
    assert "Duplicate entity" in report["last_error"]


def test_stale_replay_never_regresses_graph_state(store, graph):
    cfg = config(graph.tenant_id, graph.workspace_id, "proj-stale")
    coordinator = SyncCoordinator(store, graph)
    entity_id = _name()
    payload_v1 = record(entity_id, "Version One")
    payload_v2 = record(entity_id, "Version Two")
    assert coordinator._persist_upsert(cfg, payload_v1) is True
    Neo4jProjector(store, graph).run_once(cfg.identity.connector_id)
    assert coordinator._persist_upsert(cfg, payload_v2) is True
    Neo4jProjector(store, graph).run_once(cfg.identity.connector_id)
    assert graph.get_entity(f"proj-src::{entity_id}").name == "Version Two"

    aggregate_id = scoped_key(graph.tenant_id, graph.workspace_id, "proj-src", "r1")
    with store.transaction() as cur:
        cur.execute("""
            INSERT INTO outbox_events (event_id, tenant_id, workspace_id, connector_id, aggregate_id,
                aggregate_revision, event_type, idempotency_key, occurred_at, payload, status)
            VALUES (%s,%s,%s,%s,%s,1,'source_record.upserted',%s,%s,%s,'PENDING')
            """, (_name(), graph.tenant_id, graph.workspace_id, cfg.identity.connector_id, aggregate_id,
                  _name(), datetime.now(timezone.utc), json.dumps(payload_v1.model_dump(mode="json"))))
    report = Neo4jProjector(store, graph).run_once(cfg.identity.connector_id)
    assert report["superseded"] == 1 and report["delivered"] == 0
    assert graph.get_entity(f"proj-src::{entity_id}").name == "Version Two"  # never regressed to v1


def test_concurrent_workers_do_not_double_apply(store, graph):
    cfg = config(graph.tenant_id, graph.workspace_id, "proj-concurrent")
    coordinator = SyncCoordinator(store, graph)
    for index in range(6):
        entity_id = f"{_name()}-{index}"
        aggregate_payload = SourceRecordInput(
            source_id="proj-src", record_id=f"r-{index}", title=f"r-{index}",
            entities=[EntityInput(external_id=entity_id, type=EntityType.SERVICE, name=f"Svc {index}")])
        coordinator._persist_upsert(cfg, aggregate_payload)

    def worker(n):
        return Neo4jProjector(store, graph, worker_id=f"worker-{n}").run_once(cfg.identity.connector_id, limit=10)

    with ThreadPoolExecutor(max_workers=4) as pool:
        reports = list(pool.map(worker, range(4)))
    total_delivered = sum(r["delivered"] for r in reports)
    assert total_delivered == 6
    assert outbox_count(store, cfg.identity.connector_id, "DELIVERED") == 6
    assert outbox_count(store, cfg.identity.connector_id, "PENDING") == 0
    assert outbox_count(store, cfg.identity.connector_id, "CLAIMED") == 0
