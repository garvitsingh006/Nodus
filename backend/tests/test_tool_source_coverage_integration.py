"""Integration coverage for source.coverage, against real isolated Neo4j and
PostgreSQL instances."""
from __future__ import annotations

import os
import secrets
import socket
import subprocess
import time
import uuid
from datetime import datetime, timezone

import psycopg
import pytest
from neo4j import GraphDatabase

from backend.app.config import Settings
from backend.app.graph.driver import Neo4jStore
from backend.app.graph.models import EntityInput, SourceRecordInput
from backend.app.graph.repository import GraphRepository
from backend.app.ingestion.connector import ConnectorConfig, ConnectorIdentity
from backend.app.ingestion.postgres_store import PostgresStore
from backend.app.ingestion.sync import SyncCoordinator
from backend.app.security import AuthorizationContext, Permission, Principal
from backend.app.tools.contracts import ToolInvocation, ToolStatus
from backend.app.tools.executor import ToolExecutor
from backend.app.tools.registry import ToolDependencies, ToolRegistry
from backend.app.tools.source_tools import SOURCE_COVERAGE_TOOL_ID, SOURCE_COVERAGE_VERSION, register_source_tools

pytestmark = pytest.mark.integration


def _name() -> str:
    return uuid.uuid4().hex


@pytest.fixture(scope="module")
def isolated_neo4j():
    uri = os.getenv("GRAPH_HACKS_TEST_NEO4J_URI")
    password = os.getenv("GRAPH_HACKS_TEST_NEO4J_PASSWORD")
    if uri and password:
        yield uri, password
        return
    suffix = _name()[:12]
    container = f"gh-neo4j-coverage-{suffix}"
    volume = f"gh-neo4j-coverage-data-{suffix}"
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
        subprocess.run(["docker", "rm", "-f", container],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(["docker", "volume", "rm", volume],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


@pytest.fixture(scope="module")
def isolated_postgres():
    suffix = _name()[:12]
    container = f"gh-postgres-coverage-{suffix}"
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


@pytest.fixture
def graph_store(isolated_neo4j):
    uri, password = isolated_neo4j
    settings = Settings(neo4j_uri=uri, neo4j_user="neo4j", neo4j_password=password)
    store = Neo4jStore(settings)
    store.start()
    try:
        yield store
    finally:
        store.close()


@pytest.fixture
def postgres_store(isolated_postgres):
    store = PostgresStore(isolated_postgres)
    store.start()
    return store


@pytest.fixture
def registry():
    reg = ToolRegistry()
    register_source_tools(reg)
    return reg


def _context(tenant: str, workspace: str) -> AuthorizationContext:
    permissions = frozenset({Permission.READ_GRAPH, Permission.READ_EVIDENCE})
    user = Principal(subject="reader", tenant_id=tenant, workspaces=(workspace,), permissions=permissions)
    return AuthorizationContext(principal=user, tenant_id=tenant, workspace_id=workspace, task_id="t1",
                               permissions=permissions)


def _invoke(executor, arguments, context):
    return executor.execute(ToolInvocation(tool_id=SOURCE_COVERAGE_TOOL_ID, version=SOURCE_COVERAGE_VERSION,
                                           arguments=arguments, context=context))


# 9. Unknown or incomplete source coverage (no persisted metadata at all)
def test_unconfigured_source_without_postgres_is_unknown(graph_store, registry):
    tenant, workspace = _name(), _name()
    executor = ToolExecutor(registry, ToolDependencies(graph_store=graph_store))
    result = _invoke(executor, {"source_id": "nothing"}, _context(tenant, workspace))
    assert result.status == ToolStatus.SUCCESS
    assert result.data["status"] == "UNKNOWN_UNVERIFIED"
    assert result.data["connector_configured"] is None


def test_unconfigured_source_with_postgres_is_unsupported(graph_store, postgres_store, registry):
    tenant, workspace = _name(), _name()
    executor = ToolExecutor(registry, ToolDependencies(graph_store=graph_store, postgres_store=postgres_store))
    result = _invoke(executor, {"source_id": "nothing"}, _context(tenant, workspace))
    assert result.status == ToolStatus.SUCCESS
    assert result.data["status"] == "UNSUPPORTED_NOT_CONFIGURED"
    assert result.data["connector_configured"] is False


def test_configured_but_never_run_is_unknown(graph_store, postgres_store, registry):
    tenant, workspace = _name(), _name()
    coordinator = SyncCoordinator(postgres_store, GraphRepository(graph_store, tenant, workspace, "writer",
                                                                  allow_all=True, can_evidence=True))
    coordinator.save_config(ConnectorConfig(
        tenant_id=tenant, workspace_id=workspace,
        identity=ConnectorIdentity(connector_id="conn-1", source_id="jira", kind="jira")))
    executor = ToolExecutor(registry, ToolDependencies(graph_store=graph_store, postgres_store=postgres_store))
    result = _invoke(executor, {"source_id": "jira"}, _context(tenant, workspace))
    assert result.status == ToolStatus.SUCCESS
    assert result.data["status"] == "UNKNOWN_UNVERIFIED"
    assert result.data["connector_configured"] is True


def test_configured_and_succeeded_is_available(graph_store, postgres_store, registry):
    tenant, workspace = _name(), _name()
    repo = GraphRepository(graph_store, tenant, workspace, "writer", allow_all=True, can_evidence=True)
    repo.ingest(SourceRecordInput(
        source_id="jira", record_id="rec-1", title="rec-1",
        entities=[EntityInput(external_id="t-1", canonical_id="t-1", type="TASK", name="Task 1")],
    ))
    coordinator = SyncCoordinator(postgres_store, repo)
    coordinator.save_config(ConnectorConfig(
        tenant_id=tenant, workspace_id=workspace,
        identity=ConnectorIdentity(connector_id="conn-1", source_id="jira", kind="jira")))
    run_id = str(uuid.uuid4())
    coordinator._start_run(run_id, coordinator.load_config(tenant, workspace, "conn-1"), datetime.now(timezone.utc))
    coordinator._finish_run(run_id, "SUCCEEDED", 1, 1, None, None)
    executor = ToolExecutor(registry, ToolDependencies(graph_store=graph_store, postgres_store=postgres_store))
    result = _invoke(executor, {"source_id": "jira"}, _context(tenant, workspace))
    assert result.status == ToolStatus.SUCCESS
    assert result.data["status"] == "AVAILABLE"
    assert result.data["graph_record_count"] == 1


def test_disabled_connector_is_unsupported(graph_store, postgres_store, registry):
    tenant, workspace = _name(), _name()
    repo = GraphRepository(graph_store, tenant, workspace, "writer", allow_all=True, can_evidence=True)
    coordinator = SyncCoordinator(postgres_store, repo)
    coordinator.save_config(ConnectorConfig(
        tenant_id=tenant, workspace_id=workspace, enabled=False,
        identity=ConnectorIdentity(connector_id="conn-1", source_id="jira", kind="jira")))
    executor = ToolExecutor(registry, ToolDependencies(graph_store=graph_store, postgres_store=postgres_store))
    result = _invoke(executor, {"source_id": "jira"}, _context(tenant, workspace))
    assert result.status == ToolStatus.SUCCESS
    assert result.data["status"] == "UNSUPPORTED_NOT_CONFIGURED"


def test_retryable_failure_is_temporarily_unavailable(graph_store, postgres_store, registry):
    tenant, workspace = _name(), _name()
    repo = GraphRepository(graph_store, tenant, workspace, "writer", allow_all=True, can_evidence=True)
    coordinator = SyncCoordinator(postgres_store, repo)
    coordinator.save_config(ConnectorConfig(
        tenant_id=tenant, workspace_id=workspace,
        identity=ConnectorIdentity(connector_id="conn-1", source_id="jira", kind="jira")))
    run_id = str(uuid.uuid4())
    coordinator._start_run(run_id, coordinator.load_config(tenant, workspace, "conn-1"), datetime.now(timezone.utc))
    coordinator._finish_run(run_id, "FAILED", 0, 0, "rate limited", True)
    executor = ToolExecutor(registry, ToolDependencies(graph_store=graph_store, postgres_store=postgres_store))
    result = _invoke(executor, {"source_id": "jira"}, _context(tenant, workspace))
    assert result.status == ToolStatus.SUCCESS
    assert result.data["status"] == "TEMPORARILY_UNAVAILABLE"
