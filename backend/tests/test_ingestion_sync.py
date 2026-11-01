from __future__ import annotations

import os
import secrets
import socket
import subprocess
import time
import uuid

import psycopg
import pytest
from neo4j import GraphDatabase
from neo4j.exceptions import ServiceUnavailable
from pydantic import ValidationError

from backend.app.config import Settings
from backend.app.graph.driver import Neo4jStore
from backend.app.graph.models import EntityInput, EntityType, RelationInput, RelationType, SourceRecordInput
from backend.app.graph.repository import GraphRepository
from backend.app.ingestion.connector import (
    ConnectorCapabilities, ConnectorConfig, ConnectorError, ConnectorIdentity, ErrorClass, FetchPage,
    SourceCoverage, permanent,
)
from backend.app.ingestion.postgres_store import PostgresStore
from backend.app.ingestion.sync import SyncCoordinator


def _name() -> str:
    return uuid.uuid4().hex


@pytest.fixture(scope="session")
def isolated_postgres():
    suffix = _name()[:12]
    container = f"gh-postgres-test-{suffix}"
    password = secrets.token_urlsafe(24)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    dsn = f"postgresql://postgres:{password}@127.0.0.1:{port}/postgres"
    try:
        subprocess.run(["docker", "run", "-d", "--name", container,
                        "-p", f"127.0.0.1:{port}:5432",
                        "-e", f"POSTGRES_PASSWORD={password}",
                        "postgres:16.10-alpine"], check=True, stdout=subprocess.DEVNULL)
        for _ in range(180):  # bounded retries: generous for a loaded Docker daemon
            try:
                with psycopg.connect(dsn, connect_timeout=2):
                    break
            except Exception:
                time.sleep(1)
        else:
            pytest.fail("Isolated PostgreSQL did not become ready")
        yield dsn
    finally:
        subprocess.run(["docker", "rm", "-f", container], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


@pytest.fixture(scope="session")
def isolated_neo4j():
    uri = os.getenv("GRAPH_HACKS_TEST_NEO4J_URI")
    password = os.getenv("GRAPH_HACKS_TEST_NEO4J_PASSWORD")
    if uri and password:
        yield uri, password
        return
    suffix = _name()[:12]
    container = f"gh-neo4j-sync-test-{suffix}"
    volume = f"gh-neo4j-sync-test-data-{suffix}"
    password = secrets.token_urlsafe(24)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    uri = f"bolt://127.0.0.1:{port}"
    try:
        subprocess.run(["docker", "run", "-d", "--name", container,
                        "-p", f"127.0.0.1:{port}:7687",
                        "-e", f"NEO4J_AUTH=neo4j/{password}",
                        "-v", f"{volume}:/data", "neo4j:5.26.32-community"],
                       check=True, stdout=subprocess.DEVNULL)
        driver = GraphDatabase.driver(uri, auth=("neo4j", password))
        for _ in range(180):  # bounded retries: generous for a loaded Docker daemon
            try:
                driver.verify_connectivity()
                break
            except Exception:
                time.sleep(1)
        else:
            pytest.fail("Isolated Neo4j did not become ready")
        driver.close()
        yield uri, password
    finally:
        subprocess.run(["docker", "rm", "-f", container], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(["docker", "volume", "rm", volume], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


@pytest.fixture
def store(isolated_postgres):
    instance = PostgresStore(isolated_postgres)
    instance.start()
    yield instance


@pytest.fixture
def graph(isolated_neo4j):
    uri, password = isolated_neo4j
    neo4j_store = Neo4jStore(Settings(neo4j_uri=uri, neo4j_user="neo4j", neo4j_password=password))
    neo4j_store.start()
    tenant, workspace = _name(), _name()
    repo = GraphRepository(neo4j_store, tenant, workspace, "sync-coordinator", allow_all=True)
    yield repo
    neo4j_store.close()


def identity(source_id: str = "fake", connector_id: str | None = None) -> ConnectorIdentity:
    return ConnectorIdentity(connector_id=connector_id or f"connector-{_name()}", source_id=source_id, kind="fake")


def config(tenant: str, workspace: str, source_id: str = "fake", connector_id: str | None = None) -> ConnectorConfig:
    return ConnectorConfig(tenant_id=tenant, workspace_id=workspace, identity=identity(source_id, connector_id))


def record(source_id: str, record_id: str, entity_id: str, name: str) -> SourceRecordInput:
    return SourceRecordInput(source_id=source_id, record_id=record_id, title=record_id,
                             entities=[EntityInput(external_id=entity_id, type=EntityType.TASK, name=name)])


class FakeConnector:
    """Pages addressed by an integer-string cursor (None == page 0). Errors
    scheduled for a specific cursor fire once, then are consumed (so a retry
    with the same cursor succeeds) unless ``persistent`` is set."""

    def __init__(self, pages: list[FetchPage], errors: dict[str | None, ConnectorError] | None = None,
                persistent_errors: bool = False):
        self.pages = pages
        self.errors = dict(errors or {})
        self.persistent_errors = persistent_errors
        self.calls: list[str | None] = []

    def identity(self) -> ConnectorIdentity:
        return identity()

    def capabilities(self) -> ConnectorCapabilities:
        return ConnectorCapabilities(supports_incremental=True, supports_deletion_detection=True)

    def fetch(self, cfg: ConnectorConfig, cursor: str | None) -> FetchPage:
        self.calls.append(cursor)
        error = self.errors.get(cursor) if self.persistent_errors else self.errors.pop(cursor, None)
        if error:
            raise error
        index = int(cursor) if cursor is not None else 0
        return self.pages[index]


def page(records=(), retracted=(), next_cursor=None, has_more=False, coverage=SourceCoverage.PARTIAL):
    return FetchPage(records=tuple(records), retracted_record_ids=tuple(retracted),
                     next_cursor=next_cursor, has_more=has_more, coverage=coverage)


def test_connector_and_envelope_contract_validation():
    with pytest.raises(ValidationError):
        ConnectorIdentity(connector_id="", source_id="s", kind="k")
    with pytest.raises(ValidationError):
        ConnectorConfig(tenant_id="", workspace_id="w", identity=identity())
    FetchPage()  # defaults are valid: empty page, partial coverage, no pagination
    with pytest.raises(ValidationError):
        RelationInput(source_external_id="a", target_external_id="b", type=RelationType.DEPENDS_ON,
                     origin="inferred")  # missing derivation_rule_version, same rule as the graph layer


def test_tenant_specific_configuration_isolation(store):
    tenant_a, tenant_b, workspace = _name(), _name(), _name()
    coordinator = SyncCoordinator(store, graph=None)
    cfg_a = config(tenant_a, workspace, connector_id="shared-id")
    cfg_b = config(tenant_b, workspace, connector_id="shared-id", source_id="other-source")
    coordinator.save_config(cfg_a)
    coordinator.save_config(cfg_b)
    loaded_a = coordinator.load_config(tenant_a, workspace, "shared-id")
    loaded_b = coordinator.load_config(tenant_b, workspace, "shared-id")
    assert loaded_a.identity.source_id == "fake" and loaded_b.identity.source_id == "other-source"
    assert coordinator.load_config(_name(), workspace, "shared-id") is None


def test_duplicate_ingestion_is_idempotent_and_checkpoint_persists_across_runs(store, graph):
    tenant, workspace = graph.tenant_id, graph.workspace_id
    cfg = config(tenant, workspace)
    coordinator = SyncCoordinator(store, graph)
    entity_id = _name()
    connector = FakeConnector([page([record("fake", "r1", entity_id, "Original")])])

    first = coordinator.run(connector, cfg)
    assert first["records_seen"] == 1 and first["records_changed"] == 1
    second = coordinator.run(connector, cfg)
    assert second["records_changed"] == 0  # byte-identical replay is a no-op
    outbox_rows = store.read("SELECT count(*) AS n FROM outbox_events WHERE aggregate_revision=1")
    assert outbox_rows[0]["n"] == 1  # one event, not duplicated by the second run

    assert connector.calls == [None, None]
    assert graph.get_entity(f"fake::{entity_id}").name == "Original"


def test_transactional_rollback_leaves_no_partial_state(store):
    with pytest.raises(psycopg.Error):
        with store.transaction() as cur:
            cur.execute("INSERT INTO sync_checkpoints (tenant_id, workspace_id, connector_id, cursor) "
                       "VALUES ('t','w','c','x')")
            cur.execute("SELECT 1/0")  # force a mid-transaction failure
    rows = store.read("SELECT * FROM sync_checkpoints WHERE tenant_id='t' AND connector_id='c'")
    assert rows == []  # the whole transaction, including the first statement, rolled back


def test_failed_graph_delivery_leaves_outbox_pending_and_retry_delivers(store, graph, monkeypatch):
    tenant, workspace = graph.tenant_id, graph.workspace_id
    cfg = config(tenant, workspace)
    coordinator = SyncCoordinator(store, graph)
    entity_id = _name()
    connector = FakeConnector([page([record("fake", "r1", entity_id, "Delivered-later")])])

    original_ingest = graph.ingest
    monkeypatch.setattr(graph, "ingest", lambda *_: (_ for _ in ()).throw(ServiceUnavailable("down")))
    with pytest.raises(ConnectorError) as excinfo:
        coordinator.run(connector, cfg)
    assert excinfo.value.classification == ErrorClass.RETRYABLE
    runs = store.read("SELECT status, retryable FROM sync_runs WHERE connector_id=%s", (cfg.identity.connector_id,))
    assert runs[0]["status"] == "FAILED" and runs[0]["retryable"] is True
    pending = store.read("SELECT status FROM outbox_events WHERE connector_id=%s", (cfg.identity.connector_id,))
    assert pending[0]["status"] == "PENDING"
    assert graph.get_entity(f"fake::{entity_id}") is None  # never reached Neo4j

    monkeypatch.setattr(graph, "ingest", original_ingest)
    connector.calls.clear()
    connector.pages = [page([], has_more=False)]  # nothing new to fetch; only delivery should matter
    result = coordinator.run(connector, cfg)
    assert result["status"] == "SUCCEEDED"
    assert graph.get_entity(f"fake::{entity_id}").name == "Delivered-later"
    delivered = store.read("SELECT status FROM outbox_events WHERE connector_id=%s", (cfg.identity.connector_id,))
    assert delivered[0]["status"] == "DELIVERED"


def test_interrupted_run_resumes_from_checkpoint_without_reprocessing(store, graph):
    tenant, workspace = graph.tenant_id, graph.workspace_id
    cfg = config(tenant, workspace)
    coordinator = SyncCoordinator(store, graph)
    first_id, second_id = _name(), _name()
    pages = [
        page([record("fake", "r1", first_id, "Page one")], next_cursor="1", has_more=True),
        page([record("fake", "r2", second_id, "Page two")], next_cursor=None, has_more=False),
    ]
    connector = FakeConnector(pages, errors={"1": permanent("source outage on page two")})

    with pytest.raises(ConnectorError):
        coordinator.run(connector, cfg)
    assert graph.get_entity(f"fake::{first_id}") is not None
    assert graph.get_entity(f"fake::{second_id}") is None

    retry = coordinator.run(connector, cfg)
    assert retry["records_seen"] == 1 and retry["records_changed"] == 1  # only page two reprocessed
    assert connector.calls == [None, "1", "1"]  # page one never refetched on retry
    assert graph.get_entity(f"fake::{second_id}").name == "Page two"


def test_partial_enumeration_never_causes_false_deletion(store, graph):
    tenant, workspace = graph.tenant_id, graph.workspace_id
    cfg = config(tenant, workspace)
    coordinator = SyncCoordinator(store, graph)
    kept_id = _name()
    coordinator.run(FakeConnector([page([record("fake", "keep", kept_id, "Kept")])]), cfg)
    assert graph.get_entity(f"fake::{kept_id}") is not None

    # A second, unrelated page never mentions "keep" and asserts no retraction;
    # absence must never be interpreted as deletion.
    unrelated_id = _name()
    coordinator.run(FakeConnector([page([record("fake", "other", unrelated_id, "Other")],
                                        coverage=SourceCoverage.PARTIAL)]), cfg)
    assert graph.get_entity(f"fake::{kept_id}") is not None
    assert graph.get_entity(f"fake::{unrelated_id}") is not None


def test_unauthorized_source_data_is_rejected(store, graph):
    tenant, workspace = graph.tenant_id, graph.workspace_id
    cfg = config(tenant, workspace, source_id="authorized")
    coordinator = SyncCoordinator(store, graph)
    smuggled_id = _name()
    connector = FakeConnector([page([record("unauthorized", "r1", smuggled_id, "Smuggled")])])

    with pytest.raises(ConnectorError) as excinfo:
        coordinator.run(connector, cfg)
    assert excinfo.value.classification == ErrorClass.PERMANENT
    assert graph.get_entity(f"unauthorized::{smuggled_id}") is None
    state = store.read(
        "SELECT * FROM source_record_state WHERE tenant_id=%s AND workspace_id=%s AND record_id='r1'",
        (cfg.tenant_id, cfg.workspace_id))
    assert state == []  # rejected before any persistence
    runs = store.read("SELECT status, retryable FROM sync_runs WHERE connector_id=%s", (cfg.identity.connector_id,))
    assert runs[0]["status"] == "FAILED" and runs[0]["retryable"] is False


def test_source_specific_retraction_through_sync(store, graph):
    tenant, workspace = graph.tenant_id, graph.workspace_id
    cfg = config(tenant, workspace)
    coordinator = SyncCoordinator(store, graph)
    entity_id = _name()
    coordinator.run(FakeConnector([page([record("fake", "r1", entity_id, "Alive")])]), cfg)
    assert graph.get_entity(f"fake::{entity_id}") is not None

    coordinator.run(FakeConnector([page([], retracted=["r1"])]), cfg)
    assert graph.get_entity(f"fake::{entity_id}") is None

    repeat = coordinator.run(FakeConnector([page([], retracted=["r1"])]), cfg)
    assert repeat["records_changed"] == 0  # idempotent repeat retraction
