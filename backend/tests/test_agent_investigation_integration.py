"""GH-007A integration: the real ADK ``Runner`` + ``LlmAgent`` driving the
real ``SkillRegistry``/``ToolRegistry``/``ToolExecutor`` against an isolated
Neo4j instance — the same ``isolated_neo4j`` pattern used by
``test_tool_graph_integration.py``/``test_skills_workflow_integration.py``.

The model itself is still the offline scripted ``FakeProviderClient`` (no
network, no credential) — this file's job is to prove the agent's tool calls
really reach real graph data through the real executor/ACL, not to test
model reasoning quality.
"""
from __future__ import annotations

import asyncio
import os
import secrets
import socket
import subprocess
import time
import uuid

import pytest
from neo4j import GraphDatabase

from backend.app.agent.contracts import (
    AgentExecutionContext, ApprovedSkill, InvestigationRequest, InvestigationStatus, RuntimeLimits,
)
from backend.app.agent.investigator import run_investigation
from backend.app.config import Settings
from backend.app.gateway.contracts import ModelRole
from backend.app.gateway.gateway import ModelGateway
from backend.app.gateway.provider import ProviderResponse, RawFunctionCall, RawUsage
from backend.app.gateway.routing import RoleRouter
from backend.app.graph.authority import AuthorityRule, AuthorityScope
from backend.app.graph.driver import Neo4jStore
from backend.app.graph.models import EntityInput, RelationInput, SourceRecordInput
from backend.app.graph.repository import GraphRepository
from backend.app.security import AuthorizationContext, Permission, Principal
from backend.app.skills.catalog import build_production_skill_registry
from backend.app.tools import register_all_tools
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
    container = f"gh-neo4j-agent-{suffix}"
    volume = f"gh-neo4j-agent-data-{suffix}"
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
        for _ in range(180):
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


class FakeProviderClient:
    def __init__(self, input_tokens: int = 10):
        self.input_tokens = input_tokens
        self.calls = 0
        self.queue: list = []

    def count_tokens(self, model, contents) -> int:
        return self.input_tokens

    def generate(self, model, contents, config) -> ProviderResponse:
        self.calls += 1
        return self.queue.pop(0)

    def available_models(self):
        return frozenset({"fake-lite", "fake-default"})


def _success(text=None, function_calls=()) -> ProviderResponse:
    return ProviderResponse(
        text=text, parsed=None,
        usage=RawUsage(input_tokens=10, output_tokens=5, thinking_tokens=0, total_tokens=15),
        model_version="fake-default-001", finish_reason="STOP", safety_blocked=False, function_calls=function_calls,
    )


def _router() -> RoleRouter:
    return RoleRouter(role_models={ModelRole.LIGHTWEIGHT: "fake-lite", ModelRole.DEFAULT: "fake-default"})


def _exec_context(tenant: str, workspace: str, *, skill_id: str, with_evidence: bool = True) -> AgentExecutionContext:
    permissions = {Permission.READ_GRAPH, Permission.EXECUTE_TOOL}
    if with_evidence:
        permissions.add(Permission.READ_EVIDENCE)
    permissions = frozenset(permissions)
    principal = Principal(subject="reader", tenant_id=tenant, workspaces=(workspace,), permissions=permissions)
    auth = AuthorizationContext(principal=principal, tenant_id=tenant, workspace_id=workspace, task_id="t1",
                                permissions=permissions)
    return AgentExecutionContext(
        authorization=auth, run_id=f"run-{_name()}", model_role=ModelRole.DEFAULT,
        approved_skills=(ApprovedSkill(skill_id=skill_id, version="1.0.0"),),
        limits=RuntimeLimits(max_llm_calls=6, max_tool_calls=6, overall_timeout_seconds=60),
    )


