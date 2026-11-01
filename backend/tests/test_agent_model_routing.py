"""ND-13C/13D/13E: verification-status correctness, per-specialist model
routing, token-usage rollup, and IMPACT completion-status honesty.

Offline/scripted only (no live Gemini credentials used or claimed here) —
consistent with ``test_agent_coordinator.py``'s harness. End-to-end budget/
authorization enforcement across passes is already covered by the existing
ND-13A/13B test suites and is not duplicated here.
"""
from __future__ import annotations

import asyncio
import dataclasses

from backend.app.agent.contracts import (
    ClaimType, ClaimVerificationResult, InvestigationRequest, InvestigationStatus, SpecialistRole,
    VerificationStatus,
)
from backend.app.agent.coordinator import run_coordinated_investigation, verification_status_for
from backend.app.gateway.contracts import ModelRole
from backend.app.gateway.gateway import ModelGateway
from backend.app.gateway.provider import ProviderResponse, RawFunctionCall, RawUsage
from backend.app.gateway.routing import RoleRouter
from backend.app.security import AuthorizationContext, Permission, Principal
from backend.app.skills.catalog import build_production_skill_registry
from backend.app.tools import register_all_tools
from backend.app.tools.contracts import ToolResult, ToolStatus
from backend.app.tools.executor import ToolExecutor
from backend.app.tools.registry import ToolDependencies, ToolRegistry


def _claim(status: VerificationStatus, claim_type: ClaimType = ClaimType.ENTITY_ASSERTION) -> ClaimVerificationResult:
    return ClaimVerificationResult(claim_id="c", claim_type=claim_type, claim_summary="s", status=status)


# 1. verification_status must reflect how complete and how strong the check actually was, not merely
# whether the verification pass ran (ND-13C section 1 fix).
def test_verification_status_reflects_completeness_and_strength():
    assert verification_status_for(()) == "PENDING_INDEPENDENT_VERIFICATION"

    # Only the weaker entity-existence check ran (no relationship-level claim) -- never the stronger value,
    # even though the one claim produced was itself fully resolved (SUPPORTED).
    assert verification_status_for((_claim(VerificationStatus.SUPPORTED),)) == "PARTIALLY_VERIFIED"

    # A NOT_CHECKED (budget/access) or STALE claim anywhere means the check did not reach a complete,
    # current conclusion -- even alongside an otherwise-resolved relationship-level claim.
    assert verification_status_for((
        _claim(VerificationStatus.SUPPORTED, ClaimType.DEPENDENCY_PATH),
        _claim(VerificationStatus.NOT_CHECKED),
    )) == "PARTIALLY_VERIFIED"
    assert verification_status_for((
        _claim(VerificationStatus.SUPPORTED, ClaimType.DEPENDENCY_PATH),
        _claim(VerificationStatus.STALE),
    )) == "PARTIALLY_VERIFIED"

    # An UNSUPPORTED or CONTRADICTED verdict does NOT by itself weaken the status -- independently
    # catching a false/conflicting claim is a successful, complete verification.
    assert verification_status_for((
        _claim(VerificationStatus.UNSUPPORTED, ClaimType.DEPENDENCY_PATH),
        _claim(VerificationStatus.SUPPORTED),
    )) == "INDEPENDENTLY_VERIFIED"
    assert verification_status_for((
        _claim(VerificationStatus.CONTRADICTED, ClaimType.DEPENDENCY_PATH),
    )) == "INDEPENDENTLY_VERIFIED"


class FakeProviderClient:
    def __init__(self):
        self.calls = 0
        self.queue: list = []

    def count_tokens(self, model, contents) -> int:
        return 10

    def generate(self, model, contents, config) -> ProviderResponse:
        self.calls += 1
        # Echo back the actually-requested model id, exactly like a real provider would, so the test can
        # verify which resolved model a pass actually used -- not just that *a* call succeeded.
        return dataclasses.replace(self.queue.pop(0), model_version=model)

    def available_models(self):
        return frozenset({"fake-lite", "fake-default"})


