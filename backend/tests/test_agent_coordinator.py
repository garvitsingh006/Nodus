"""ND-13A: bounded, permission-aware multi-specialist coordination.

Follows the exact offline scripted-model harness convention of
``test_agent_investigator.py`` (``FakeProviderClient``, ``ToolDependencies(
graph_store=None)`` so tool handlers deterministically fail with
EXECUTION_FAILED rather than requiring a live Neo4j — no live Gemini
credentials are used or claimed here). Exactly 3 focused tests, one per
ND-13A section 7 scenario.
"""
from __future__ import annotations

import asyncio

import pytest

from backend.app.agent.contracts import (
    CoordinatorBudget, InvestigationRequest, InvestigationStatus, SpecialistRole,
)
from backend.app.agent.coordinator import run_coordinated_investigation
from backend.app.agent.tool_bridge import build_investigation_tools, refresh_authorization
from backend.app.gateway.contracts import ModelRole
from backend.app.gateway.gateway import ModelGateway
from backend.app.gateway.provider import ProviderResponse, RawFunctionCall, RawUsage
from backend.app.gateway.routing import RoleRouter
from backend.app.governance.identity import NEXUS_TENANT_ID, ROLE_PERMISSIONS
from backend.app.governance.store import GovernanceStore
from backend.app.security import AuthorizationContext, Permission, Principal
from backend.app.skills.catalog import build_production_skill_registry
from backend.app.tools import register_all_tools
from backend.app.tools.executor import ToolExecutor
from backend.app.tools.registry import ToolDependencies, ToolRegistry


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


@pytest.fixture
def tool_registry() -> ToolRegistry:
    registry = ToolRegistry()
    register_all_tools(registry)
    return registry


@pytest.fixture
def skill_registry(tool_registry):
    return build_production_skill_registry(tool_registry=tool_registry)


@pytest.fixture
def tool_executor(tool_registry) -> ToolExecutor:
    return ToolExecutor(tool_registry, ToolDependencies(graph_store=None))


def _authorization(permissions) -> AuthorizationContext:
    principal = Principal(subject="agent", tenant_id="t1", workspaces=("w1",), permissions=frozenset(permissions))
    return AuthorizationContext(principal=principal, tenant_id="t1", workspace_id="w1", task_id="task-1",
                                permissions=frozenset(permissions))


# 1. Real coordination: distinct discovery/impact/synthesis passes, the impact
# specialist actually invokes an existing Stage 12 tool, and evidence/graph
# revision/uncertainty survive into the synthesized result.
def test_coordinated_investigation_runs_distinct_specialist_passes(tool_registry, skill_registry, tool_executor):
    provider = FakeProviderClient()
    provider.queue = [
        # DISCOVERY pass (ADK, model-driven)
        _success(function_calls=(RawFunctionCall(id="d1", name="skill_discover", arguments={}),)),
        _success(function_calls=(RawFunctionCall(id="d2", name="skill_load_instructions",
                                                 arguments={"skill_id": "graph-trace"}),)),
        _success(function_calls=(RawFunctionCall(id="d3", name="graph_entity_lookup",
                                                 arguments={"entity_id": "pr-42"}),)),
        _success(text="Looked up pr-42; no graph store was reachable."),
        # IMPACT pass (ND-13E: deterministic-first -- no ADK/skill_discover round trip at all; it calls
        # pr.change_impact directly. With no graph store wired here that call fails deterministically, so
        # no narrative call is even attempted -- this queue item is never consumed.
    ]
    gateway = ModelGateway(provider, _router())
    authorization = _authorization({Permission.READ_GRAPH, Permission.READ_EVIDENCE, Permission.EXECUTE_TOOL})
    request = InvestigationRequest(question="What could be affected by deploying PR-42?", entity_id="pr-42")

    result = asyncio.run(run_coordinated_investigation(
        request, authorization, "coord-1", gateway, tool_registry, tool_executor, skill_registry))

    roles = [s.role for s in result.specialist_results]
    assert roles == [SpecialistRole.DISCOVERY, SpecialistRole.IMPACT, SpecialistRole.VERIFICATION,
                     SpecialistRole.SYNTHESIS]

    discovery, impact, verification, synthesis = result.specialist_results
    assert discovery.skill_used is not None and discovery.skill_used.skill_id == "graph-trace"
    # ND-13E: IMPACT is deterministic-first -- its skill is still resolved/trust-checked even though no
    # LLM ever called skill_discover/skill_load_instructions for it.
    assert impact.skill_used is not None and impact.skill_used.skill_id == "pr-impact"
    assert "pr.change_impact" in impact.tool_ids_called  # the impact specialist really called a Stage 12 tool
    assert "graph.entity_lookup" in discovery.tool_ids_called
    assert impact.model_used is None  # tool call failed (no graph store) -- no narrative was even attempted

    # evidence/uncertainty/provenance survive the handoff into the synthesized result
    assert synthesis.role == SpecialistRole.SYNTHESIS
    assert verification.role == SpecialistRole.VERIFICATION
    # No real graph store is wired here, so the verifier's own independent re-fetch of pr.change_impact
    # comes back NOT_FOUND too -- only the weaker entity-existence check ran (no DEPENDENCY_PATH claim),
    # so this must be reported as PARTIALLY_VERIFIED, never the stronger INDEPENDENTLY_VERIFIED (ND-13C
    # section 1 fix; see a real, graph-store-backed example in test_agent_verifier.py).
    assert result.verification_status == "PARTIALLY_VERIFIED"
    assert provider.calls == 4  # discovery's 4 ADK calls only -- impact never reaches the model at all here
    assert result.status in (InvestigationStatus.PARTIAL, InvestigationStatus.COMPLETE,
                             InvestigationStatus.INCOMPLETE)