# 7 / 12. Authorized tool execution through the real executor; evidence verification with source refs.
def test_verify_evidence_investigation_against_real_graph(store, tool_registry, skill_registry):
    tenant, workspace = _name(), _name()
    repo = GraphRepository(store, tenant, workspace, "writer", allow_all=True, can_evidence=True)
    for source in ("github", "jira"):
        repo.ingest(SourceRecordInput(
            source_id=source, record_id="rec-1", title="rec-1",
            entities=[EntityInput(external_id="alice", canonical_id="alice", type="PERSON", name="Alice")],
        ))
    tool_executor = ToolExecutor(tool_registry, ToolDependencies(graph_store=store))

    provider = FakeProviderClient()
    provider.queue = [
        _success(function_calls=(RawFunctionCall(id="1", name="skill_discover", arguments={}),)),
        _success(function_calls=(RawFunctionCall(id="2", name="skill_load_instructions",
                                                 arguments={"skill_id": "verify-evidence"}),)),
        _success(function_calls=(RawFunctionCall(id="3", name="evidence_verify",
                                                 arguments={"entity_id": "alice"}),)),
        _success(text="Alice is SUPPORTED by evidence from github and jira."),
    ]
    gateway = ModelGateway(provider, _router())
    exec_context = _exec_context(tenant, workspace, skill_id="verify-evidence")
    request = InvestigationRequest(question="Is Alice's record supported by evidence?", entity_id="alice")

    result = asyncio.run(run_investigation(request, exec_context, gateway, tool_registry, tool_executor,
                                           skill_registry))

    assert result.status == InvestigationStatus.COMPLETE
    assert "alice" in result.entity_ids
    assert result.evidence_refs  # real evidence references from the real graph
    assert result.graph_revision is not None
    assert not result.rejected_unverified_claims
    assert result.skill_used.skill_id == "verify-evidence"


# 13. Conflicting evidence remains unresolved (no authority rule configured).
def test_investigate_conflict_workflow_preserves_unresolved_conflict(store, tool_registry, skill_registry):
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
    tool_executor = ToolExecutor(tool_registry, ToolDependencies(graph_store=store))

    provider = FakeProviderClient()
    provider.queue = [
        _success(function_calls=(RawFunctionCall(id="1", name="skill_discover", arguments={}),)),
        _success(function_calls=(RawFunctionCall(id="2", name="skill_load_instructions",
                                                 arguments={"skill_id": "investigate-conflict"}),)),
        _success(function_calls=(RawFunctionCall(id="3", name="graph_entity_lookup",
                                                 arguments={"entity_id": "alice"}),)),
        _success(function_calls=(RawFunctionCall(id="4", name="evidence_conflicts",
                                                 arguments={"entity_id": "alice"}),)),
        _success(text="Alice's name is CONFLICTING between github and jira with no authority rule to resolve it."),
    ]
    gateway = ModelGateway(provider, _router())
    exec_context = _exec_context(tenant, workspace, skill_id="investigate-conflict")
    request = InvestigationRequest(question="Is there a conflict about Alice's name?", entity_id="alice")

    result = asyncio.run(run_investigation(request, exec_context, gateway, tool_registry, tool_executor,
                                           skill_registry))

    assert result.status == InvestigationStatus.PARTIAL  # unresolved conflict -> never silently COMPLETE
    assert result.unresolved_conflicts
    assert "alice" in result.entity_ids


# 11. Graph relationship investigation (graph-trace), bounded and origin-labeled at the tool layer.
def test_graph_trace_investigation_against_real_graph(store, tool_registry, skill_registry):
    tenant, workspace = _name(), _name()
    repo = GraphRepository(store, tenant, workspace, "writer", allow_all=True, can_evidence=True)
    repo.ingest(SourceRecordInput(
        source_id="github", record_id="rec-1", title="rec-1",
        entities=[EntityInput(external_id="alice", canonical_id="alice", type="PERSON", name="Alice"),
                 EntityInput(external_id="file1", canonical_id="file1", type="FILE", name="File")],
        relationships=[RelationInput(source_external_id="alice", target_external_id="file1",
                                     type="AUTHORED", origin="source_asserted")],
    ))
    tool_executor = ToolExecutor(tool_registry, ToolDependencies(graph_store=store))

    provider = FakeProviderClient()
    provider.queue = [
        _success(function_calls=(RawFunctionCall(id="1", name="skill_discover", arguments={}),)),
        _success(function_calls=(RawFunctionCall(id="2", name="skill_load_instructions",
                                                 arguments={"skill_id": "graph-trace"}),)),
        _success(function_calls=(RawFunctionCall(id="3", name="graph_neighborhood",
                                                 arguments={"entity_id": "alice", "limit": 10}),)),
        _success(text="Alice authored File (source-backed, current)."),
    ]
    gateway = ModelGateway(provider, _router())
    exec_context = _exec_context(tenant, workspace, skill_id="graph-trace")
    request = InvestigationRequest(question="What does Alice relate to?", entity_id="alice")

    result = asyncio.run(run_investigation(request, exec_context, gateway, tool_registry, tool_executor,
                                           skill_registry))

    assert result.status == InvestigationStatus.COMPLETE
    assert "alice" in result.entity_ids
    assert "file1" in result.entity_ids
    assert result.relationship_ids  # the AUTHORED relationship was actually returned by the tool


