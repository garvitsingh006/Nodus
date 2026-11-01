from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

from backend.app.graph.models import EntityInput, EntityType, SourceRecordInput
from backend.app.graph.repository import scoped_key
from backend.app.ingestion.connector import ConnectorConfig, ConnectorIdentity
from backend.app.ingestion.reconciliation import reconcile
from backend.app.ingestion.sync import SyncCoordinator
from backend.tests.test_ingestion_sync import graph, isolated_neo4j, isolated_postgres, store  # noqa: F401


def _name() -> str:
    return uuid.uuid4().hex


def config(tenant: str, workspace: str, connector_id: str) -> ConnectorConfig:
    return ConnectorConfig(tenant_id=tenant, workspace_id=workspace,
                           identity=ConnectorIdentity(connector_id=connector_id, source_id="rec-src", kind="test"))


def record(record_id: str, entity_id: str, name: str) -> SourceRecordInput:
    return SourceRecordInput(source_id="rec-src", record_id=record_id, title=record_id,
                             entities=[EntityInput(external_id=entity_id, type=EntityType.SERVICE, name=name)])


def test_reconciliation_delivers_safely_recoverable_pending_events(store, graph):
    cfg = config(graph.tenant_id, graph.workspace_id, "rec-happy")
    coordinator = SyncCoordinator(store, graph)
    entity_id = _name()
    coordinator.save_config(cfg)
    coordinator._persist_upsert(cfg, record("r1", entity_id, "Recovered"))
    assert graph.get_entity(f"rec-src::{entity_id}") is None  # not delivered yet

    report = reconcile(store, graph, cfg.identity.connector_id)
    assert report["delivered"] == 1
    assert report["unresolved_divergence"] == []
    assert graph.get_entity(f"rec-src::{entity_id}").name == "Recovered"


def test_reconciliation_reports_unresolved_divergence_without_fabricating_a_fix(store, graph):
    cfg = config(graph.tenant_id, graph.workspace_id, "rec-lost")
    coordinator = SyncCoordinator(store, graph)
    entity_id = _name()
    coordinator.save_config(cfg)
    coordinator._persist_upsert(cfg, record("r1", entity_id, "Orphaned"))
    aggregate_id = scoped_key(graph.tenant_id, graph.workspace_id, "rec-src", "r1")

    # Simulate catastrophic event loss: the outbox row is gone, but
    # source_record_state (Postgres) still records that revision 1 exists.
    with store.transaction() as cur:
        cur.execute("DELETE FROM outbox_events WHERE aggregate_id=%s", (aggregate_id,))

    report = reconcile(store, graph, cfg.identity.connector_id)
    assert report["delivered"] == 0
    assert len(report["unresolved_divergence"]) == 1
    divergence = report["unresolved_divergence"][0]
    assert divergence["source_id"] == "rec-src" and divergence["record_id"] == "r1"
    assert divergence["source_revision"] == 1 and divergence["projected_revision"] == 0
    assert graph.get_entity(f"rec-src::{entity_id}") is None  # never fabricated


def test_reconciliation_cleans_up_inconsistent_delivered_claim_metadata(store, graph):
    cfg = config(graph.tenant_id, graph.workspace_id, "rec-cleanup")
    coordinator = SyncCoordinator(store, graph)
    entity_id = _name()
    coordinator.save_config(cfg)
    coordinator._persist_upsert(cfg, record("r1", entity_id, "Clean"))
    reconcile(store, graph, cfg.identity.connector_id)

    # Simulate a stray claim left on an already-delivered row (should never
    # happen through normal code paths, but reconciliation must notice and
    # clean up this metadata inconsistency without touching graph facts).
    with store.transaction() as cur:
        cur.execute("""
            UPDATE outbox_events SET claimed_by='ghost-worker', lease_expires_at=now()
            WHERE connector_id=%s AND status='DELIVERED'
            """, (cfg.identity.connector_id,))

    report = reconcile(store, graph, cfg.identity.connector_id)
    assert report["cleaned_inconsistent_claims"] == 1
    remaining = store.read(
        "SELECT claimed_by FROM outbox_events WHERE connector_id=%s AND status='DELIVERED'",
        (cfg.identity.connector_id,))
    assert remaining[0]["claimed_by"] is None
    assert graph.get_entity(f"rec-src::{entity_id}").name == "Clean"  # untouched