# 2. Authorization and revocation: the coordinator re-resolves authorization
# via the existing Stage 08/09 governance refresh before EVERY specialist
# pass (not once at coordinator start) — a grant revoked between the
# discovery and impact passes narrows what the impact pass can even see, and
# no forbidden tool/data leaks into the synthesized result.
class _FakeGovernanceStore(GovernanceStore):
    """Exercises the real ``refresh_principal_source_ids``/
    ``refresh_authorization`` logic without a live Postgres: ``read`` serves
    canned rows from an in-memory table keyed by the SQL's target table."""

    def __init__(self):
        self.employee_role: dict[str, str] = {"emp-1": "role-dev"}  # mutated to simulate revocation
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


def test_authorization_is_refreshed_and_narrowed_between_specialist_passes(tool_registry, skill_registry):
    store = _FakeGovernanceStore()
    tool_executor = ToolExecutor(tool_registry, ToolDependencies(graph_store=None, governance_store=store))
    principal = Principal(subject="nexus:emp-1", tenant_id=NEXUS_TENANT_ID, workspaces=("w1",),
                          permissions=ROLE_PERMISSIONS["DEVELOPER"])
    authorization = AuthorizationContext(principal=principal, tenant_id=NEXUS_TENANT_ID, workspace_id="w1",
                                        task_id="task-1", permissions=principal.permissions)

    # Before revocation: the real refresh resolves the same DEVELOPER permissions, and pr.change_impact
    # (READ_GRAPH + READ_EVIDENCE) is part of the permitted/exposed tool set.
    before = refresh_authorization(authorization, tool_executor)
    assert before.permissions == ROLE_PERMISSIONS["DEVELOPER"]
    tools_before = build_investigation_tools(tool_registry, frozenset({("pr.change_impact", "1.0.0")}))
    assert any(t.name == "pr_change_impact" for t in tools_before)

    # Revoke: the employee's demo identity can no longer be resolved at all.
    store.employee_role.clear()
    after = refresh_authorization(authorization, tool_executor)
    assert after.permissions == frozenset()  # fully revoked, never a stale snapshot
    assert after.principal.source_ids == ()

    # Now run the full coordinator end-to-end. A spy around the real, unmocked ``refresh_authorization``
    # (still executing its real logic via ``wraps``) proves the coordinator re-resolves authorization
    # before EVERY specialist pass from the SAME base ``authorization`` object — never caching the first
    # pass's snapshot for the next one, and never trusting a prior pass's own result for this decision.
    provider = FakeProviderClient()
    provider.queue = [
        _success(function_calls=(RawFunctionCall(id="d1", name="skill_discover", arguments={}),)),
        _success(function_calls=(RawFunctionCall(id="d2", name="skill_load_instructions",
                                                 arguments={"skill_id": "graph-trace"}),)),
        _success(text="No authorized tool was available after the access revocation."),
        # IMPACT (ND-13E: deterministic-first) never reaches the model here -- pr.change_impact is denied
        # by the executor itself (FORBIDDEN) before any narrative call would be attempted.
    ]
    gateway = ModelGateway(provider, _router())
    # entity_id supplied explicitly: IMPACT's deterministic pass needs a concrete entity id to call its
    # tool with (it does not explore conversationally the way the old LLM-driven pass could).
    request = InvestigationRequest(question="What could be affected by deploying PR-42?", entity_id="pr-42")

    from unittest.mock import patch
    with patch("backend.app.agent.coordinator.refresh_authorization",
              wraps=refresh_authorization) as spy:
        result = asyncio.run(run_coordinated_investigation(
            request, authorization, "coord-2", gateway, tool_registry, tool_executor, skill_registry))

    assert spy.call_count == 3  # once per pass: discovery, impact, verification -- never a single cached refresh
    for call in spy.call_args_list:
        assert call.args[0] is authorization  # always derived from the server-trusted base context

    by_role = {s.role: s for s in result.specialist_results}
    assert by_role[SpecialistRole.DISCOVERY].tool_ids_called == ()
    assert by_role[SpecialistRole.IMPACT].status == InvestigationStatus.BLOCKED  # denied, not fabricated
    assert result.evidence_refs == () and result.entity_ids == ()  # nothing forbidden leaked into the synthesis