def _success(text=None, function_calls=()) -> ProviderResponse:
    return ProviderResponse(
        text=text, parsed=None,
        usage=RawUsage(input_tokens=10, output_tokens=5, thinking_tokens=0, total_tokens=15),
        model_version="fake-default-001", finish_reason="STOP", safety_blocked=False, function_calls=function_calls,
    )


def _authorization() -> AuthorizationContext:
    permissions = frozenset({Permission.READ_GRAPH, Permission.READ_EVIDENCE, Permission.EXECUTE_TOOL})
    principal = Principal(subject="agent", tenant_id="t1", workspaces=("w1",), permissions=permissions)
    return AuthorizationContext(principal=principal, tenant_id="t1", workspace_id="w1", task_id="task-1",
                                permissions=permissions)


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


class _FakeDeterministicToolExecutor:
    """Stands in for the real ``ToolExecutor`` so IMPACT's deterministic Stage-12 call (ND-13E) succeeds
    without a real graph store -- exercising the narrative/model-routing path, which only runs on tool
    SUCCESS. VERIFICATION's own calls (``pr.change_impact`` re-fetch + ``graph.find_paths``) are answered
    too, so it can run without erroring on an unrelated fake-shape mismatch."""

    def __init__(self):
        self.dependencies = ToolDependencies()
        self.invocations: list = []

    def execute(self, invocation) -> ToolResult:
        self.invocations.append(invocation)
        if invocation.tool_id == "pr.change_impact":
            return ToolResult(tool_id=invocation.tool_id, version=invocation.version, status=ToolStatus.SUCCESS,
                              correlation_id="x", data=_PR_DATA, evidence_refs=("src:1",), graph_revision=5)
        if invocation.tool_id == "graph.find_paths":
            return ToolResult(tool_id=invocation.tool_id, version=invocation.version, status=ToolStatus.SUCCESS,
                              correlation_id="x", data={"paths": [], "coverage": "source_backed_only"},
                              graph_revision=5)
        return ToolResult(tool_id=invocation.tool_id, version=invocation.version, status=ToolStatus.NOT_FOUND,
                          correlation_id="x")


