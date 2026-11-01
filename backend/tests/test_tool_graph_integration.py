"""Integration coverage of registry -> executor -> security -> GraphRepository
for the graph/evidence/ontology tools, against a real isolated Neo4j instance."""
from __future__ import annotations

import os
import secrets
import socket
import subprocess
import time
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from neo4j import GraphDatabase

from backend.app.config import Settings
from backend.app.graph.driver import Neo4jStore
from backend.app.graph.models import EntityInput, RelationInput, SourceRecordInput
from backend.app.graph.repository import GraphRepository
from backend.app.security import AuthorizationContext, Permission, Principal
from backend.app.tools.contracts import ToolInvocation, ToolStatus
from backend.app.tools.evidence_tools import (
    EVIDENCE_CONFLICTS_TOOL_ID, EVIDENCE_CONFLICTS_VERSION, EVIDENCE_LOOKUP_TOOL_ID, EVIDENCE_LOOKUP_VERSION,
    EVIDENCE_VERIFY_TOOL_ID, EVIDENCE_VERIFY_VERSION, VerificationOutcome,
)
from backend.app.tools.executor import ToolExecutor
from backend.app.tools.graph_tools import (
    GRAPH_ENTITY_LOOKUP_TOOL_ID, GRAPH_ENTITY_LOOKUP_VERSION, GRAPH_FIND_PATHS_TOOL_ID,
    GRAPH_FIND_PATHS_VERSION, GRAPH_NEIGHBORHOOD_TOOL_ID, GRAPH_NEIGHBORHOOD_VERSION,
    GRAPH_RELATIONSHIP_LOOKUP_TOOL_ID, GRAPH_RELATIONSHIP_LOOKUP_VERSION,
    GRAPH_REVERSE_DEPENDENCIES_TOOL_ID, GRAPH_REVERSE_DEPENDENCIES_VERSION,
    GRAPH_REVISION_TOOL_ID, GRAPH_REVISION_VERSION, register_graph_tools,
)
from backend.app.tools.ontology_tools import (
    ONTOLOGY_VALIDATE_RELATIONSHIP_TOOL_ID, ONTOLOGY_VALIDATE_RELATIONSHIP_VERSION, register_ontology_tools,
)
from backend.app.tools.registry import ToolDependencies, ToolRegistry

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
    container = f"gh-neo4j-tools-{suffix}"
    volume = f"gh-neo4j-tools-data-{suffix}"
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


@pytest.fixture
def store(isolated_neo4j):
    uri, password = isolated_neo4j
    settings = Settings(neo4j_uri=uri, neo4j_user="neo4j", neo4j_password=password)
    neo4j_store = Neo4jStore(settings)
    neo4j_store.start()
    try:
        yield neo4j_store
    finally:
        neo4j_store.close()


@pytest.fixture
def registry():
    reg = ToolRegistry()
    register_graph_tools(reg)
    register_ontology_tools(reg)
    return reg


def _ingest_entity(store: Neo4jStore, tenant: str, workspace: str, external_id: str, name: str,
                   acl: list[str] | None = None, source_id: str = "src") -> None:
    repo = GraphRepository(store, tenant, workspace, "writer", allow_all=True, can_evidence=True)
    repo.ingest(SourceRecordInput(
        source_id=source_id, record_id="rec-1", title="rec-1", acl_subjects=acl or [],
        entities=[EntityInput(external_id=external_id, canonical_id=external_id, type="PERSON", name=name)],
    ))


def _context(tenant: str, workspace: str, permissions=None) -> AuthorizationContext:
    permissions = permissions or frozenset({Permission.READ_GRAPH})
    user = Principal(subject="reader", tenant_id=tenant, workspaces=(workspace,), permissions=permissions)
    return AuthorizationContext(principal=user, tenant_id=tenant, workspace_id=workspace, task_id="t1",
                               permissions=permissions)


def _invoke(executor: ToolExecutor, tool_id: str, version: str, arguments: dict, context: AuthorizationContext):
    return executor.execute(ToolInvocation(tool_id=tool_id, version=version, arguments=arguments, context=context))


