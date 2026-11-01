"""GH-005C acceptance: full connector-to-tool workflow, concurrent read-only
invocations, source-coverage tenant isolation, and timeout/cancellation
behavior — against real isolated Neo4j and PostgreSQL instances."""
from __future__ import annotations

import os
import secrets
import socket
import subprocess
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor as PyThreadPoolExecutor

import psycopg
import pytest
from neo4j import GraphDatabase
from pydantic import BaseModel

from backend.app.config import Settings
from backend.app.graph.driver import Neo4jStore
from backend.app.graph.models import EntityInput, SourceRecordInput
from backend.app.graph.repository import GraphRepository
from backend.app.ingestion.connector import (
    ConnectorCapabilities, ConnectorConfig, ConnectorIdentity, FetchPage,
)
from backend.app.ingestion.postgres_store import PostgresStore
from backend.app.ingestion.sync import SyncCoordinator
from backend.app.security import AuthorizationContext, Permission, Principal
from backend.app.tools import register_all_tools
from backend.app.tools.contracts import ExecutionLimits, ToolInvocation, ToolStatus
from backend.app.tools.evidence_tools import EVIDENCE_LOOKUP_TOOL_ID, EVIDENCE_LOOKUP_VERSION
from backend.app.tools.executor import ToolExecutor
from backend.app.tools.graph_tools import GRAPH_ENTITY_LOOKUP_TOOL_ID, GRAPH_ENTITY_LOOKUP_VERSION
from backend.app.tools.registry import SideEffect, ToolDependencies, ToolRegistry, ToolSpec
from backend.app.tools.source_tools import SOURCE_COVERAGE_TOOL_ID, SOURCE_COVERAGE_VERSION

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
    container = f"gh-neo4j-reliability-{suffix}"
    volume = f"gh-neo4j-reliability-data-{suffix}"
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
    container = f"gh-postgres-reliability-{suffix}"
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
    register_all_tools(reg)
    return reg


def _context(tenant: str, workspace: str) -> AuthorizationContext:
    permissions = frozenset({Permission.READ_GRAPH, Permission.READ_EVIDENCE})
    user = Principal(subject="reader", tenant_id=tenant, workspaces=(workspace,), permissions=permissions)
    return AuthorizationContext(principal=user, tenant_id=tenant, workspace_id=workspace, task_id="t1",
                               permissions=permissions)


def _invoke(executor, tool_id, version, arguments, context):
    return executor.execute(ToolInvocation(tool_id=tool_id, version=version, arguments=arguments, context=context))


class _FakeConnector:
    """Minimal ``Connector`` implementation driving records through the real
    ``SyncCoordinator`` -> outbox -> ``Neo4jProjector`` path, exactly as a
    production connector would."""

    def __init__(self, source_id: str, pages: list[FetchPage]):
        self._source_id = source_id
        self._pages = pages
        self._index = 0

    def identity(self) -> ConnectorIdentity:
        return ConnectorIdentity(connector_id="fake-conn", source_id=self._source_id, kind="fake")

    def capabilities(self) -> ConnectorCapabilities:
        return ConnectorCapabilities(supports_incremental=True, supports_deletion_detection=True)

    def fetch(self, config: ConnectorConfig, cursor: str | None) -> FetchPage:
        page = self._pages[self._index]
        self._index += 1
        return page


# 6. Full-workflow integration: connector -> outbox -> Neo4j projection -> tools, including a retraction
def test_full_connector_to_tool_workflow_reflects_retraction(graph_store, postgres_store, registry):
    tenant, workspace = _name(), _name()
    repo = GraphRepository(graph_store, tenant, workspace, "writer", allow_all=True, can_evidence=True)
    coordinator = SyncCoordinator(postgres_store, repo)
    config = ConnectorConfig(tenant_id=tenant, workspace_id=workspace,
                             identity=ConnectorIdentity(connector_id="fake-conn", source_id="fake-src", kind="fake"))
    coordinator.save_config(config)

    record = SourceRecordInput(
        source_id="fake-src", record_id="rec-1", title="rec-1",
        entities=[EntityInput(external_id="alice", canonical_id="alice", type="PERSON", name="Alice")],
    )
    connector = _FakeConnector("fake-src", [
        FetchPage(records=(record,), next_cursor="cursor-1", has_more=False),
    ])
    run_report = coordinator.run(connector, config)
    assert run_report["status"] == "SUCCEEDED"
    assert run_report["records_changed"] == 1

    executor = ToolExecutor(registry, ToolDependencies(graph_store=graph_store, postgres_store=postgres_store))
    context = _context(tenant, workspace)

    lookup = _invoke(executor, GRAPH_ENTITY_LOOKUP_TOOL_ID, GRAPH_ENTITY_LOOKUP_VERSION,
                     {"entity_id": "alice"}, context)
    assert lookup.status == ToolStatus.SUCCESS
    assert lookup.data["entity"]["name"] == "Alice"

    evidence = _invoke(executor, EVIDENCE_LOOKUP_TOOL_ID, EVIDENCE_LOOKUP_VERSION,
                       {"entity_id": "alice"}, context)
    assert evidence.status == ToolStatus.SUCCESS
    assert evidence.data["items"][0]["source_id"] == "fake-src"

    coverage = _invoke(executor, SOURCE_COVERAGE_TOOL_ID, SOURCE_COVERAGE_VERSION,
                       {"source_id": "fake-src"}, context)
    assert coverage.status == ToolStatus.SUCCESS
    assert coverage.data["status"] == "AVAILABLE"

    # Now retract through the same connector-driven path and confirm tool results change.
    connector_retract = _FakeConnector("fake-src", [
        FetchPage(retracted_record_ids=("rec-1",), next_cursor="cursor-2", has_more=False),
    ])
    retract_report = coordinator.run(connector_retract, coordinator.load_config(tenant, workspace, "fake-conn"))
    assert retract_report["status"] == "SUCCEEDED"
    assert retract_report["records_changed"] == 1

    lookup_after = _invoke(executor, GRAPH_ENTITY_LOOKUP_TOOL_ID, GRAPH_ENTITY_LOOKUP_VERSION,
                           {"entity_id": "alice"}, context)
    assert lookup_after.status == ToolStatus.NOT_FOUND

    lookup_history = _invoke(executor, GRAPH_ENTITY_LOOKUP_TOOL_ID, GRAPH_ENTITY_LOOKUP_VERSION,
                             {"entity_id": "alice", "include_history": True}, context)
    assert lookup_history.status == ToolStatus.SUCCESS
    assert lookup_history.data["entity"]["active"] is False


