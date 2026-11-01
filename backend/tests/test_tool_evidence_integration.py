"""Integration coverage for evidence.lookup / evidence.verify / evidence.conflicts
against a real isolated Neo4j instance."""
from __future__ import annotations

import os
import secrets
import socket
import subprocess
import time
import uuid

import pytest
from neo4j import GraphDatabase

from backend.app.config import Settings
from backend.app.graph.authority import AuthorityRule, AuthorityScope
from backend.app.graph.driver import Neo4jStore
from backend.app.graph.models import EntityInput, SourceRecordInput
from backend.app.graph.repository import GraphRepository
from backend.app.security import AuthorizationContext, Permission, Principal
from backend.app.tools.contracts import ToolInvocation, ToolStatus
from backend.app.tools.evidence_tools import (
    EVIDENCE_CONFLICTS_TOOL_ID, EVIDENCE_CONFLICTS_VERSION, EVIDENCE_LOOKUP_TOOL_ID, EVIDENCE_LOOKUP_VERSION,
    EVIDENCE_VERIFY_TOOL_ID, EVIDENCE_VERIFY_VERSION, register_evidence_tools,
)
from backend.app.tools.executor import ToolExecutor
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
    container = f"gh-neo4j-evidence-{suffix}"
    volume = f"gh-neo4j-evidence-data-{suffix}"
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
    register_evidence_tools(reg)
    return reg


def _context(tenant: str, workspace: str) -> AuthorizationContext:
    permissions = frozenset({Permission.READ_GRAPH, Permission.READ_EVIDENCE})
    user = Principal(subject="reader", tenant_id=tenant, workspaces=(workspace,), permissions=permissions)
    return AuthorizationContext(principal=user, tenant_id=tenant, workspace_id=workspace, task_id="t1",
                               permissions=permissions)


def _invoke(executor, tool_id, version, arguments, context):
    return executor.execute(ToolInvocation(tool_id=tool_id, version=version, arguments=arguments, context=context))


# 5. Evidence lookup and provenance retrieval
def test_evidence_lookup_returns_provenance(store, registry):
    tenant, workspace = _name(), _name()
    repo = GraphRepository(store, tenant, workspace, "writer", allow_all=True, can_evidence=True)
    repo.ingest(SourceRecordInput(
        source_id="github", record_id="pr-1", title="PR #1",
        entities=[EntityInput(external_id="alice", canonical_id="alice", type="PERSON", name="Alice")],
    ))
    executor = ToolExecutor(registry, ToolDependencies(graph_store=store))
    result = _invoke(executor, EVIDENCE_LOOKUP_TOOL_ID, EVIDENCE_LOOKUP_VERSION,
                     {"entity_id": "alice"}, _context(tenant, workspace))
    assert result.status == ToolStatus.SUCCESS
    assert result.data["items"][0]["source_id"] == "github"
    assert result.evidence_refs == ("github:pr-1",)


# 6. Multi-source evidence verification (agreeing sources -> SUPPORTED)
def test_multi_source_agreement_is_supported(store, registry):
    tenant, workspace = _name(), _name()
    repo = GraphRepository(store, tenant, workspace, "writer", allow_all=True, can_evidence=True)
    for source in ("github", "jira"):
        repo.ingest(SourceRecordInput(
            source_id=source, record_id="rec-1", title="rec-1",
            entities=[EntityInput(external_id="alice", canonical_id="alice", type="PERSON", name="Alice")],
        ))
    executor = ToolExecutor(registry, ToolDependencies(graph_store=store))
    result = _invoke(executor, EVIDENCE_VERIFY_TOOL_ID, EVIDENCE_VERIFY_VERSION,
                     {"entity_id": "alice"}, _context(tenant, workspace))
    assert result.status == ToolStatus.SUCCESS
    assert result.data["outcome"] == "SUPPORTED"
    assert len(result.data["assertions"]) == 2