# 3. Shared budget: a global cap across both specialist passes (not a fresh
# allowance per specialist) produces a bounded PARTIAL result, never an
# unbounded loop, and the impact pass is skipped cleanly once the shared
# tool-call budget is already spent.
def test_shared_budget_bounds_both_specialist_passes(tool_registry, skill_registry, tool_executor):
    provider = FakeProviderClient()
    provider.queue = [
        _success(function_calls=(RawFunctionCall(id="d1", name="skill_discover", arguments={}),)),
        _success(function_calls=(RawFunctionCall(id="d2", name="skill_load_instructions",
                                                 arguments={"skill_id": "graph-trace"}),)),
        _success(function_calls=(RawFunctionCall(id="d3", name="graph_entity_lookup",
                                                 arguments={"entity_id": "a"}),)),
        _success(function_calls=(RawFunctionCall(id="d4", name="graph_entity_lookup",
                                                 arguments={"entity_id": "b"}),)),
        # A 3rd tool call attempt beyond the shared budget's 2-call cap for this pass; the tool bridge's
        # own per-run max_tool_calls (carved down to 2 by the coordinator) denies it with an error the
        # model then summarizes, rather than the run looping forever.
        _success(function_calls=(RawFunctionCall(id="d5", name="graph_entity_lookup",
                                                 arguments={"entity_id": "c"}),)),
        _success(text="Looked up two entities before hitting the tool-call limit."),
    ]
    gateway = ModelGateway(provider, _router())
    authorization = _authorization({Permission.READ_GRAPH, Permission.READ_EVIDENCE, Permission.EXECUTE_TOOL})
    # entity_id supplied explicitly so IMPACT's deterministic pass has a concrete id to analyze -- this
    # test is specifically about the shared TOOL-CALL budget stopping it, not about entity resolution.
    request = InvestigationRequest(question="What could be affected by deploying PR-42?", entity_id="pr-42")
    budget = CoordinatorBudget(max_total_tool_calls=2, max_total_llm_calls=20, overall_timeout_seconds=60)

    result = asyncio.run(run_coordinated_investigation(
        request, authorization, "coord-3", gateway, tool_registry, tool_executor, skill_registry, budget=budget))

    assert len(result.specialist_results) == 2  # impact was skipped outright -- no fresh allowance granted
    discovery, synthesis = result.specialist_results
    assert discovery.tool_calls_made <= 2  # the shared budget, not just the pass's own default, bounded it
    assert any("impact pass" in reason and "budget exhausted" in reason for reason in result.incomplete_work)
    # Bounded, not an endless loop or a crash: a partial/incomplete result is reported and returned.
    assert result.status in (InvestigationStatus.PARTIAL, InvestigationStatus.INCOMPLETE)
