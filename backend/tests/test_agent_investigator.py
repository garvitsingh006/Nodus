"""GH-007A: end-to-end offline coverage of the general investigator agent —
a real Google ADK ``LlmAgent``/``Runner`` pair, a real ``ModelGateway`` (via
the existing ``FakeProviderClient`` scripted-model pattern from
``test_model_gateway.py``), the real ``SkillRegistry``/``ToolRegistry``/
``ToolExecutor``, driven through ``backend.app.agent.investigator.run_investigation``.

No Neo4j here: tool handlers that need a graph store fail with
EXECUTION_FAILED when ``ToolDependencies(graph_store=None)`` — which is
itself a legitimate, deterministically-handled outcome these tests rely on.
The grounded, real-graph investigation workflows (entity lookup, evidence
verification, conflicting evidence, cross-tenant isolation) are covered
against an isolated Neo4j instance in
``backend/tests/test_agent_investigation_integration.py``.
"""
from __future__ import annotations

import asyncio

import pytest

from backend.app.agent.contracts import (
    AgentExecutionContext, ApprovedSkill, InvestigationRequest, InvestigationStatus, RuntimeLimits,
)
from backend.app.agent.investigator import run_investigation
from backend.app.gateway.budget import SharedTokenBudgetTracker
from backend.app.gateway.contracts import ModelRole
from backend.app.gateway.gateway import ModelGateway
from backend.app.gateway.provider import ProviderResponse, RawFunctionCall, RawUsage
from backend.app.gateway.provider import RateLimitedError, AuthenticationFailedError
from backend.app.gateway.routing import RoleRouter
from backend.app.security import AuthorizationContext, Permission, Principal
from backend.app.skills.catalog import build_production_skill_registry
from backend.app.tools import register_all_tools
from backend.app.tools.executor import ToolExecutor
from backend.app.tools.registry import ToolDependencies, ToolRegistry


class FakeProviderClient:
    def __init__(self, input_tokens: int = 10):
        self.input_tokens = input_tokens
        self.calls = 0
        self.queue: list = []

    def count_tokens(self, model, contents) -> int:
        return self.input_tokens

    def generate(self, model, contents, config) -> ProviderResponse:
        self.calls += 1
        item = self.queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def available_models(self):
        return frozenset({"fake-lite", "fake-default"})


