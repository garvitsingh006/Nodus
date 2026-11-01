"""ND-14A: durable PostgreSQL-backed investigation checkpoints and bounded,
authorization-rechecked resume.

Uses a real, isolated PostgreSQL container (same ``isolated_postgres``
fixture convention as ``test_ingestion_sync.py``) for the actual persistence
layer. Discovery/impact tool execution is stubbed with a small deterministic
fake executor (same convention as ``test_agent_model_routing.py``'s
``_FakeDeterministicToolExecutor``) so these tests stay fast and focus on
checkpoint/resume/authorization mechanics rather than re-testing specialist
correctness already covered elsewhere.
"""
from __future__ import annotations

import asyncio
import secrets
import socket
import subprocess
import time
import uuid

import psycopg
import pytest

from backend.app.agent.contracts import (
    ClaimType, ClaimVerificationResult, CoordinatorBudget, InvestigationStatus, SpecialistResult, SpecialistRole,
    VerificationStatus,
)
from backend.app.agent.coordinator import resume_coordinated_investigation
from backend.app.agent.persistence import InvestigationStore, RunClaimDenied
from backend.app.gateway.contracts import ModelRole
from backend.app.gateway.gateway import ModelGateway
from backend.app.gateway.provider import ProviderResponse, RawFunctionCall, RawUsage
from backend.app.gateway.routing import RoleRouter
from backend.app.security import AuthorizationContext, Permission, Principal
from backend.app.skills.catalog import build_production_skill_registry
from backend.app.tools import register_all_tools
from backend.app.tools.contracts import ToolResult, ToolStatus
from backend.app.tools.registry import ToolDependencies, ToolRegistry

pytestmark = pytest.mark.integration


def _name() -> str:
    return uuid.uuid4().hex