# 8/10. Cross-tenant isolation: tenant A's investigation never sees tenant B's data.
def test_cross_tenant_investigation_isolation(store, tool_registry, skill_registry):
    tenant_a, tenant_b, workspace = _name(), _name(), _name()
    for tenant, name in ((tenant_a, "Alpha Co"), (tenant_b, "Beta Co")):
        repo = GraphRepository(store, tenant, workspace, "writer", allow_all=True, can_evidence=True)
        repo.ingest(SourceRecordInput(
            source_id="github", record_id="rec-1", title="rec-1",
            entities=[EntityInput(external_id="shared", canonical_id="shared", type="PERSON", name=name)],
        ))
    tool_executor = ToolExecutor(tool_registry, ToolDependencies(graph_store=store))

    def _queue():
        return [
            _success(function_calls=(RawFunctionCall(id="1", name="skill_discover", arguments={}),)),
            _success(function_calls=(RawFunctionCall(id="2", name="skill_load_instructions",
                                                     arguments={"skill_id": "verify-evidence"}),)),
            _success(function_calls=(RawFunctionCall(id="3", name="evidence_verify",
                                                     arguments={"entity_id": "shared"}),)),
            _success(text="Found the shared entity."),
        ]

    provider_a = FakeProviderClient()
    provider_a.queue = _queue()
    gateway_a = ModelGateway(provider_a, _router())
    result_a = asyncio.run(run_investigation(
        InvestigationRequest(question="who is shared?", entity_id="shared"),
        _exec_context(tenant_a, workspace, skill_id="verify-evidence"),
        gateway_a, tool_registry, tool_executor, skill_registry))

    assert result_a.status == InvestigationStatus.COMPLETE
    assert "shared" in result_a.entity_ids
    # The tool result itself only ever carried tenant A's name — tenant B's name never appeared anywhere.
    assert all("Beta Co" not in call.model_dump_json() for call in result_a.tool_calls)


# Empty graph result: a nonexistent entity is reported as unresolved, never guessed into existing.
def test_nonexistent_entity_is_incomplete_not_a_guess(store, tool_registry, skill_registry):
    tool_executor = ToolExecutor(tool_registry, ToolDependencies(graph_store=store))
    tenant, workspace = _name(), _name()

    provider = FakeProviderClient()
    provider.queue = [
        _success(function_calls=(RawFunctionCall(id="1", name="skill_discover", arguments={}),)),
        _success(function_calls=(RawFunctionCall(id="2", name="skill_load_instructions",
                                                 arguments={"skill_id": "verify-evidence"}),)),
        _success(function_calls=(RawFunctionCall(id="3", name="evidence_verify",
                                                 arguments={"entity_id": "ghost"}),)),
        _success(text="I could not find any record of 'ghost' in the graph."),
    ]
    gateway = ModelGateway(provider, _router())
    exec_context = _exec_context(tenant, workspace, skill_id="verify-evidence")
    request = InvestigationRequest(question="Does ghost exist?", entity_id="ghost")

    result = asyncio.run(run_investigation(request, exec_context, gateway, tool_registry, tool_executor,
                                           skill_registry))

    assert "ghost" not in result.entity_ids  # never fabricated as found
    assert result.rejected_unverified_claims  # the hinted entity_id was never confirmed
    assert any("looked up and not found" in claim for claim in result.rejected_unverified_claims)