def _success(text=None, function_calls=(), input_tokens=10, output_tokens=5) -> ProviderResponse:
    return ProviderResponse(
        text=text, parsed=None,
        usage=RawUsage(input_tokens=input_tokens, output_tokens=output_tokens, thinking_tokens=0,
                       total_tokens=input_tokens + output_tokens),
        model_version="fake-default-001", finish_reason="STOP", safety_blocked=False,
        function_calls=function_calls,
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


def _exec_context(*, limits=None, permissions=None, approved=(("verify-evidence", "1.0.0"),)) -> AgentExecutionContext:
    permissions = permissions or frozenset({Permission.READ_GRAPH, Permission.READ_EVIDENCE,
                                            Permission.EXECUTE_TOOL})
    principal = Principal(subject="agent", tenant_id="t1", workspaces=("w1",), permissions=permissions)
    auth = AuthorizationContext(principal=principal, tenant_id="t1", workspace_id="w1", task_id="task-1",
                                permissions=permissions)
    return AgentExecutionContext(
        authorization=auth, run_id="run-" + str(id(auth)), model_role=ModelRole.DEFAULT,
        approved_skills=tuple(ApprovedSkill(skill_id=s, version=v) for s, v in approved),
        limits=limits or RuntimeLimits(max_llm_calls=6, max_tool_calls=6, overall_timeout_seconds=30),
    )


# 1. ADK imports and real agent construction succeed; the Runner executes a full turn.
def test_runner_executes_a_scripted_agent_turn(tool_registry, skill_registry, tool_executor):
    provider = FakeProviderClient()
    provider.queue = [_success(text="There is nothing to investigate yet.")]
    gateway = ModelGateway(provider, _router())
    exec_context = _exec_context()
    request = InvestigationRequest(question="What is NODUS?")

    result = asyncio.run(run_investigation(request, exec_context, gateway, tool_registry, tool_executor,
                                           skill_registry))

    assert result.run_id == exec_context.run_id
    assert result.summary == "There is nothing to investigate yet."
    assert provider.calls == 1
    assert result.model_usage.llm_calls == 1


# 2. Full function-call / tool-response round trip: discover -> load -> call a tool -> final answer.
def test_full_skill_and_tool_round_trip(tool_registry, skill_registry, tool_executor):
    provider = FakeProviderClient()
    provider.queue = [
        _success(function_calls=(RawFunctionCall(id="1", name="skill_discover", arguments={}),)),
        _success(function_calls=(RawFunctionCall(id="2", name="skill_load_instructions",
                                                 arguments={"skill_id": "verify-evidence"}),)),
        _success(function_calls=(RawFunctionCall(id="3", name="graph_revision", arguments={}),)),
        _success(text="I checked the graph revision but could not reach the graph store."),
    ]
    gateway = ModelGateway(provider, _router())
    exec_context = _exec_context()
    request = InvestigationRequest(question="Is Alice's record supported by evidence?", entity_id="alice")

    result = asyncio.run(run_investigation(request, exec_context, gateway, tool_registry, tool_executor,
                                           skill_registry))

    assert result.skill_used is not None
    assert result.skill_used.skill_id == "verify-evidence"
    assert result.skill_used.trust_status == "TRUSTED"
    assert len(result.tool_calls) == 1
    assert result.tool_calls[0].tool_id == "graph.revision"
    assert provider.calls == 4
    # alice was never actually looked up via a tool -> flagged, not silently trusted.
    assert any("alice" in claim for claim in result.rejected_unverified_claims)
    assert result.status == InvestigationStatus.PARTIAL


# 3. Per-run LLM-call limit (RunConfig.max_llm_calls) is enforced, not ADK's unrestricted default.
def test_per_run_llm_call_limit_is_enforced(tool_registry, skill_registry, tool_executor):
    provider = FakeProviderClient()
    # The model keeps calling skill_discover forever; max_llm_calls must cut it off.
    provider.queue = [_success(function_calls=(RawFunctionCall(id=str(i), name="skill_discover", arguments={}),))
                      for i in range(20)]
    gateway = ModelGateway(provider, _router())
    exec_context = _exec_context(limits=RuntimeLimits(max_llm_calls=3, max_tool_calls=20,
                                                      overall_timeout_seconds=30))
    request = InvestigationRequest(question="loop forever")

    result = asyncio.run(run_investigation(request, exec_context, gateway, tool_registry, tool_executor,
                                           skill_registry))

    assert provider.calls <= 3
    assert result.status == InvestigationStatus.INCOMPLETE


# 4. Budget exhaustion stops additional model calls.
def test_budget_exhaustion_stops_additional_model_calls(tool_registry, skill_registry, tool_executor):
    provider = FakeProviderClient(input_tokens=50)
    provider.queue = [_success(function_calls=(RawFunctionCall(id="1", name="skill_discover", arguments={}),))
                      for _ in range(5)]
    tracker = SharedTokenBudgetTracker()
    # One call's estimated_total is input_tokens(50) + default max_output_tokens_per_call(1024) = 1074;
    # it reconciles down to the response's actual usage (10 + 5 = 15), so a 1080 limit lets the first
    # reservation through (1074 <= 1080) but leaves only 1065 remaining — not enough for a second 1074.
    tracker.configure("tenant-1", limit=1080)
    gateway = ModelGateway(provider, _router(), budget_tracker=tracker)
    exec_context = _exec_context(limits=RuntimeLimits(max_llm_calls=10, max_tool_calls=10,
                                                      overall_timeout_seconds=30, request_budget_key="tenant-1"))
    request = InvestigationRequest(question="whatever")

    result = asyncio.run(run_investigation(request, exec_context, gateway, tool_registry, tool_executor,
                                           skill_registry))

    assert provider.calls == 1  # the second preflight exceeds the shared budget and is rejected
    assert result.error_message is not None and "BUDGET_EXHAUSTED" in result.error_message
    assert result.status in (InvestigationStatus.PARTIAL, InvestigationStatus.INCOMPLETE,
                             InvestigationStatus.BLOCKED)


# 5. Provider failures (e.g. authentication) are handled, not raised out of run_investigation.
def test_provider_error_produces_a_structured_result_not_an_exception(tool_registry, skill_registry, tool_executor):
    provider = FakeProviderClient()
    provider.queue = [AuthenticationFailedError("bad key")]
    gateway = ModelGateway(provider, _router())
    exec_context = _exec_context()
    request = InvestigationRequest(question="whatever")

    result = asyncio.run(run_investigation(request, exec_context, gateway, tool_registry, tool_executor,
                                           skill_registry))

    assert result.status in (InvestigationStatus.PARTIAL, InvestigationStatus.BLOCKED,
                             InvestigationStatus.INCOMPLETE)
    assert "AUTHENTICATION_FAILED" in result.error_message
    assert provider.calls == 1  # no retry for a non-retryable category


# 6. Retryable provider errors (rate limit) are retried within the gateway's own bounded policy.
def test_retryable_provider_error_is_retried_then_succeeds(tool_registry, skill_registry, tool_executor):
    from backend.app.gateway.retry import RetryPolicy

    provider = FakeProviderClient()
    provider.queue = [RateLimitedError("slow down"), _success(text="done after retry")]
    gateway = ModelGateway(provider, _router(), retry_policy=RetryPolicy(max_retries=2, base_delay_seconds=0.01))
    exec_context = _exec_context()
    request = InvestigationRequest(question="whatever")

    result = asyncio.run(run_investigation(request, exec_context, gateway, tool_registry, tool_executor,
                                           skill_registry))

    assert result.summary == "done after retry"
    assert provider.calls == 2


# 7. Missing/unapproved skill: the agent cannot select one, and the run is reported INCOMPLETE.
def test_missing_approved_skill_leads_to_incomplete_result(tool_registry, skill_registry, tool_executor):
    provider = FakeProviderClient()
    provider.queue = [
        _success(function_calls=(RawFunctionCall(id="1", name="skill_load_instructions",
                                                 arguments={"skill_id": "nonexistent-skill"}),)),
        _success(text="I could not find an applicable skill."),
    ]
    gateway = ModelGateway(provider, _router())
    exec_context = _exec_context()
    request = InvestigationRequest(question="whatever")

    result = asyncio.run(run_investigation(request, exec_context, gateway, tool_registry, tool_executor,
                                           skill_registry))

    assert result.skill_used is None
    assert result.status == InvestigationStatus.INCOMPLETE


# 8. Concurrent runs never leak context (different tenants, different scripted conversations).
def test_concurrent_runs_do_not_leak_context(tool_registry, skill_registry, tool_executor):
    provider_a = FakeProviderClient()
    provider_a.queue = [_success(text="Answer for tenant A")]
    provider_b = FakeProviderClient()
    provider_b.queue = [_success(text="Answer for tenant B")]
    gateway_a = ModelGateway(provider_a, _router())
    gateway_b = ModelGateway(provider_b, _router())

    principal_a = Principal(subject="agent-a", tenant_id="tenant-a", workspaces=("w-a",),
                            permissions=frozenset({Permission.READ_GRAPH, Permission.EXECUTE_TOOL}))
    auth_a = AuthorizationContext(principal=principal_a, tenant_id="tenant-a", workspace_id="w-a",
                                  task_id="t-a", permissions=principal_a.permissions)
    exec_a = AgentExecutionContext(authorization=auth_a, run_id="run-a", model_role=ModelRole.DEFAULT,
                                   approved_skills=(ApprovedSkill(skill_id="graph-trace", version="1.0.0"),),
                                   limits=RuntimeLimits(max_llm_calls=4, overall_timeout_seconds=30))

    principal_b = Principal(subject="agent-b", tenant_id="tenant-b", workspaces=("w-b",),
                            permissions=frozenset({Permission.READ_GRAPH, Permission.READ_EVIDENCE,
                                                   Permission.EXECUTE_TOOL}))
    auth_b = AuthorizationContext(principal=principal_b, tenant_id="tenant-b", workspace_id="w-b",
                                  task_id="t-b", permissions=principal_b.permissions)
    exec_b = AgentExecutionContext(authorization=auth_b, run_id="run-b", model_role=ModelRole.DEFAULT,
                                   approved_skills=(ApprovedSkill(skill_id="verify-evidence", version="1.0.0"),),
                                   limits=RuntimeLimits(max_llm_calls=4, overall_timeout_seconds=30))

    async def _main():
        return await asyncio.gather(
            run_investigation(InvestigationRequest(question="q-a"), exec_a, gateway_a, tool_registry,
                              tool_executor, skill_registry),
            run_investigation(InvestigationRequest(question="q-b"), exec_b, gateway_b, tool_registry,
                              tool_executor, skill_registry),
        )

    result_a, result_b = asyncio.run(_main())

    assert result_a.run_id == "run-a" and result_a.summary == "Answer for tenant A"
    assert result_b.run_id == "run-b" and result_b.summary == "Answer for tenant B"
    # Each run's gateway/provider instance is untouched by the other run.
    assert provider_a.calls == 1 and provider_b.calls == 1


# 9. Existing tools/skills/gateway modules remain importable and usable independent of the agent package.
def test_existing_subsystems_do_not_depend_on_the_agent_package():
    import ast
    from pathlib import Path

    app_dir = Path(__file__).resolve().parents[2] / "backend" / "app"
    for sub in ("tools", "skills", "gateway"):
        for path in (app_dir / sub).glob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module:
                    assert "backend.app.agent" not in node.module, f"{path} must not depend on backend.app.agent"