@pytest.fixture(scope="module")
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
        for _ in range(180):
            try:
                with psycopg.connect(dsn, connect_timeout=2):
                    break
            except Exception:
                time.sleep(1)
        else:
            pytest.fail("BLOCKED: isolated PostgreSQL did not become ready (environment, not code)")
        yield dsn
    finally:
        subprocess.run(["docker", "rm", "-f", container], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


@pytest.fixture
def store(isolated_postgres) -> InvestigationStore:
    s = InvestigationStore(isolated_postgres)
    s.start()
    return s


@pytest.fixture
def tool_registry() -> ToolRegistry:
    registry = ToolRegistry()
    register_all_tools(registry)
    return registry


@pytest.fixture
def skill_registry(tool_registry):
    return build_production_skill_registry(tool_registry=tool_registry)


_PR_DATA = {
    "pr_id": "pr-1", "change_state": "OPEN", "deployment_authorized": False, "deployment_evidence_refs": [],
    "change_state_uncertainty": None,
    "changed_resources": [{"entity_id": "svc-a", "type": "SERVICE", "name": "svc-a", "via": "declared"}],
    "findings": [{
        "changed_resource": "svc-a", "affected_resource": "svc-b", "relationship_path": ["svc-a", "svc-b"],
        "edge_types": ["DEPENDS_ON"], "evidence_refs": ["src:1"], "source_revisions": [None],
        "classification": "DIRECT_DEPENDENCY_POTENTIALLY_AFFECTED", "explanation": "x", "uncertainty": "x",
        "suggested_verification": "x",
    }],
    "unmapped_changed_files": [], "coverage": "source_backed_only",
}


class _FakeToolExecutor:
    """Deterministic stand-in for the real graph-backed ToolExecutor, so VERIFICATION (and, if exercised,
    IMPACT) can complete without a live Neo4j. ``graph_revision`` is mutable so a test can simulate the
    graph changing between checkpoint creation and resume."""

    def __init__(self, graph_revision: int = 5):
        self.dependencies = ToolDependencies()
        self.graph_revision = graph_revision
        self.invocations: list = []

    def execute(self, invocation) -> ToolResult:
        self.invocations.append(invocation)
        if invocation.tool_id == "graph.revision":
            return ToolResult(tool_id=invocation.tool_id, version=invocation.version, status=ToolStatus.SUCCESS,
                              correlation_id="x", data={"revision": self.graph_revision},
                              graph_revision=self.graph_revision)
        if invocation.tool_id == "pr.change_impact":
            return ToolResult(tool_id=invocation.tool_id, version=invocation.version, status=ToolStatus.SUCCESS,
                              correlation_id="x", data=_PR_DATA, evidence_refs=("src:1",),
                              graph_revision=self.graph_revision)
        if invocation.tool_id == "graph.find_paths":
            # Matches _PR_DATA's one finding (relationship_path=["svc-a","svc-b"], edge_types=["DEPENDS_ON"])
            # so the verifier's independent re-derivation actually confirms it as SUPPORTED.
            return ToolResult(tool_id=invocation.tool_id, version=invocation.version, status=ToolStatus.SUCCESS,
                              correlation_id="x",
                              data={"paths": [{"nodes": [{"id": "svc-a"}, {"id": "svc-b"}],
                                              "relationships": [{"type": "DEPENDS_ON"}]}],
                                   "coverage": "source_backed_only"},
                              graph_revision=self.graph_revision)
        return ToolResult(tool_id=invocation.tool_id, version=invocation.version, status=ToolStatus.NOT_FOUND,
                          correlation_id="x")


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


def _router() -> RoleRouter:
    return RoleRouter(role_models={ModelRole.LIGHTWEIGHT: "fake-lite", ModelRole.DEFAULT: "fake-default"})


_QUESTION = "What could be affected by deploying this PR?"


def _authorization(tenant_id: str, subject: str, permissions) -> AuthorizationContext:
    permissions = frozenset(permissions)
    principal = Principal(subject=subject, tenant_id=tenant_id, workspaces=("w1",), permissions=permissions)
    return AuthorizationContext(principal=principal, tenant_id=tenant_id, workspace_id="w1", task_id="resume-task",
                                permissions=permissions)


_FULL_PERMISSIONS = {Permission.READ_GRAPH, Permission.READ_EVIDENCE, Permission.EXECUTE_TOOL}


# 1. Durable recovery: DISCOVERY and IMPACT checkpoints already persisted (simulating an interruption
# right after them) are restored -- never re-executed -- by a resume against a BRAND NEW
# InvestigationStore/ToolExecutor instance, and VERIFICATION + SYNTHESIS finish correctly from there.
def test_resume_restores_completed_phases_without_reexecuting_them(store, tool_registry, skill_registry):
    tenant_id, subject = _name(), "tester"
    run_id = f"run-{_name()}"
    authorization = _authorization(tenant_id, subject, _FULL_PERMISSIONS)
    budget = CoordinatorBudget()

    store.create_run(run_id=run_id, tenant_id=tenant_id, workspace_id="w1", principal_subject=subject,
                     objective=_QUESTION, entity_id="pr-1", budget=budget)
    discovery_result = SpecialistResult(role=SpecialistRole.DISCOVERY, status=InvestigationStatus.COMPLETE,
                                        entity_ids=("pr-1",), graph_revision=5, summary="discovered pr-1")
    impact_result = SpecialistResult(role=SpecialistRole.IMPACT, status=InvestigationStatus.COMPLETE,
                                     entity_ids=("svc-a", "svc-b"), evidence_refs=("src:1",), graph_revision=5,
                                     tool_ids_called=("pr.change_impact",), tool_calls_made=1,
                                     summary="impact analysis done")
    store.save_checkpoint(run_id, SpecialistRole.DISCOVERY, discovery_result, 5)
    store.save_checkpoint(run_id, SpecialistRole.IMPACT, impact_result, 5)
    # The run's own row is left at status='RUNNING' (never finished) -- exactly what an interruption
    # between IMPACT and VERIFICATION would leave behind.

    # A brand new store instance (same DSN) and tool executor, as the task requires ("new process").
    fresh_store = InvestigationStore(store.dsn)
    fresh_executor = _FakeToolExecutor(graph_revision=5)
    provider = FakeProviderClient()
    provider.queue = []  # if DISCOVERY or IMPACT were re-executed, popping this empty queue would crash
    gateway = ModelGateway(provider, _router())

    result = asyncio.run(resume_coordinated_investigation(
        run_id, authorization, gateway, tool_registry, fresh_executor, skill_registry, fresh_store,
        worker_id="worker-2"))

    assert provider.calls == 0  # proof DISCOVERY/IMPACT were restored, never re-run
    by_role = {s.role: s for s in result.specialist_results}
    assert by_role[SpecialistRole.DISCOVERY] == discovery_result
    assert by_role[SpecialistRole.IMPACT] == impact_result
    assert by_role[SpecialistRole.VERIFICATION].status == InvestigationStatus.COMPLETE
    assert any(c.status.value == "SUPPORTED" for c in by_role[SpecialistRole.VERIFICATION].claim_verifications)
    assert result.status == InvestigationStatus.COMPLETE

    row = fresh_store.get_run(run_id)
    assert row["status"] == "COMPLETED"


# 2. Authorization changes: a run cannot be resumed under permissions weaker than it needs, and a
# different tenant/identity cannot resume someone else's run -- both fail with the same generic denial,
# and nothing persisted leaks into either failure's result.
def test_resume_enforces_current_authorization_and_identity(store, tool_registry, skill_registry):
    tenant_id, subject = _name(), "tester"
    run_id = f"run-{_name()}"
    store.create_run(run_id=run_id, tenant_id=tenant_id, workspace_id="w1", principal_subject=subject,
                     objective=_QUESTION, entity_id="pr-1", budget=CoordinatorBudget())
    store.save_checkpoint(run_id, SpecialistRole.DISCOVERY, SpecialistResult(
        role=SpecialistRole.DISCOVERY, status=InvestigationStatus.COMPLETE, entity_ids=("pr-1",),
        evidence_refs=("secret-src:1",), graph_revision=5, summary="discovered pr-1"), 5)

    fresh_executor = _FakeToolExecutor(graph_revision=5)
    gateway = ModelGateway(FakeProviderClient(), _router())

    # 2a. Access has since been revoked (READ_EVIDENCE missing) -- must stop safely, not use the stale
    # permission snapshot from when the run started.
    revoked_auth = _authorization(tenant_id, subject, {Permission.READ_GRAPH, Permission.EXECUTE_TOOL})
    result_revoked = asyncio.run(resume_coordinated_investigation(
        run_id, revoked_auth, gateway, tool_registry, fresh_executor, skill_registry, store, worker_id="w-a"))
    assert result_revoked.status == InvestigationStatus.BLOCKED
    assert result_revoked.evidence_refs == () and result_revoked.entity_ids == ()
    assert "secret-src:1" not in result_revoked.summary

    # 2b. A different tenant/identity must never resume this run, and the denial must look identical to
    # the revoked-access case above (a generic message, not "wrong tenant" vs "no permission").
    other_auth = _authorization(_name(), "someone-else", _FULL_PERMISSIONS)
    result_other = asyncio.run(resume_coordinated_investigation(
        run_id, other_auth, gateway, tool_registry, fresh_executor, skill_registry, store, worker_id="w-b"))
    assert result_other.status == InvestigationStatus.BLOCKED
    assert result_other.evidence_refs == () and result_other.entity_ids == ()
    # Neither denial is specific enough to let a caller distinguish "not your run" from "it exists but
    # isn't yours" by probing -- no tenant id, subject, or permission name ever appears in either message.
    for summary in (result_revoked.summary, result_other.summary):
        assert tenant_id not in summary and subject not in summary and "READ_EVIDENCE" not in summary


# 3. Checkpoint idempotency, exclusive concurrent-resume ownership, and budget-usage carryover.
def test_checkpoint_idempotency_and_exclusive_concurrent_resume(store, tool_registry, skill_registry):
    tenant_id, subject = _name(), "tester"
    run_id = f"run-{_name()}"
    store.create_run(run_id=run_id, tenant_id=tenant_id, workspace_id="w1", principal_subject=subject,
                     objective=_QUESTION, entity_id="pr-1", budget=CoordinatorBudget())

    # 3a. Repeating the same checkpoint write is idempotent -- never a duplicate phase row.
    first = SpecialistResult(role=SpecialistRole.DISCOVERY, status=InvestigationStatus.COMPLETE,
                             entity_ids=("pr-1",), graph_revision=5, summary="v1")
    store.save_checkpoint(run_id, SpecialistRole.DISCOVERY, first, 5)
    store.save_checkpoint(run_id, SpecialistRole.DISCOVERY, first, 5)
    updated = SpecialistResult(role=SpecialistRole.DISCOVERY, status=InvestigationStatus.COMPLETE,
                               entity_ids=("pr-1", "pr-2"), graph_revision=6, summary="v2")
    store.save_checkpoint(run_id, SpecialistRole.DISCOVERY, updated, 6)
    checkpoints = store.load_checkpoints(run_id)
    assert len(checkpoints) == 1  # one row for this (run_id, phase), not three
    assert checkpoints[SpecialistRole.DISCOVERY].summary == "v2"  # the latest write wins, not the first

    # 3b. Only one of two "concurrent" resume attempts may successfully claim the same run.
    authorization = _authorization(tenant_id, subject, _FULL_PERMISSIONS)
    claimed = store.claim_for_resume(run_id, tenant_id=tenant_id, principal_subject=subject, worker_id="worker-A")
    assert claimed["status"] == "RUNNING" and claimed["claimed_by"] == "worker-A"
    with pytest.raises(RunClaimDenied):
        store.claim_for_resume(run_id, tenant_id=tenant_id, principal_subject=subject, worker_id="worker-B")

    # 3c. Budget usage already spent before the interruption is preserved (not reset) across resume: seed
    # a nonzero usage snapshot, then resume to completion and confirm the final usage only grew from it.
    # worker-A's own claim-lease from 3b is expired first, simulating that original attempt's process
    # having ended (crashed or finished) rather than still actively holding the run.
    with store._transaction() as cur:
        cur.execute("""
            UPDATE nodus_investigation_runs
            SET lease_expires_at = now() - interval '1 second',
                usage_json = '{"tool_calls_used": 3, "llm_calls_used": 2, "elapsed_seconds": 1.0}'
            WHERE run_id = %s
            """, (run_id,))
    fresh_executor = _FakeToolExecutor(graph_revision=6)
    provider = FakeProviderClient()
    # Only DISCOVERY was checkpointed in this test; IMPACT still has to run fresh and will make its one
    # bounded narrative call over the (fake-tool-provided) findings.
    provider.queue = [_success(text="svc-a may potentially affect svc-b; not a confirmed failure.")]
    gateway = ModelGateway(provider, _router())
    result = asyncio.run(resume_coordinated_investigation(
        run_id, authorization, gateway, tool_registry, fresh_executor, skill_registry, store,
        worker_id="worker-A"))
    assert result.status in (InvestigationStatus.COMPLETE, InvestigationStatus.PARTIAL,
                             InvestigationStatus.INCOMPLETE)
    final_row = store.get_run(run_id)
    assert final_row["usage_json"]["tool_calls_used"] >= 3  # carried forward, never reset to 0


# 4 (optional). A graph revision that moved since a VERIFICATION checkpoint was written must never let
# that checkpoint's claims be presented as still current -- DISCOVERY/IMPACT stay restored as-is, but
# VERIFICATION is re-derived against the new revision (ND-14A section 7).
def test_resume_with_changed_graph_revision_invalidates_stale_verification(store, tool_registry, skill_registry):
    tenant_id, subject = _name(), "tester"
    run_id = f"run-{_name()}"
    store.create_run(run_id=run_id, tenant_id=tenant_id, workspace_id="w1", principal_subject=subject,
                     objective=_QUESTION, entity_id="pr-1", budget=CoordinatorBudget())

    discovery_result = SpecialistResult(role=SpecialistRole.DISCOVERY, status=InvestigationStatus.COMPLETE,
                                        entity_ids=("pr-1",), graph_revision=5, summary="discovered pr-1")
    impact_result = SpecialistResult(role=SpecialistRole.IMPACT, status=InvestigationStatus.COMPLETE,
                                     entity_ids=("svc-a", "svc-b"), evidence_refs=("src:1",), graph_revision=5,
                                     tool_ids_called=("pr.change_impact",), tool_calls_made=1, summary="impact done")
    stale_claim = ClaimVerificationResult(claim_id="stale", claim_type=ClaimType.DEPENDENCY_PATH,
                                          claim_summary="stale claim from before the graph changed",
                                          status=VerificationStatus.SUPPORTED)
    stale_verification = SpecialistResult(role=SpecialistRole.VERIFICATION, status=InvestigationStatus.COMPLETE,
                                          graph_revision=5, claim_verifications=(stale_claim,),
                                          summary="verification as of revision 5")
    store.save_checkpoint(run_id, SpecialistRole.DISCOVERY, discovery_result, 5)
    store.save_checkpoint(run_id, SpecialistRole.IMPACT, impact_result, 5)
    store.save_checkpoint(run_id, SpecialistRole.VERIFICATION, stale_verification, 5)
    # Run never reached SYNTHESIS -- interrupted right after VERIFICATION, which is exactly the case
    # where a stale checkpoint could otherwise be presented as still current.

    fresh_executor = _FakeToolExecutor(graph_revision=99)  # the graph has moved since the checkpoint
    provider = FakeProviderClient()
    provider.queue = []  # DISCOVERY/IMPACT must still never be re-executed
    gateway = ModelGateway(provider, _router())
    authorization = _authorization(tenant_id, subject, _FULL_PERMISSIONS)

    result = asyncio.run(resume_coordinated_investigation(
        run_id, authorization, gateway, tool_registry, fresh_executor, skill_registry, store, worker_id="w-stale"))

    assert provider.calls == 0  # DISCOVERY/IMPACT restored, never rerun, even though VERIFICATION is redone
    by_role = {s.role: s for s in result.specialist_results}
    assert by_role[SpecialistRole.DISCOVERY] == discovery_result
    assert by_role[SpecialistRole.IMPACT] == impact_result
    assert any("graph revision changed" in note and "5" in note and "99" in note
              for note in result.incomplete_work)
    fresh_verification = by_role[SpecialistRole.VERIFICATION]
    assert fresh_verification.claim_verifications != (stale_claim,)  # the stale claim was not reused as-is
    assert fresh_verification.graph_revision == 99  # re-derived against the CURRENT revision, not the old one