# 2 / 18. Existing GH-005A entity_lookup functionality
def test_authorized_entity_lookup_still_succeeds(store, registry):
    tenant, workspace = _name(), _name()
    _ingest_entity(store, tenant, workspace, "alice", "Alice")
    executor = ToolExecutor(registry, ToolDependencies(graph_store=store))
    result = _invoke(executor, GRAPH_ENTITY_LOOKUP_TOOL_ID, GRAPH_ENTITY_LOOKUP_VERSION,
                     {"entity_id": "alice"}, _context(tenant, workspace))
    assert result.status == ToolStatus.SUCCESS
    assert result.data["entity"]["name"] == "Alice"


# 2. Authorized graph neighborhood and path retrieval; 4. relationship type/direction filtering
def test_neighborhood_and_find_paths(store, registry):
    tenant, workspace = _name(), _name()
    repo = GraphRepository(store, tenant, workspace, "writer", allow_all=True, can_evidence=True)
    repo.ingest(SourceRecordInput(
        source_id="src", record_id="rec-1", title="rec-1",
        entities=[
            EntityInput(external_id="svc-a", canonical_id="svc-a", type="SERVICE", name="A"),
            EntityInput(external_id="svc-b", canonical_id="svc-b", type="SERVICE", name="B"),
        ],
        relationships=[RelationInput(source_external_id="svc-a", target_external_id="svc-b", type="DEPENDS_ON")],
    ))
    executor = ToolExecutor(registry, ToolDependencies(graph_store=store))
    context = _context(tenant, workspace)

    neighborhood = _invoke(executor, GRAPH_NEIGHBORHOOD_TOOL_ID, GRAPH_NEIGHBORHOOD_VERSION,
                           {"entity_id": "svc-a"}, context)
    assert neighborhood.status == ToolStatus.SUCCESS
    assert len(neighborhood.data["relationships"]) == 1
    assert neighborhood.data["relationships"][0]["type"] == "DEPENDS_ON"
    assert neighborhood.graph_revision is not None

    paths = _invoke(executor, GRAPH_FIND_PATHS_TOOL_ID, GRAPH_FIND_PATHS_VERSION,
                    {"source_id": "svc-a", "target_id": "svc-b"}, context)
    assert paths.status == ToolStatus.SUCCESS
    assert len(paths.data["paths"]) == 1

    relationship_id = neighborhood.data["relationships"][0]["id"]
    looked_up = _invoke(executor, GRAPH_RELATIONSHIP_LOOKUP_TOOL_ID, GRAPH_RELATIONSHIP_LOOKUP_VERSION,
                        {"relation_id": relationship_id}, context)
    assert looked_up.status == ToolStatus.SUCCESS
    assert looked_up.data["relationship"]["type"] == "DEPENDS_ON"


# 3. Directed reverse dependencies
def test_reverse_dependencies(store, registry):
    tenant, workspace = _name(), _name()
    repo = GraphRepository(store, tenant, workspace, "writer", allow_all=True, can_evidence=True)
    repo.ingest(SourceRecordInput(
        source_id="src", record_id="rec-1", title="rec-1",
        entities=[
            EntityInput(external_id="svc-a", canonical_id="svc-a", type="SERVICE", name="A"),
            EntityInput(external_id="svc-b", canonical_id="svc-b", type="SERVICE", name="B"),
        ],
        relationships=[RelationInput(source_external_id="svc-a", target_external_id="svc-b", type="DEPENDS_ON")],
    ))
    executor = ToolExecutor(registry, ToolDependencies(graph_store=store))
    result = _invoke(executor, GRAPH_REVERSE_DEPENDENCIES_TOOL_ID, GRAPH_REVERSE_DEPENDENCIES_VERSION,
                     {"entity_id": "svc-b"}, _context(tenant, workspace))
    assert result.status == ToolStatus.SUCCESS
    assert any(rel["source_id"] == "svc-a" for rel in result.data["relationships"])


