"""GH-007A: pure unit tests (no I/O) for the agent's typed contracts —
mirrors the style of ``test_foundation_contracts.py``."""
from __future__ import annotations

import pytest

from backend.app.agent.contracts import (
    AgentExecutionContext, ApprovedSkill, InvestigationRequest, InvestigationResult, InvestigationStatus,
    ModelUsageSummary, RuntimeLimits,
)
from backend.app.gateway.contracts import ModelRole
from backend.app.security import AuthorizationContext, Permission, Principal


def _auth() -> AuthorizationContext:
    principal = Principal(subject="agent", tenant_id="t1", workspaces=("w1",),
                          permissions=frozenset({Permission.READ_GRAPH, Permission.EXECUTE_TOOL}))
    return AuthorizationContext(principal=principal, tenant_id="t1", workspace_id="w1", task_id="task-1",
                               permissions=principal.permissions)


def test_agent_execution_context_requires_at_least_one_approved_skill():
    with pytest.raises(Exception):
        AgentExecutionContext(authorization=_auth(), run_id="r1", approved_skills=())


def test_agent_execution_context_rejects_duplicate_skill_ids():
    with pytest.raises(Exception):
        AgentExecutionContext(
            authorization=_auth(), run_id="r1",
            approved_skills=(ApprovedSkill(skill_id="verify-evidence", version="1.0.0"),
                             ApprovedSkill(skill_id="verify-evidence", version="2.0.0")))


def test_agent_execution_context_defaults_are_small_and_explicit():
    context = AgentExecutionContext(
        authorization=_auth(), run_id="r1",
        approved_skills=(ApprovedSkill(skill_id="verify-evidence", version="1.0.0"),))
    assert context.limits.max_llm_calls == 6  # a small default, not ADK's unrestricted 500
    assert context.model_role == ModelRole.DEFAULT


def test_runtime_limits_are_bounded():
    with pytest.raises(Exception):
        RuntimeLimits(max_llm_calls=0)
    with pytest.raises(Exception):
        RuntimeLimits(max_llm_calls=1000)  # exceeds the le=50 cap


def test_investigation_request_requires_a_nonempty_question():
    with pytest.raises(Exception):
        InvestigationRequest(question="")


def test_investigation_result_status_values():
    result = InvestigationResult(run_id="r1", status=InvestigationStatus.PARTIAL, summary="ok",
                                 model_usage=ModelUsageSummary())
    assert result.status is InvestigationStatus.PARTIAL
    assert result.skill_used is None
    assert result.tool_calls == ()


def test_agent_execution_context_is_frozen():
    context = AgentExecutionContext(
        authorization=_auth(), run_id="r1",
        approved_skills=(ApprovedSkill(skill_id="verify-evidence", version="1.0.0"),))
    with pytest.raises(Exception):
        context.run_id = "other"
