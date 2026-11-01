"""GH-006C acceptance (real-graph half): drives each production skill's
*declared* tool sequence through the real ``ToolRegistry``/``ToolExecutor``
against an isolated Neo4j instance, exactly as a future Stage 06 agent
would — except the sequence here is fixed by the test, not chosen by an
LLM. This proves the skill's declared ``compatible_tools`` actually work
end-to-end; it does not and cannot prove an LLM will follow the skill's
prose correctly.
"""
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
from backend.app.graph.models import EntityInput, RelationInput, SourceRecordInput
from backend.app.graph.repository import GraphRepository
from backend.app.security import AuthorizationContext, Permission, Principal
from backend.app.skills.catalog import build_production_skill_registry
from backend.app.tools import register_all_tools
from backend.app.tools.contracts import ToolInvocation, ToolStatus
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
    container = f"gh-neo4j-skills-{suffix}"
    volume = f"gh-neo4j-skills-data-{suffix}"
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
            pytest.fail("BLOCKED: isolated Neo4j did not become ready (environment, not code)")
        driver.close()
        yield uri, password
    finally:
        subprocess.run(["docker", "rm", "-f", container], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(["docker", "volume", "rm", volume], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


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
def tool_registry() -> ToolRegistry:
    registry = ToolRegistry()
    register_all_tools(registry)
    return registry


@pytest.fixture
def skill_registry(tool_registry):
    return build_production_skill_registry(tool_registry=tool_registry)


def _context(tenant: str, workspace: str, *, with_evidence: bool = True) -> AuthorizationContext:
    permissions = {Permission.READ_GRAPH}
    if with_evidence:
        permissions.add(Permission.READ_EVIDENCE)
    permissions = frozenset(permissions)
    user = Principal(subject="reader", tenant_id=tenant, workspaces=(workspace,), permissions=permissions)
    return AuthorizationContext(principal=user, tenant_id=tenant, workspace_id=workspace, task_id="t1",
                               permissions=permissions)


def _invoke(executor, tool_id, version, arguments, context):
    return executor.execute(ToolInvocation(tool_id=tool_id, version=version, arguments=arguments, context=context))


def _declared_tool(skill_registry, skill_id, tool_id):
    """Looks up the exact (tool_id, version) the skill actually declared —
    proves the workflow below invokes what the skill promises, not an
    independently hardcoded pair."""
    manifest = skill_registry.manifest(skill_id, "1.0.0")
    for dependency in manifest.compatible_tools:
        if dependency.tool_id == tool_id:
            return dependency.tool_id, dependency.version
    raise AssertionError(f"{skill_id} does not declare {tool_id} as a compatible tool")


# 6. verify-evidence: supported, conflicting and withdrawn assertions
def test_verify_evidence_skill_workflow_supported(store, tool_registry, skill_registry):
    tenant, workspace = _name(), _name()
    repo = GraphRepository(store, tenant, workspace, "writer", allow_all=True, can_evidence=True)
    for source in ("github", "jira"):
        repo.ingest(SourceRecordInput(
            source_id=source, record_id="rec-1", title="rec-1",
            entities=[EntityInput(external_id="alice", canonical_id="alice", type="PERSON", name="Alice")],
        ))
    executor = ToolExecutor(tool_registry, ToolDependencies(graph_store=store))
    context = _context(tenant, workspace)

    tool_id, version = _declared_tool(skill_registry, "verify-evidence", "evidence.verify")
    result = _invoke(executor, tool_id, version, {"entity_id": "alice"}, context)
    assert result.status == ToolStatus.SUCCESS
    assert result.data["outcome"] == "SUPPORTED"
    assert result.evidence_refs  # evidence references were returned, per the skill's output contract


def test_verify_evidence_skill_workflow_conflicting_with_authority(store, tool_registry):
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
    rule = AuthorityRule(rule_id="r1", scope=AuthorityScope.TENANT, tenant_id=tenant,
                        entity_type="PERSON", source_id="github", precedence=1)
    skill_registry = build_production_skill_registry(tool_registry=tool_registry)
    executor = ToolExecutor(tool_registry, ToolDependencies(graph_store=store, authority_rules=(rule,)))
    context = _context(tenant, workspace)

    verify_tool, verify_version = _declared_tool(skill_registry, "verify-evidence", "evidence.verify")
    verify_result = _invoke(executor, verify_tool, verify_version, {"entity_id": "alice"}, context)
    assert verify_result.data["outcome"] == "CONFLICTING"

    conflicts_tool, conflicts_version = _declared_tool(skill_registry, "verify-evidence", "evidence.conflicts")
    conflicts_result = _invoke(executor, conflicts_tool, conflicts_version, {"entity_id": "alice"}, context)
    assert conflicts_result.status == ToolStatus.SUCCESS
    assert conflicts_result.data["authority"]["name"]["status"] == "authoritative"
    assert conflicts_result.data["authority"]["name"]["winning_source_id"] == "github"
    # The skill's instructions forbid treating this as a resolved, single
    # surviving value — the conflicting values must remain fully reported.
    assert sorted(conflicts_result.data["conflicts"]["name"]) == ["Alice G", "Alice J"]


def test_verify_evidence_skill_workflow_withdrawn_assertion(store, tool_registry, skill_registry):
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
    executor = ToolExecutor(tool_registry, ToolDependencies(graph_store=store))
    tool_id, version = _declared_tool(skill_registry, "verify-evidence", "evidence.verify")
    result = _invoke(executor, tool_id, version, {"entity_id": "alice"}, _context(tenant, workspace))
    assert result.status == ToolStatus.SUCCESS
    assert result.data["outcome"] == "SUPPORTED"  # jira still currently supports it
    by_source = {item["source_id"]: item["current"] for item in result.data["assertions"]}
    assert by_source == {"github": False, "jira": True}  # withdrawn source correctly marked not current


# 12. Missing source coverage produces an incomplete/unknown outcome, not a false negative
def test_verify_evidence_skill_workflow_missing_entity_is_incomplete_not_a_guess(store, tool_registry,
                                                                                 skill_registry):
    executor = ToolExecutor(tool_registry, ToolDependencies(graph_store=store))
    tenant, workspace = _name(), _name()
    tool_id, version = _declared_tool(skill_registry, "verify-evidence", "evidence.verify")
    result = _invoke(executor, tool_id, version, {"entity_id": "ghost"}, _context(tenant, workspace))
    assert result.status == ToolStatus.NOT_FOUND  # never fabricated as SUPPORTED or UNSUPPORTED
    assert result.data is None


# 7. graph-trace: bounded, authorized paths, labeled by origin
def test_graph_trace_skill_workflow_labels_origin_and_respects_bounds(store, tool_registry, skill_registry):
    tenant, workspace = _name(), _name()
    repo = GraphRepository(store, tenant, workspace, "writer", allow_all=True, can_evidence=True)
    repo.ingest(SourceRecordInput(
        source_id="github", record_id="rec-1", title="rec-1",
        entities=[EntityInput(external_id="alice", canonical_id="alice", type="PERSON", name="Alice"),
                 EntityInput(external_id="file1", canonical_id="file1", type="FILE", name="File")],
        relationships=[RelationInput(source_external_id="alice", target_external_id="file1",
                                     type="AUTHORED", origin="source_asserted")],
    ))
    executor = ToolExecutor(tool_registry, ToolDependencies(graph_store=store))
    context = _context(tenant, workspace)

    tool_id, version = _declared_tool(skill_registry, "graph-trace", "graph.neighborhood")
    result = _invoke(executor, tool_id, version, {"entity_id": "alice", "limit": 10}, context)
    assert result.status == ToolStatus.SUCCESS
    assert result.data["relationships"][0]["origin"] == "source_asserted"
    assert result.truncated is False


# 9/10/11. Deterministic authority application, tenant isolation, no evidence leakage
def test_investigate_conflict_skill_workflow_isolates_tenants_and_preserves_unresolved(store, tool_registry,
                                                                                       skill_registry):
    tenant_a, tenant_b, workspace = _name(), _name(), _name()
    for tenant, name in ((tenant_a, "Name A"), (tenant_b, "Name B")):
        repo = GraphRepository(store, tenant, workspace, "writer", allow_all=True, can_evidence=True)
        repo.ingest(SourceRecordInput(
            source_id="github", record_id="rec-1", title="rec-1",
            entities=[EntityInput(external_id="shared", canonical_id="shared", type="PERSON", name=name)],
        ))
        repo.ingest(SourceRecordInput(
            source_id="jira", record_id="rec-1", title="rec-1",
            entities=[EntityInput(external_id="shared", canonical_id="shared", type="PERSON", name=f"{name}-X")],
        ))
    executor = ToolExecutor(tool_registry, ToolDependencies(graph_store=store))

    tool_id, version = _declared_tool(skill_registry, "investigate-conflict", "evidence.conflicts")
    result_a = _invoke(executor, tool_id, version, {"entity_id": "shared"}, _context(tenant_a, workspace))
    result_b = _invoke(executor, tool_id, version, {"entity_id": "shared"}, _context(tenant_b, workspace))

    # 8. No authority rule was configured -> unresolved by design, not a guessed winner.
    assert result_a.data["authority"]["name"]["status"] == "no_rule"
    assert sorted(result_a.data["conflicts"]["name"]) == ["Name A", "Name A-X"]
    assert sorted(result_b.data["conflicts"]["name"]) == ["Name B", "Name B-X"]  # 10: no cross-tenant leakage


# 11. Restricted evidence never leaks without READ_EVIDENCE
def test_verify_evidence_skill_workflow_withholds_evidence_without_permission(store, tool_registry,
                                                                              skill_registry):
    tenant, workspace = _name(), _name()
    repo = GraphRepository(store, tenant, workspace, "writer", allow_all=True, can_evidence=True)
    repo.ingest(SourceRecordInput(
        source_id="github", record_id="rec-1", title="rec-1",
        entities=[EntityInput(external_id="alice", canonical_id="alice", type="PERSON", name="Alice")],
    ))
    executor = ToolExecutor(tool_registry, ToolDependencies(graph_store=store))
    context = _context(tenant, workspace, with_evidence=False)
    tool_id, version = _declared_tool(skill_registry, "verify-evidence", "evidence.verify")
    result = _invoke(executor, tool_id, version, {"entity_id": "alice"}, context)
    assert result.status == ToolStatus.SUCCESS
    assert result.data["outcome"] == "INACCESSIBLE"
    assert result.data["assertions"] is None
    assert result.data["evidence"] is None