# 11. Historical versus current graph results
def test_historical_vs_current_neighborhood(store, registry):
    tenant, workspace = _name(), _name()
    repo = GraphRepository(store, tenant, workspace, "writer", allow_all=True, can_evidence=True)
    repo.ingest(SourceRecordInput(
        source_id="src", record_id="rec-1", title="rec-1",
        entities=[EntityInput(external_id="alice", canonical_id="alice", type="PERSON", name="Alice")],
    ))
    repo.retract_record("src", "rec-1")
    executor = ToolExecutor(registry, ToolDependencies(graph_store=store))
    context = _context(tenant, workspace)
    current = _invoke(executor, GRAPH_ENTITY_LOOKUP_TOOL_ID, GRAPH_ENTITY_LOOKUP_VERSION,
                      {"entity_id": "alice"}, context)
    assert current.status == ToolStatus.NOT_FOUND
    historical = _invoke(executor, GRAPH_ENTITY_LOOKUP_TOOL_ID, GRAPH_ENTITY_LOOKUP_VERSION,
                         {"entity_id": "alice", "include_history": True}, context)
    assert historical.status == ToolStatus.SUCCESS
    assert historical.data["entity"]["active"] is False


# 12. Graph revision access
def test_graph_revision_tool(store, registry):
    tenant, workspace = _name(), _name()
    _ingest_entity(store, tenant, workspace, "alice", "Alice")
    executor = ToolExecutor(registry, ToolDependencies(graph_store=store))
    result = _invoke(executor, GRAPH_REVISION_TOOL_ID, GRAPH_REVISION_VERSION, {}, _context(tenant, workspace))
    assert result.status == ToolStatus.SUCCESS
    assert result.data["revision"] >= 1
    assert result.graph_revision == result.data["revision"]


# 13. Cross-tenant access denial
def test_cross_tenant_neighborhood_denied(store, registry):
    tenant, other_tenant, workspace = _name(), _name(), _name()
    _ingest_entity(store, tenant, workspace, "alice", "Alice")
    executor = ToolExecutor(registry, ToolDependencies(graph_store=store))
    result = _invoke(executor, GRAPH_NEIGHBORHOOD_TOOL_ID, GRAPH_NEIGHBORHOOD_VERSION,
                     {"entity_id": "alice"}, _context(other_tenant, workspace))
    assert result.status == ToolStatus.NOT_FOUND
    assert result.data is None


# 14. Restricted intermediate-node/path isolation
def test_restricted_source_entity_not_disclosed_via_neighborhood(store, registry):
    tenant, workspace = _name(), _name()
    repo = GraphRepository(store, tenant, workspace, "writer", allow_all=True, can_evidence=True)
    repo.ingest(SourceRecordInput(
        source_id="src", record_id="public", title="public",
        entities=[EntityInput(external_id="svc-a", canonical_id="svc-a", type="SERVICE", name="A")],
    ))
    repo.ingest(SourceRecordInput(
        source_id="src", record_id="restricted", title="restricted", acl_subjects=["someone-else"],
        entities=[
            EntityInput(external_id="svc-a", canonical_id="svc-a", type="SERVICE", name="A"),
            EntityInput(external_id="svc-secret", canonical_id="svc-secret", type="SERVICE", name="Secret"),
        ],
        relationships=[RelationInput(source_external_id="svc-a", target_external_id="svc-secret", type="DEPENDS_ON")],
    ))
    executor = ToolExecutor(registry, ToolDependencies(graph_store=store))
    unauthorized = _context(tenant, workspace, frozenset({Permission.READ_GRAPH}))
    hidden = _invoke(executor, GRAPH_ENTITY_LOOKUP_TOOL_ID, GRAPH_ENTITY_LOOKUP_VERSION,
                     {"entity_id": "svc-secret"}, unauthorized)
    assert hidden.status == ToolStatus.NOT_FOUND
    neighborhood = _invoke(executor, GRAPH_NEIGHBORHOOD_TOOL_ID, GRAPH_NEIGHBORHOOD_VERSION,
                           {"entity_id": "svc-a"}, unauthorized)
    assert neighborhood.status == ToolStatus.SUCCESS
    assert all(rel["target_id"] != "svc-secret" for rel in neighborhood.data["relationships"])
    assert all(node["id"] != "svc-secret" for node in neighborhood.data["nodes"])