# 2. Per-specialist model routing is configurable, the actual resolved model is recorded per pass, an
# unconfigured/unallowlisted role fails cleanly rather than silently falling back to a different model, and
# token usage rolls up across specialists without double-counting (ND-13D section 3/6; ND-13E's
# deterministic-first IMPACT still makes exactly one, bounded, tool-free model call for its narrative).
def test_specialist_model_routing_is_configurable_and_fails_clearly():
    tool_registry = ToolRegistry()
    register_all_tools(tool_registry)
    skill_registry = build_production_skill_registry(tool_registry=tool_registry)
    router = RoleRouter(role_models={ModelRole.LIGHTWEIGHT: "fake-lite", ModelRole.DEFAULT: "fake-default"})
    request = InvestigationRequest(question="What could be affected by deploying this PR?", entity_id="pr-1")

    # 2a. Default routing: both DISCOVERY and IMPACT default to LIGHTWEIGHT ("fake-lite"). DISCOVERY makes
    # 3 ADK calls (15 tokens each); IMPACT's one tool call succeeds deterministically (no model call for
    # that), then makes exactly ONE narrative call (also 15 tokens) -- 4 model calls total, not the 6+ the
    # old ADK-driven IMPACT needed. The coordinator's rolled-up ``model_usage`` must equal the exact sum.
    provider = FakeProviderClient()
    provider.queue = [
        _success(function_calls=(RawFunctionCall(id="d1", name="skill_discover", arguments={}),)),
        _success(function_calls=(RawFunctionCall(id="d2", name="skill_load_instructions",
                                                 arguments={"skill_id": "graph-trace"}),)),
        _success(text="No graph store was reachable."),
        _success(text="svc-a may potentially affect svc-b; not a confirmed failure."),
    ]
    gateway = ModelGateway(provider, router)

    result = asyncio.run(run_coordinated_investigation(
        request, _authorization(), "coord-routing", gateway, tool_registry, _FakeDeterministicToolExecutor(),
        skill_registry))

    by_role = {s.role: s for s in result.specialist_results}
    assert by_role[SpecialistRole.DISCOVERY].model_used == "fake-lite"
    assert by_role[SpecialistRole.IMPACT].model_used == "fake-lite"
    assert by_role[SpecialistRole.IMPACT].summary == "svc-a may potentially affect svc-b; not a confirmed failure."
    assert "pr.change_impact" in by_role[SpecialistRole.IMPACT].tool_ids_called
    assert set(result.models_used) == {"fake-lite"}

    assert by_role[SpecialistRole.DISCOVERY].total_tokens == 3 * 15
    assert by_role[SpecialistRole.IMPACT].total_tokens == 1 * 15  # just the narrative call, not the tool call
    assert result.model_usage.total_input_tokens == 4 * 10
    assert result.model_usage.total_output_tokens == 4 * 5
    assert result.model_usage.total_tokens == 4 * 15  # exact sum, not double-counted or re-derived

    # 2b. An explicit per-specialist override still works (configurability, not just a new hardcoded
    # default): IMPACT's narrative call can be routed to DEFAULT ("fake-default") while DISCOVERY stays
    # LIGHTWEIGHT.
    provider_override = FakeProviderClient()
    provider_override.queue = [
        _success(function_calls=(RawFunctionCall(id="d1", name="skill_discover", arguments={}),)),
        _success(function_calls=(RawFunctionCall(id="d2", name="skill_load_instructions",
                                                 arguments={"skill_id": "graph-trace"}),)),
        _success(text="No graph store was reachable."),
        _success(text="svc-a may potentially affect svc-b; not a confirmed failure."),
    ]
    gateway_override = ModelGateway(provider_override, router)
    result_override = asyncio.run(run_coordinated_investigation(
        request, _authorization(), "coord-routing-override", gateway_override, tool_registry,
        _FakeDeterministicToolExecutor(), skill_registry,
        specialist_model_roles={SpecialistRole.IMPACT: ModelRole.DEFAULT}))
    by_role_override = {s.role: s for s in result_override.specialist_results}
    assert by_role_override[SpecialistRole.DISCOVERY].model_used == "fake-lite"
    assert by_role_override[SpecialistRole.IMPACT].model_used == "fake-default"

    # 2c. An unconfigured role (ESCALATION was never given a model in this router) must fail the affected
    # pass cleanly -- BLOCKED with a clear error, never a silent fallback to a different model. IMPACT does
    # not depend on DISCOVERY's success (it only needs the request's own entity_id), so it still completes
    # here via its one deterministic tool call plus one (still LIGHTWEIGHT-routed) narrative call.
    provider2 = FakeProviderClient()
    provider2.queue = [
        _success(text="svc-a may potentially affect svc-b; not a confirmed failure."),
    ]
    gateway2 = ModelGateway(provider2, router)

    result2 = asyncio.run(run_coordinated_investigation(
        request, _authorization(), "coord-routing-2", gateway2, tool_registry, _FakeDeterministicToolExecutor(),
        skill_registry, specialist_model_roles={SpecialistRole.DISCOVERY: ModelRole.ESCALATION}))

    by_role2 = {s.role: s for s in result2.specialist_results}
    assert by_role2[SpecialistRole.DISCOVERY].status == InvestigationStatus.BLOCKED
    assert by_role2[SpecialistRole.IMPACT].status == InvestigationStatus.COMPLETE
    assert provider2.calls == 1  # only IMPACT's one narrative call; DISCOVERY never reaches the model