# G8: concurrent read-only invocations preserve tenant isolation and correctness
def test_concurrent_invocations_preserve_tenant_isolation(graph_store, registry):
    tenant_a, tenant_b, workspace = _name(), _name(), _name()
    repo_a = GraphRepository(graph_store, tenant_a, workspace, "writer", allow_all=True, can_evidence=True)
    repo_a.ingest(SourceRecordInput(
        source_id="src", record_id="rec-1", title="rec-1",
        entities=[EntityInput(external_id="shared", canonical_id="shared", type="PERSON", name="Tenant A Name")],
    ))
    repo_b = GraphRepository(graph_store, tenant_b, workspace, "writer", allow_all=True, can_evidence=True)
    repo_b.ingest(SourceRecordInput(
        source_id="src", record_id="rec-1", title="rec-1",
        entities=[EntityInput(external_id="shared", canonical_id="shared", type="PERSON", name="Tenant B Name")],
    ))
    executor = ToolExecutor(registry, ToolDependencies(graph_store=graph_store))
    context_a, context_b = _context(tenant_a, workspace), _context(tenant_b, workspace)

    def call(context, expected_name):
        result = _invoke(executor, GRAPH_ENTITY_LOOKUP_TOOL_ID, GRAPH_ENTITY_LOOKUP_VERSION,
                         {"entity_id": "shared"}, context)
        assert result.status == ToolStatus.SUCCESS
        assert result.data["entity"]["name"] == expected_name
        return True

    with PyThreadPoolExecutor(max_workers=20) as pool:
        futures = []
        for i in range(20):
            if i % 2 == 0:
                futures.append(pool.submit(call, context_a, "Tenant A Name"))
            else:
                futures.append(pool.submit(call, context_b, "Tenant B Name"))
        assert all(future.result(timeout=30) for future in futures)


# Source coverage metadata isolation across tenants
def test_source_coverage_does_not_leak_across_tenants(graph_store, postgres_store, registry):
    tenant_a, tenant_b, workspace = _name(), _name(), _name()
    repo_a = GraphRepository(graph_store, tenant_a, workspace, "writer", allow_all=True, can_evidence=True)
    coordinator_a = SyncCoordinator(postgres_store, repo_a)
    coordinator_a.save_config(ConnectorConfig(
        tenant_id=tenant_a, workspace_id=workspace,
        identity=ConnectorIdentity(connector_id="conn-a", source_id="jira", kind="jira")))
    executor = ToolExecutor(registry, ToolDependencies(graph_store=graph_store, postgres_store=postgres_store))
    result_b = _invoke(executor, SOURCE_COVERAGE_TOOL_ID, SOURCE_COVERAGE_VERSION,
                       {"source_id": "jira"}, _context(tenant_b, workspace))
    assert result_b.status == ToolStatus.SUCCESS
    assert result_b.data["status"] == "UNSUPPORTED_NOT_CONFIGURED"
    assert result_b.data["connector_configured"] is False


# Timeout wrapper does not claim hard cancellation (verified behavior, documented in executor.py)
def test_executor_timeout_does_not_kill_the_handler_thread():
    class In(BaseModel):
        pass

    class Out(BaseModel):
        value: str

    completed = threading.Event()

    def slow_handler(context, input_model, dependencies):
        time.sleep(0.5)
        completed.set()
        return {"value": "done"}

    registry = ToolRegistry()
    registry.register(ToolSpec(
        tool_id="slow", version="1.0.0", description="d", input_model=In, output_model=Out,
        handler=slow_handler, required_permissions=frozenset({Permission.READ_GRAPH}),
        side_effect=SideEffect.READ_ONLY, limits=ExecutionLimits(timeout_seconds=0.05),
    ))
    user = Principal(subject="u", tenant_id="t", workspaces=("w",), permissions=frozenset({Permission.READ_GRAPH}))
    context = AuthorizationContext(principal=user, tenant_id="t", workspace_id="w", task_id="t1",
                                   permissions=user.permissions)
    executor = ToolExecutor(registry, ToolDependencies())
    start = time.monotonic()
    result = _invoke(executor, "slow", "1.0.0", {}, context)
    elapsed = time.monotonic() - start
    assert result.status == ToolStatus.UNAVAILABLE
    assert elapsed < 0.4  # returns promptly, not after the handler's own 0.5s sleep
    assert not completed.is_set()  # handler has not finished yet when we got our answer
    time.sleep(0.6)
    assert completed.is_set()  # but it keeps running afterward: this wrapper cannot hard-cancel it