# 15. Delegated permission restrictions
def test_delegated_context_narrows_tool_access(store, registry):
    tenant, workspace = _name(), _name()
    _ingest_entity(store, tenant, workspace, "alice", "Alice")
    executor = ToolExecutor(registry, ToolDependencies(graph_store=store))
    full_context = _context(tenant, workspace, frozenset({Permission.READ_GRAPH, Permission.READ_EVIDENCE}))
    narrowed = full_context.delegate(frozenset({Permission.READ_EVIDENCE}))
    result = _invoke(executor, GRAPH_ENTITY_LOOKUP_TOOL_ID, GRAPH_ENTITY_LOOKUP_VERSION,
                     {"entity_id": "alice"}, narrowed)
    assert result.status == ToolStatus.FORBIDDEN


# 16. Pagination and bounded traversal
def test_neighborhood_limit_truncation(store, registry):
    tenant, workspace = _name(), _name()
    repo = GraphRepository(store, tenant, workspace, "writer", allow_all=True, can_evidence=True)
    entities = [EntityInput(external_id="hub", canonical_id="hub", type="SERVICE", name="hub")]
    entities += [EntityInput(external_id=f"leaf-{i}", canonical_id=f"leaf-{i}", type="SERVICE", name=f"leaf-{i}")
                for i in range(5)]
    relationships = [RelationInput(source_external_id="hub", target_external_id=f"leaf-{i}", type="DEPENDS_ON")
                     for i in range(5)]
    repo.ingest(SourceRecordInput(source_id="src", record_id="rec-1", title="rec-1",
                                  entities=entities, relationships=relationships))
    executor = ToolExecutor(registry, ToolDependencies(graph_store=store))
    result = _invoke(executor, GRAPH_NEIGHBORHOOD_TOOL_ID, GRAPH_NEIGHBORHOOD_VERSION,
                     {"entity_id": "hub", "limit": 2}, _context(tenant, workspace))
    assert result.status == ToolStatus.INCOMPLETE
    assert result.truncated is True
    assert len(result.data["relationships"]) == 2


# 17. Malformed inputs and safe error responses
def test_malformed_input_rejected_safely(store, registry):
    executor = ToolExecutor(registry, ToolDependencies(graph_store=store))
    tenant, workspace = _name(), _name()
    result = _invoke(executor, GRAPH_FIND_PATHS_TOOL_ID, GRAPH_FIND_PATHS_VERSION,
                     {"source_id": "a", "target_id": "b", "max_depth": 99}, _context(tenant, workspace))
    assert result.status == ToolStatus.INVALID_INPUT
    assert result.data is None


# 10. Invalid ontology endpoints and origins
def test_ontology_validate_relationship_tool(store, registry):
    executor = ToolExecutor(registry, ToolDependencies(graph_store=store))
    tenant, workspace = _name(), _name()
    context = _context(tenant, workspace)
    valid = _invoke(executor, ONTOLOGY_VALIDATE_RELATIONSHIP_TOOL_ID, ONTOLOGY_VALIDATE_RELATIONSHIP_VERSION,
                    {"relation_type": "DEPENDS_ON", "source_type": "SERVICE", "target_type": "SERVICE"}, context)
    assert valid.status == ToolStatus.SUCCESS
    assert valid.data["valid"] is True

    invalid_endpoint = _invoke(executor, ONTOLOGY_VALIDATE_RELATIONSHIP_TOOL_ID, ONTOLOGY_VALIDATE_RELATIONSHIP_VERSION,
                               {"relation_type": "OWNS", "source_type": "SERVICE", "target_type": "SERVICE"}, context)
    assert invalid_endpoint.status == ToolStatus.SUCCESS
    assert invalid_endpoint.data["valid"] is False

    invalid_origin = _invoke(executor, ONTOLOGY_VALIDATE_RELATIONSHIP_TOOL_ID, ONTOLOGY_VALIDATE_RELATIONSHIP_VERSION,
                             {"relation_type": "OWNS", "source_type": "PERSON", "target_type": "SERVICE",
                              "origin": "inferred"}, context)
    assert invalid_origin.status == ToolStatus.SUCCESS
    assert invalid_origin.data["valid"] is False