# 3. ND-13E section 2's actual fix: once IMPACT's deterministic Stage 12 tool call has succeeded, it must
# complete WITHOUT waiting on -- or needing -- any additional model-generated text. Zero remaining model
# turns (all spent by DISCOVERY) must not stop it, and the summary must be honestly marked as not
# model-generated (no narrative was even attempted, so none is fabricated as a successful one).
def test_impact_completes_from_deterministic_tool_result_alone():
    tool_registry = ToolRegistry()
    register_all_tools(tool_registry)
    skill_registry = build_production_skill_registry(tool_registry=tool_registry)
    router = RoleRouter(role_models={ModelRole.LIGHTWEIGHT: "fake-lite", ModelRole.DEFAULT: "fake-default"})

    provider = FakeProviderClient()
    provider.queue = [
        _success(function_calls=(RawFunctionCall(id="d1", name="skill_discover", arguments={}),)),
        _success(function_calls=(RawFunctionCall(id="d2", name="skill_load_instructions",
                                                 arguments={"skill_id": "graph-trace"}),)),
        _success(text="No graph store was reachable."),
        # Nothing queued for IMPACT: the shared budget leaves it zero LLM calls, so it must never even
        # attempt a narrative call, let alone wait on one.
    ]
    gateway = ModelGateway(provider, router)
    request = InvestigationRequest(question="What could be affected by deploying this PR?", entity_id="pr-1")

    from backend.app.agent.contracts import CoordinatorBudget
    budget = CoordinatorBudget(max_total_llm_calls=3, max_total_tool_calls=16, overall_timeout_seconds=60)

    result = asyncio.run(run_coordinated_investigation(
        request, _authorization(), "coord-impact-deterministic", gateway, tool_registry,
        _FakeDeterministicToolExecutor(), skill_registry, budget=budget))

    by_role = {s.role: s for s in result.specialist_results}
    impact = by_role[SpecialistRole.IMPACT]
    assert impact.status == InvestigationStatus.COMPLETE
    assert impact.model_used is None  # no narrative call was made -- never mistaken for one that succeeded
    assert "svc-a" in impact.entity_ids and "svc-b" in impact.entity_ids
    assert impact.evidence_refs == ("src:1",)
    assert provider.calls == 3  # discovery's 3 calls only; IMPACT never touched the model at all


# 4. A failed/unresolvable required tool result must still produce an honest INCOMPLETE/BLOCKED IMPACT
# status, and VERIFICATION subsequently running must never promote the OVERALL result to COMPLETE on
# IMPACT's behalf ("a completed verifier with an incomplete IMPACT pass does not count as success";
# ND-13D section 1/5, ND-13E section 2).
def test_impact_tool_failure_never_yields_a_false_overall_complete():
    tool_registry = ToolRegistry()
    register_all_tools(tool_registry)
    skill_registry = build_production_skill_registry(tool_registry=tool_registry)
    tool_executor = ToolExecutor(tool_registry, ToolDependencies(graph_store=None))  # no graph store -> fails
    router = RoleRouter(role_models={ModelRole.LIGHTWEIGHT: "fake-lite", ModelRole.DEFAULT: "fake-default"})

    provider = FakeProviderClient()
    provider.queue = [
        # DISCOVERY completes normally; IMPACT's deterministic tool call fails before any model call, so
        # nothing is queued for it.
        _success(function_calls=(RawFunctionCall(id="d1", name="skill_discover", arguments={}),)),
        _success(function_calls=(RawFunctionCall(id="d2", name="skill_load_instructions",
                                                 arguments={"skill_id": "graph-trace"}),)),
        _success(text="No graph store was reachable."),
    ]
    gateway = ModelGateway(provider, router)
    request = InvestigationRequest(question="What could be affected by deploying this PR?", entity_id="pr-1")

    result = asyncio.run(run_coordinated_investigation(
        request, _authorization(), "coord-impact-incomplete", gateway, tool_registry, tool_executor,
        skill_registry))

    by_role = {s.role: s for s in result.specialist_results}
    assert by_role[SpecialistRole.IMPACT].status == InvestigationStatus.INCOMPLETE
    assert by_role[SpecialistRole.IMPACT].model_used is None  # never fabricated as a model result
    # VERIFICATION may still run (it independently re-checks via the tool gateway), but that must never
    # promote the OVERALL result to COMPLETE on IMPACT's behalf.
    assert result.status != InvestigationStatus.COMPLETE