# 7. Contradictory authority claims
def test_conflicting_claims_and_authority_resolution(store, registry):
    tenant, workspace = _name(), _name()
    repo = GraphRepository(store, tenant, workspace, "writer", allow_all=True, can_evidence=True)
    repo.ingest(SourceRecordInput(
        source_id="github", record_id="rec-1", title="rec-1",
        entities=[EntityInput(external_id="alice", canonical_id="alice", type="PERSON", name="Alice G")],
    ))
    repo.ingest(SourceRecordInput(
        source_id="jira", record_id="rec-1", title="rec-1",
        entities=[EntityInput(external_id="alice", canonical_id="alice", type="PERSON", name="Alice J")],
    ))
    executor = ToolExecutor(registry, ToolDependencies(graph_store=store))
    context = _context(tenant, workspace)

    unresolved = _invoke(executor, EVIDENCE_VERIFY_TOOL_ID, EVIDENCE_VERIFY_VERSION,
                         {"entity_id": "alice"}, context)
    assert unresolved.status == ToolStatus.SUCCESS
    assert unresolved.data["outcome"] == "CONFLICTING"
    assert unresolved.data["entity"]["authority"]["name"]["status"] == "no_rule"

    conflicts_only = _invoke(executor, EVIDENCE_CONFLICTS_TOOL_ID, EVIDENCE_CONFLICTS_VERSION,
                             {"entity_id": "alice"}, context)
    assert conflicts_only.status == ToolStatus.SUCCESS
    assert sorted(conflicts_only.data["conflicts"]["name"]) == ["Alice G", "Alice J"]

    rule = AuthorityRule(rule_id="r1", scope=AuthorityScope.TENANT, tenant_id=tenant,
                        entity_type="PERSON", source_id="github", precedence=1)
    repo_with_rule = GraphRepository(store, tenant, workspace, "writer", allow_all=True, can_evidence=True,
                                     authority_rules=(rule,))
    resolved_entity = repo_with_rule.get_entity("alice")
    assert resolved_entity.authority["name"]["status"] == "authoritative"
    assert resolved_entity.authority["name"]["winning_source_id"] == "github"
    # Authority ranks a winner but never silently removes the conflict itself.
    assert resolved_entity.conflicts["name"]


# 8. Withdrawn and superseded evidence
def test_withdrawn_source_marked_not_current(store, registry):
    tenant, workspace = _name(), _name()
    repo = GraphRepository(store, tenant, workspace, "writer", allow_all=True, can_evidence=True)
    repo.ingest(SourceRecordInput(
        source_id="github", record_id="rec-1", title="rec-1",
        entities=[EntityInput(external_id="alice", canonical_id="alice", type="PERSON", name="Alice")],
    ))
    repo.ingest(SourceRecordInput(
        source_id="jira", record_id="rec-1", title="rec-1",
        entities=[EntityInput(external_id="alice", canonical_id="alice", type="PERSON", name="Alice")],
    ))
    repo.retract_record("github", "rec-1")
    executor = ToolExecutor(registry, ToolDependencies(graph_store=store))
    result = _invoke(executor, EVIDENCE_VERIFY_TOOL_ID, EVIDENCE_VERIFY_VERSION,
                     {"entity_id": "alice"}, _context(tenant, workspace))
    assert result.status == ToolStatus.SUCCESS
    assert result.data["outcome"] == "SUPPORTED"
    by_source = {item["source_id"]: item["current"] for item in result.data["assertions"]}
    assert by_source == {"github": False, "jira": True}


def test_verify_missing_entity_not_found(store, registry):
    executor = ToolExecutor(registry, ToolDependencies(graph_store=store))
    tenant, workspace = _name(), _name()
    result = _invoke(executor, EVIDENCE_VERIFY_TOOL_ID, EVIDENCE_VERIFY_VERSION,
                     {"entity_id": "ghost"}, _context(tenant, workspace))
    assert result.status == ToolStatus.NOT_FOUND
    assert result.data is None


def test_verify_without_read_evidence_is_inaccessible_but_safe(store, registry):
    tenant, workspace = _name(), _name()
    repo = GraphRepository(store, tenant, workspace, "writer", allow_all=True, can_evidence=True)
    repo.ingest(SourceRecordInput(
        source_id="github", record_id="rec-1", title="rec-1",
        entities=[EntityInput(external_id="alice", canonical_id="alice", type="PERSON", name="Alice")],
    ))
    permissions = frozenset({Permission.READ_GRAPH})
    user = Principal(subject="reader", tenant_id=tenant, workspaces=(workspace,), permissions=permissions)
    context = AuthorizationContext(principal=user, tenant_id=tenant, workspace_id=workspace,
                                   task_id="t1", permissions=permissions)
    executor = ToolExecutor(registry, ToolDependencies(graph_store=store))
    result = _invoke(executor, EVIDENCE_VERIFY_TOOL_ID, EVIDENCE_VERIFY_VERSION, {"entity_id": "alice"}, context)
    assert result.status == ToolStatus.SUCCESS
    assert result.data["outcome"] == "INACCESSIBLE"
    assert result.data["assertions"] is None
    assert result.data["evidence"] is None
