"""ND-14B: access-aware pauses, human-to-human investigation handoff, and
permission-aware Investigation Handoff Packets.

Uses a real, isolated PostgreSQL container (same convention as
``test_agent_persistence.py``) for both ``InvestigationStore`` and, in test 1,
a real ``GovernanceStore`` (same schema, same database) seeded with the
minimal Stage 09 rows needed to exercise the real
``create_access_request``/``approve_request`` lifecycle end to end.
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
    ClaimType, ClaimVerificationResult, CoordinatorBudget, InvestigationStatus, SpecialistResult,
    SpecialistRole, VerificationStatus,
)
from backend.app.agent.coordinator import resume_coordinated_investigation
from backend.app.agent.handoff import build_handoff_packet
from backend.app.agent.persistence import HandoffConflict, InvestigationStore
from backend.app.gateway.contracts import ModelRole
from backend.app.gateway.gateway import ModelGateway
from backend.app.gateway.provider import ProviderResponse, RawUsage
from backend.app.gateway.routing import RoleRouter
from backend.app.governance.access_requests import approve_request, create_access_request
from backend.app.governance.identity import NEXUS_TENANT_ID, ROLE_PERMISSIONS
from backend.app.governance.store import GovernanceStore
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


class _FakeToolExecutor:
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


def _router() -> RoleRouter:
    return RoleRouter(role_models={ModelRole.LIGHTWEIGHT: "fake-lite", ModelRole.DEFAULT: "fake-default"})


_QUESTION = "What could be affected by deploying this PR?"
_FULL_PERMISSIONS = {Permission.READ_GRAPH, Permission.READ_EVIDENCE, Permission.EXECUTE_TOOL}


def _authorization(tenant_id: str, subject: str, permissions, source_ids: tuple[str, ...] = ()) -> AuthorizationContext:
    permissions = frozenset(permissions)
    principal = Principal(subject=subject, tenant_id=tenant_id, workspaces=("w1",), permissions=permissions,
                          source_ids=source_ids)
    return AuthorizationContext(principal=principal, tenant_id=tenant_id, workspace_id="w1", task_id="t",
                                permissions=permissions)


# 1. Access pause and Stage 09 approval: a run loses source-level access (not just role permission),
# pauses safely with zero further execution, and a real Stage 09 approval is what lets it continue.
def test_access_pause_and_stage09_approval_enables_resume(isolated_postgres, store, tool_registry, skill_registry):
    governance_store = GovernanceStore(isolated_postgres)
    governance_store.start()

    company_id = "acme"
    employee_id = f"emp{_name()[:10]}"
    subject = f"nexus:{employee_id}"
    source_id = f"SRC{_name()[:10]}"
    role_id = "role-dev"

    with governance_store.transaction() as cur:
        cur.execute("""INSERT INTO nexus_roles (tenant_id, company_id, role_id, role_code, data)
                      VALUES (%s,%s,%s,%s,'{}'::jsonb) ON CONFLICT DO NOTHING""",
                   (NEXUS_TENANT_ID, company_id, role_id, "DEVELOPER"))
        cur.execute("""INSERT INTO nexus_employees (tenant_id, company_id, employee_id, display_name, data)
                      VALUES (%s,%s,%s,%s,'{}'::jsonb)""", (NEXUS_TENANT_ID, company_id, employee_id, "Tester"))
        cur.execute("""INSERT INTO nexus_demo_identities
                          (tenant_id, company_id, demo_identity_id, demo_token, employee_id, role_id, data)
                      VALUES (%s,%s,%s,%s,%s,%s,'{}'::jsonb)""",
                   (NEXUS_TENANT_ID, company_id, f"DI-{employee_id}", f"tok-{employee_id}", employee_id, role_id))
        cur.execute("""INSERT INTO nexus_sources
                          (tenant_id, company_id, source_id, stage, classification, selectable,
                           eligible_role_codes, data)
                      VALUES (%s,%s,%s,'7B','CROSS_PROJECT_CONFIDENTIAL',true,%s,'{}'::jsonb)""",
                   (NEXUS_TENANT_ID, company_id, source_id, ["DEVELOPER"]))

    tool_executor = _FakeToolExecutorWithGovernance(governance_store)
    principal = Principal(subject=subject, tenant_id=NEXUS_TENANT_ID, workspaces=("w1",),
                          permissions=ROLE_PERMISSIONS["DEVELOPER"])
    authorization = AuthorizationContext(principal=principal, tenant_id=NEXUS_TENANT_ID, workspace_id="w1",
                                        task_id="t", permissions=principal.permissions)

    run_id = f"run-{_name()}"
    store.create_run(run_id=run_id, tenant_id=NEXUS_TENANT_ID, workspace_id="w1", principal_subject=subject,
                     objective=_QUESTION, entity_id="pr-1", budget=CoordinatorBudget())
    discovery = SpecialistResult(role=SpecialistRole.DISCOVERY, status=InvestigationStatus.COMPLETE,
                                 entity_ids=("pr-1",), evidence_refs=(f"{source_id}:rec-1",), graph_revision=5,
                                 summary="discovered pr-1")
    store.save_checkpoint(run_id, SpecialistRole.DISCOVERY, discovery, 5)

    provider = FakeProviderClient()
    provider.queue = []  # a paused run must not touch the model at all
    gateway = ModelGateway(provider, _router())

    result_before = asyncio.run(resume_coordinated_investigation(
        run_id, authorization, gateway, tool_registry, tool_executor, skill_registry, store, worker_id="w1"))
    assert result_before.status == InvestigationStatus.BLOCKED
    assert provider.calls == 0
    assert store.get_run(run_id)["status"] == "PAUSED_ACCESS"
    assert source_id not in result_before.summary  # generic pause reason, no restricted source name

    request = create_access_request(governance_store, requester_employee_id=employee_id, company_id=company_id,
                                    resource_id=source_id, requested_actions=["READ"],
                                    justification="investigation needs this source")
    approve_request(governance_store, request_id=request["request_id"], company_id=company_id,
                    approver_employee_id="admin-1")

    result_after = asyncio.run(resume_coordinated_investigation(
        run_id, authorization, gateway, tool_registry, tool_executor, skill_registry, store, worker_id="w1"))
    assert result_after.status != InvestigationStatus.BLOCKED  # fresh permission evaluation picked up the grant
    assert store.get_run(run_id)["status"] != "PAUSED_ACCESS"


class _FakeToolExecutorWithGovernance:
    """A real ``ToolDependencies(governance_store=...)`` wiring so ``refresh_authorization`` genuinely
    re-resolves live grant state, without needing a real Neo4j for the graph tools this test never calls
    (DISCOVERY/IMPACT/SYNTHESIS all fail deterministically with ``graph_store=None``, which is fine here
    since this test is only about the pause/resume authorization transition, already covered elsewhere)."""

    def __init__(self, governance_store: GovernanceStore):
        from backend.app.tools.executor import ToolExecutor
        registry = ToolRegistry()
        register_all_tools(registry)
        self.dependencies = ToolDependencies(graph_store=None, governance_store=governance_store)
        self._executor = ToolExecutor(registry, self.dependencies)

    def execute(self, invocation):
        return self._executor.execute(invocation)


# 2. Authorized handoff: ownership transfers, the recipient's packet preserves objective/progress/
# findings/evidence/verification, and resume restores all three completed phases without re-executing them.
def test_authorized_handoff_packet_and_resume(store, tool_registry, skill_registry):
    tenant_id = _name()
    run_id = f"run-{_name()}"
    sender, recipient = "alice", "bob"
    store.create_run(run_id=run_id, tenant_id=tenant_id, workspace_id="w1", principal_subject=sender,
                     objective=_QUESTION, entity_id="pr-1", budget=CoordinatorBudget())

    discovery = SpecialistResult(role=SpecialistRole.DISCOVERY, status=InvestigationStatus.COMPLETE,
                                 entity_ids=("pr-1",), evidence_refs=("src:1",), graph_revision=5, summary="d")
    impact = SpecialistResult(role=SpecialistRole.IMPACT, status=InvestigationStatus.COMPLETE,
                              entity_ids=("svc-a", "svc-b"), evidence_refs=("src:1",), graph_revision=5,
                              tool_ids_called=("pr.change_impact",), tool_calls_made=1, summary="i")
    verification = SpecialistResult(role=SpecialistRole.VERIFICATION, status=InvestigationStatus.COMPLETE,
                                    graph_revision=5, claim_verifications=(ClaimVerificationResult(
                                        claim_id="c1", claim_type=ClaimType.DEPENDENCY_PATH,
                                        claim_summary="svc-a->svc-b", status=VerificationStatus.SUPPORTED),),
                                    summary="v")
    store.save_checkpoint(run_id, SpecialistRole.DISCOVERY, discovery, 5)
    store.save_checkpoint(run_id, SpecialistRole.IMPACT, impact, 5)
    store.save_checkpoint(run_id, SpecialistRole.VERIFICATION, verification, 5)
    # Interrupted before SYNTHESIS -- exactly the point a handoff is most useful.

    handoff_id = store.request_handoff(run_id, tenant_id=tenant_id, sender_subject=sender,
                                       recipient_subject=recipient)
    decided = store.decide_handoff(handoff_id, recipient_subject=recipient, accept=True)
    assert decided["status"] == "ACCEPTED"
    assert store.get_run(run_id)["assignee_subject"] == recipient
    assert store.get_run(run_id)["principal_subject"] == sender  # original creator stays immutable

    recipient_auth = _authorization(tenant_id, recipient, _FULL_PERMISSIONS)
    fresh_executor = _FakeToolExecutor(graph_revision=5)
    packet = build_handoff_packet(run_id, recipient_auth, fresh_executor, store, intended_recipient=recipient)
    assert packet is not None
    assert packet.objective == _QUESTION
    assert packet.original_creator == sender and packet.current_owner == recipient
    assert set(packet.completed_phases) == {"DISCOVERY", "IMPACT", "VERIFICATION"}
    assert packet.remaining_work == ("SYNTHESIS",)
    assert "pr-1" in packet.entity_ids and "svc-a" in packet.entity_ids
    assert packet.evidence_refs == ("src:1",)
    assert packet.graph_revision == 5
    assert any("svc-a->svc-b" in f for f in packet.potential_impacts)  # DEPENDENCY_PATH -> potential, not supported

    # The previous owner cannot continue after transfer; the recipient can, and nothing already
    # completed is re-executed (only SYNTHESIS, which is deterministic Python, remains).
    sender_auth = _authorization(tenant_id, sender, _FULL_PERMISSIONS)
    sender_provider = FakeProviderClient()
    result_sender = asyncio.run(resume_coordinated_investigation(
        run_id, sender_auth, ModelGateway(sender_provider, _router()), tool_registry, fresh_executor,
        skill_registry, store, worker_id="sender-w"))
    assert result_sender.status == InvestigationStatus.BLOCKED

    recipient_provider = FakeProviderClient()
    result_recipient = asyncio.run(resume_coordinated_investigation(
        run_id, recipient_auth, ModelGateway(recipient_provider, _router()), tool_registry, fresh_executor,
        skill_registry, store, worker_id="recipient-w"))
    assert recipient_provider.calls == 0  # DISCOVERY/IMPACT restored; VERIFICATION already checkpointed too
    by_role = {s.role: s for s in result_recipient.specialist_results}
    assert by_role[SpecialistRole.DISCOVERY] == discovery
    assert by_role[SpecialistRole.IMPACT] == impact


# 3. Unauthorized disclosure: a recipient lacking the required source scope gets no packet at all (fail
# closed, no partial redaction), and cross-tenant handoff/packet access is rejected outright.
def test_unauthorized_recipient_gets_no_packet_and_cross_tenant_is_rejected(store, tool_registry, skill_registry):
    tenant_id = _name()
    run_id = f"run-{_name()}"
    sender, restricted_recipient = "alice", "carol"
    store.create_run(run_id=run_id, tenant_id=tenant_id, workspace_id="w1", principal_subject=sender,
                     objective=_QUESTION, entity_id="pr-1", budget=CoordinatorBudget())
    store.save_checkpoint(run_id, SpecialistRole.DISCOVERY, SpecialistResult(
        role=SpecialistRole.DISCOVERY, status=InvestigationStatus.COMPLETE, entity_ids=("pr-1",),
        evidence_refs=("secret-src:rec-1",), graph_revision=5, summary="discovered pr-1"), 5)

    handoff_id = store.request_handoff(run_id, tenant_id=tenant_id, sender_subject=sender,
                                       recipient_subject=restricted_recipient)
    store.decide_handoff(handoff_id, recipient_subject=restricted_recipient, accept=True)

    fresh_executor = _FakeToolExecutor(graph_revision=5)
    restricted_auth = _authorization(tenant_id, restricted_recipient, _FULL_PERMISSIONS,
                                     source_ids=("some-other-source",))
    packet = build_handoff_packet(run_id, restricted_auth, fresh_executor, store,
                                  intended_recipient=restricted_recipient)
    assert packet is None  # fail closed -- no partial packet, no disclosed source name or count

    # Ownership transfer is independent of packet visibility (section 4), but the recipient's own
    # authorization object is never widened by the handoff itself -- no source grant was copied.
    assert store.get_run(run_id)["assignee_subject"] == restricted_recipient
    assert restricted_auth.principal.source_ids == ("some-other-source",)

    other_tenant_auth = _authorization(_name(), "mallory", _FULL_PERMISSIONS)
    assert build_handoff_packet(run_id, other_tenant_auth, fresh_executor, store) is None
    with pytest.raises(HandoffConflict):
        store.request_handoff(run_id, tenant_id=other_tenant_auth.tenant_id, sender_subject=sender,
                              recipient_subject="mallory")


# 4 (optional, but section 4 requires it). Concurrent/duplicate handoff decisions and a stale previous-
# owner resume attempt must never produce more than one valid ownership transition.
def test_handoff_prevents_duplicate_acceptance_and_stale_owner_resume(store, tool_registry, skill_registry):
    tenant_id = _name()
    run_id = f"run-{_name()}"
    sender, recipient = "alice", "bob"
    store.create_run(run_id=run_id, tenant_id=tenant_id, workspace_id="w1", principal_subject=sender,
                     objective=_QUESTION, entity_id="pr-1", budget=CoordinatorBudget())

    handoff_id = store.request_handoff(run_id, tenant_id=tenant_id, sender_subject=sender,
                                       recipient_subject=recipient)
    first = store.decide_handoff(handoff_id, recipient_subject=recipient, accept=True)
    assert first["status"] == "ACCEPTED"

    # A second "concurrent" decision on the SAME handoff -- whether a duplicate accept or a late decline
    # -- must never succeed once the first has committed.
    with pytest.raises(HandoffConflict):
        store.decide_handoff(handoff_id, recipient_subject=recipient, accept=True)
    with pytest.raises(HandoffConflict):
        store.decide_handoff(handoff_id, recipient_subject=recipient, accept=False)

    # A second, unrelated handoff request from the now-former owner must also fail -- they no longer own
    # the run.
    with pytest.raises(HandoffConflict):
        store.request_handoff(run_id, tenant_id=tenant_id, sender_subject=sender, recipient_subject="carol")

    assert store.get_run(run_id)["assignee_subject"] == recipient
    events = {e["event_type"] for e in store.list_events(run_id)}
    assert "HANDOFF_REQUESTED" in events and "HANDOFF_ACCEPTED" in events
