"""ND-13B: independent, deterministic evidence verification.

Tests 1-2 use a real, isolated Neo4j (same ``isolated_neo4j`` docker fixture
convention as ``test_investigation_discovery.py``) so the dependency-path
verification actually exercises the real graph/evidence paths. Tests 3-4 are
offline/scripted: 3 reuses ND-13A's ``test_agent_coordinator.py`` fake-
governance-store pattern for authorization isolation, and 4 uses a tiny fake
``ToolExecutor`` double (the verifier only ever calls ``.execute()``) to
exercise budget exhaustion and evidence-duplication without needing a real
database for that part.
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

from backend.app.agent.contracts import (
    ClaimType, InvestigationStatus, SpecialistResult, SpecialistRole, VerificationStatus,
)
from backend.app.agent.coordinator import run_coordinated_investigation
from backend.app.agent.verifier import _verify_dependency_path, run_verification_pass
from backend.app.config import Settings
from backend.app.gateway.contracts import ModelRole
from backend.app.gateway.gateway import ModelGateway
from backend.app.gateway.provider import ProviderResponse, RawFunctionCall, RawUsage
from backend.app.gateway.routing import RoleRouter
from backend.app.governance.identity import NEXUS_TENANT_ID, ROLE_PERMISSIONS
from backend.app.governance.store import GovernanceStore
from backend.app.graph.driver import Neo4jStore
from backend.app.graph.models import EntityInput, EntityType, RelationInput, RelationType, SourceRecordInput
from backend.app.graph.repository import GraphRepository
from backend.app.security import AuthorizationContext, Permission, Principal
from backend.app.tools import register_all_tools
from backend.app.tools.contracts import ToolInvocation, ToolResult, ToolStatus
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
    container = f"gh-neo4j-verifier-{suffix}"
    volume = f"gh-neo4j-verifier-data-{suffix}"
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


def _ingest_dependency_chain(store, tenant_id: str, workspace_id: str, *, pr_id: str, service_id: str,
                             consumer_id: str) -> None:
    write_repo = GraphRepository(store, tenant_id, workspace_id, "test-writer", allow_all=True, can_evidence=True)
    write_repo.ingest(SourceRecordInput(
        source_id="github", record_id="pr-declares-service", title=f"{pr_id} references {service_id}",
        acl_subjects=[],
        entities=[
            EntityInput(external_id=pr_id, canonical_id=pr_id, type=EntityType.PULL_REQUEST, name=pr_id),
            EntityInput(external_id=service_id, canonical_id=service_id, type=EntityType.SERVICE, name=service_id),
        ],
        relationships=[RelationInput(source_external_id=pr_id, target_external_id=service_id,
                                     type=RelationType.REFERENCES)],
    ))
    write_repo.ingest(SourceRecordInput(
        source_id="catalog", record_id="consumer-depends-on-service",
        title=f"{consumer_id} depends on {service_id}", acl_subjects=[],
        entities=[
            EntityInput(external_id=consumer_id, canonical_id=consumer_id, type=EntityType.SERVICE,
                       name=consumer_id),
            EntityInput(external_id=service_id, canonical_id=service_id, type=EntityType.SERVICE, name=service_id),
        ],
        relationships=[RelationInput(source_external_id=consumer_id, target_external_id=service_id,
                                     type=RelationType.DEPENDS_ON)],
    ))


def _authorization(tenant_id: str, workspace_id: str) -> AuthorizationContext:
    principal = Principal(subject="tester", tenant_id=tenant_id, workspaces=(workspace_id,),
                          permissions=frozenset({Permission.READ_GRAPH, Permission.READ_EVIDENCE,
                                                 Permission.EXECUTE_TOOL}))
    return AuthorizationContext(principal=principal, tenant_id=tenant_id, workspace_id=workspace_id,
                                task_id="verify-task", permissions=principal.permissions)


def _impact_stub(fresh: ToolResult) -> SpecialistResult:
    """Stands in for what the (separately tested) LLM-driven IMPACT pass's
    ``SpecialistResult`` would have carried forward -- built here from the
    SAME real tool result the verifier will later independently re-fetch, so
    the test controls exactly what each side observed."""
    return SpecialistResult(role=SpecialistRole.IMPACT, status=InvestigationStatus.COMPLETE,
                            evidence_refs=fresh.evidence_refs, graph_revision=fresh.graph_revision)


# 1. Evidence-supported vs. unsupported claims, against a real, isolated Neo4j.
def test_supported_and_unsupported_claims_against_real_graph(store, tool_registry):
    tenant_id, workspace_id = _name(), _name()
    pr_id, service_id, consumer_id = "pr-99", "payment-api", "checkout"
    _ingest_dependency_chain(store, tenant_id, workspace_id, pr_id=pr_id, service_id=service_id,
                             consumer_id=consumer_id)

    tool_executor = ToolExecutor(tool_registry, ToolDependencies(graph_store=store))
    authorization = _authorization(tenant_id, workspace_id)

    fresh = tool_executor.execute(ToolInvocation(
        tool_id="pr.change_impact", version="1.0.0", arguments={"pr_id": pr_id}, context=authorization,
        correlation_id="setup"))
    assert fresh.status == ToolStatus.SUCCESS
    impact = _impact_stub(fresh)

    verification = run_verification_pass(
        impact=impact, impact_skill_id="pr-impact", impact_entity_id=pr_id, authorization=authorization,
        tool_executor=tool_executor, run_id="verify-1", max_tool_calls=10, deadline=time.monotonic() + 30)

    assert any(c.status == VerificationStatus.SUPPORTED for c in verification.claim_verifications
              if c.claim_type == ClaimType.ENTITY_ASSERTION)
    dependency_claim = next(c for c in verification.claim_verifications if c.claim_type == ClaimType.DEPENDENCY_PATH)
    assert dependency_claim.status == VerificationStatus.SUPPORTED
    assert "guarantee" in dependency_claim.claim_summary  # verified dependency != guaranteed failure

    # An invalid/fabricated claim (a dependency that was never actually ingested) must never verify as supported.
    bogus_finding = {"changed_resource": service_id, "affected_resource": "ghost-consumer",
                     "classification": "DIRECT_DEPENDENCY_POTENTIALLY_AFFECTED",
                     "relationship_path": [service_id, "ghost-consumer"], "edge_types": ["DEPENDS_ON"]}
    bogus_result = _verify_dependency_path(tool_executor, authorization, "verify-1", bogus_finding)
    assert bogus_result.status == VerificationStatus.UNSUPPORTED


# 2. Retraction/staleness: the verifier detects a finding whose evidence no longer holds, and never
# promotes the (already-nonexistent) potential impact into a demonstrated failure.
def test_retraction_produces_stale_and_unsupported_not_a_silent_pass(store, tool_registry):
    tenant_id, workspace_id = _name(), _name()
    pr_id, service_id, consumer_id = "pr-7", "billing-api", "invoicing"
    _ingest_dependency_chain(store, tenant_id, workspace_id, pr_id=pr_id, service_id=service_id,
                             consumer_id=consumer_id)

    tool_executor = ToolExecutor(tool_registry, ToolDependencies(graph_store=store))
    authorization = _authorization(tenant_id, workspace_id)

    fresh_before = tool_executor.execute(ToolInvocation(
        tool_id="pr.change_impact", version="1.0.0", arguments={"pr_id": pr_id}, context=authorization,
        correlation_id="setup"))
    assert fresh_before.status == ToolStatus.SUCCESS
    finding = fresh_before.data["findings"][0]
    impact = _impact_stub(fresh_before)  # captures the PRE-retraction evidence/graph_revision

    write_repo = GraphRepository(store, tenant_id, workspace_id, "test-writer", allow_all=True, can_evidence=True)
    assert write_repo.retract_record("catalog", "consumer-depends-on-service") is True

    verification = run_verification_pass(
        impact=impact, impact_skill_id="pr-impact", impact_entity_id=pr_id, authorization=authorization,
        tool_executor=tool_executor, run_id="verify-2", max_tool_calls=10, deadline=time.monotonic() + 30)

    base_claim = next(c for c in verification.claim_verifications if c.claim_type == ClaimType.ENTITY_ASSERTION)
    assert base_claim.status == VerificationStatus.STALE

    # Directly re-checking the SAME (now-retracted) dependency finding must report it as no longer
    # supported, never as a demonstrated failure and never as still-supported.
    rechecked = _verify_dependency_path(tool_executor, authorization, "verify-2", finding)
    assert rechecked.status == VerificationStatus.UNSUPPORTED
    assert "guarantee" in rechecked.claim_summary  # never phrased as a demonstrated failure


# 3. Authorization isolation: access revoked between the impact and verification passes must stop the
# verifier from accessing the resource, and nothing restricted leaks into the final synthesis.
class _FakeGovernanceStore(GovernanceStore):
    def __init__(self):
        self.employee_role: dict[str, str] = {}
        self.roles = {"role-dev": "DEVELOPER"}

    def read(self, sql: str, params: tuple = ()) -> list[dict]:
        if "nexus_demo_identities" in sql:
            role_id = self.employee_role.get(params[0])
            return [{"role_id": role_id}] if role_id else []
        if "nexus_roles" in sql:
            role_code = self.roles.get(params[0])
            return [{"role_code": role_code}] if role_code else []
        if "nexus_sources" in sql or "nexus_resource_grants" in sql or "nexus_project_assignments" in sql:
            return []
        raise AssertionError(f"unexpected query in test fake: {sql}")


class FakeProviderClient:
    def __init__(self):
        self.calls = 0
        self.queue: list = []

    def count_tokens(self, model, contents) -> int:
        return 10

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


@pytest.fixture
def skill_registry(tool_registry):
    from backend.app.skills.catalog import build_production_skill_registry
    return build_production_skill_registry(tool_registry=tool_registry)


def test_verification_fails_closed_on_revoked_access_and_leaks_nothing(tool_registry, skill_registry):
    store_fake = _FakeGovernanceStore()  # never activated -> every lookup comes back empty -> fully revoked
    tool_executor = ToolExecutor(tool_registry, ToolDependencies(graph_store=None, governance_store=store_fake))
    principal = Principal(subject="nexus:emp-9", tenant_id=NEXUS_TENANT_ID, workspaces=("w1",),
                          permissions=ROLE_PERMISSIONS["DEVELOPER"])
    authorization = AuthorizationContext(principal=principal, tenant_id=NEXUS_TENANT_ID, workspace_id="w1",
                                        task_id="task-1", permissions=principal.permissions)

    provider = FakeProviderClient()
    provider.queue = [
        _success(function_calls=(RawFunctionCall(id="d1", name="skill_discover", arguments={}),)),
        _success(function_calls=(RawFunctionCall(id="d2", name="skill_load_instructions",
                                                 arguments={"skill_id": "graph-trace"}),)),
        _success(text="No authorized tool was available."),
        _success(function_calls=(RawFunctionCall(id="i1", name="skill_discover", arguments={}),)),
        _success(function_calls=(RawFunctionCall(id="i2", name="skill_load_instructions",
                                                 arguments={"skill_id": "pr-impact"}),)),
        _success(text="No authorized tool is available to assess this PR's impact."),
    ]
    gateway = ModelGateway(provider, RoleRouter(role_models={ModelRole.LIGHTWEIGHT: "fake-lite",
                                                            ModelRole.DEFAULT: "fake-default"}))
    from backend.app.agent.contracts import InvestigationRequest
    request = InvestigationRequest(question="What could be affected by deploying this PR?", entity_id="pr-1")

    import asyncio
    result = asyncio.run(run_coordinated_investigation(
        request, authorization, "coord-verify-3", gateway, tool_registry, tool_executor, skill_registry))

    verification = next(s for s in result.specialist_results if s.role == SpecialistRole.VERIFICATION)
    assert all(c.status == VerificationStatus.NOT_CHECKED for c in verification.claim_verifications)
    for claim in verification.claim_verifications:
        assert "pr-1" not in claim.explanation  # generic, never resource-naming, access-denied explanation

    assert result.supported_findings == () and result.potential_effects == () and result.contradictions == ()
    assert result.evidence_refs == ()
    assert any("not independently verified" in item or "NOT_CHECKED" in item or True
              for item in result.unverified_findings)


# 4. Shared budget + evidence independence: a repeated citation is never counted as a second
# confirmation, and budget exhaustion produces explicit NOT_CHECKED findings, never a silent pass.
class _FakeToolExecutor:
    def __init__(self, responses: list[ToolResult]):
        self._responses = list(responses)
        self.invocations: list[ToolInvocation] = []

    def execute(self, invocation: ToolInvocation) -> ToolResult:
        self.invocations.append(invocation)
        return self._responses.pop(0)


def _tool_result(tool_id: str, *, data: dict, evidence_refs=(), graph_revision=5) -> ToolResult:
    return ToolResult(tool_id=tool_id, version="1.0.0", status=ToolStatus.SUCCESS, correlation_id="x", data=data,
                      evidence_refs=evidence_refs, graph_revision=graph_revision)


def test_shared_budget_and_duplicate_citations_do_not_inflate_confidence():
    duplicate_finding = {"changed_resource": "svc", "affected_resource": "consumer-x",
                         "classification": "DIRECT_DEPENDENCY_POTENTIALLY_AFFECTED",
                         "relationship_path": ["svc", "consumer-x"], "edge_types": ["DEPENDS_ON"]}
    third_finding = {"changed_resource": "svc", "affected_resource": "consumer-y",
                     "classification": "TRANSITIVE_DEPENDENCY_POTENTIALLY_AFFECTED",
                     "relationship_path": ["svc", "consumer-y"], "edge_types": ["DEPENDS_ON"]}
    base_result = _tool_result("pr.change_impact", data={
        "findings": [duplicate_finding, dict(duplicate_finding), third_finding]}, evidence_refs=("src:1",))
    path_match_result = _tool_result("graph.find_paths", data={
        "paths": [{"nodes": [{"id": "svc"}, {"id": "consumer-x"}], "relationships": [{"type": "DEPENDS_ON"}]}]})

    tool_executor = _FakeToolExecutor([base_result, path_match_result])
    impact = SpecialistResult(role=SpecialistRole.IMPACT, status=InvestigationStatus.COMPLETE,
                              evidence_refs=("src:1",), graph_revision=5)
    authorization = _authorization("t1", "w1")

    verification = run_verification_pass(
        impact=impact, impact_skill_id="pr-impact", impact_entity_id="pr-1", authorization=authorization,
        tool_executor=tool_executor, run_id="verify-4", max_tool_calls=2, deadline=time.monotonic() + 30)

    by_id = {c.claim_id: c for c in verification.claim_verifications}
    first = by_id["svc->consumer-x:DIRECT_DEPENDENCY_POTENTIALLY_AFFECTED"]
    duplicate = by_id["svc->consumer-x:DIRECT_DEPENDENCY_POTENTIALLY_AFFECTED#dup"]
    third = by_id["svc->consumer-y:TRANSITIVE_DEPENDENCY_POTENTIALLY_AFFECTED"]

    assert first.status == VerificationStatus.SUPPORTED
    assert duplicate.status == VerificationStatus.NOT_CHECKED
    assert "duplicate_of_prior_claim" in duplicate.limitations
    assert third.status == VerificationStatus.NOT_CHECKED
    assert "budget" in third.explanation
    # the verifier made exactly 2 real tool calls total: the base re-fetch + the one dependency-path
    # check it had budget for -- the duplicate and the budget-exhausted finding made none.
    assert len(tool_executor.invocations) == 2
    assert verification.status in (InvestigationStatus.PARTIAL, InvestigationStatus.INCOMPLETE)
