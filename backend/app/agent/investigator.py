"""The single general investigator agent: builds one ADK ``LlmAgent``/
``Runner`` pair per run and drives it through ADK's own execution lifecycle
(no separate hand-rolled agent loop).

``run_investigation`` is the one public entry point. It:

1. Builds a per-run ``RunState`` bound to a ``contextvars.ContextVar`` (see
   ``backend/app/agent/run_state.py``) so concurrent runs never share
   mutable state.
2. Exposes exactly the tools the run's approved skills declare *and* the
   calling principal actually has permission for (never all registered
   tools — see ``backend/app/agent/tool_bridge.py``).
3. Runs the ADK ``Runner`` with a small, explicit ``RunConfig.max_llm_calls``
   bound (never ADK's unrestricted default) and an overall wall-clock
   timeout.
4. Builds the returned ``InvestigationResult`` from what tools actually
   returned during the run — never from parsing the model's free-text
   answer as if it were proof.
"""
from __future__ import annotations

import asyncio

from google.adk.agents import LlmAgent
from google.adk.agents.invocation_context import LlmCallsLimitExceededError
from google.adk.agents.run_config import RunConfig
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

from backend.app.agent.contracts import (
    AgentExecutionContext, InvestigationRequest, InvestigationResult, InvestigationStatus, ModelUsageSummary,
    SkillUsageRecord,
)
from backend.app.agent.gateway_llm import GatewayLlm
from backend.app.agent.run_state import CURRENT_RUN, RunState
from backend.app.agent.tool_bridge import build_investigation_tools
from backend.app.gateway.contracts import ModelErrorCategory
from backend.app.gateway.gateway import ModelGateway
from backend.app.skills.registry import SkillRegistry
from backend.app.tools.executor import ToolExecutor
from backend.app.tools.registry import ToolRegistry

_INSTRUCTION = """You are the NODUS general investigator agent. You answer scoped questions \
about company data using only authorized, deterministic tools over an evidence-backed graph.

Procedure (always, in order):
1. Call skill_discover to see which trusted skills are available for this investigation.
2. Call skill_load_instructions for exactly the one skill that best matches the question. \
You may load only one skill per investigation — choose carefully.
3. Follow that skill's instructions exactly, calling only the tools it names as compatible.
4. Base every claim only on what the tools actually returned. If a tool returns NOT_FOUND, \
INACCESSIBLE, INCOMPLETE or an empty result, say so plainly — never guess or invent a fact.
5. Never claim to have verified something a tool did not confirm. Never resolve a conflict \
yourself — report CONFLICTING/unresolved/no_rule exactly as the tool returned it.
6. Never claim broader access than the tools actually granted you.
7. When you are done, or when you cannot make further progress, give a concise final answer \
in plain text summarizing what you found, citing entity/relationship ids and evidence \
references you actually saw in tool results, and naming any unresolved conflicts or \
source-coverage limitations explicitly. Do not call any more tools after giving this answer."""

_APP_NAME = "nodus-investigator"

_RETRYABLE_CATEGORIES = frozenset({
    ModelErrorCategory.PROVIDER_UNAVAILABLE.value, ModelErrorCategory.RATE_LIMITED.value,
    ModelErrorCategory.TIMEOUT.value, ModelErrorCategory.BUDGET_EXHAUSTED.value,
    ModelErrorCategory.QUOTA_EXHAUSTED.value, ModelErrorCategory.MODEL_UNAVAILABLE.value,
})


def _permitted_tool_ids(exec_context: AgentExecutionContext, tool_registry: ToolRegistry,
                        skill_registry: SkillRegistry) -> frozenset[tuple[str, str]]:
    """Union of (tool_id, version) declared compatible by the run's approved
    skills, filtered to what the calling principal actually has permission
    for. A skill naming a tool never grants that permission by itself."""
    permitted: set[tuple[str, str]] = set()
    principal = exec_context.authorization.principal
    for approved in exec_context.approved_skills:
        try:
            manifest = skill_registry.manifest(approved.skill_id, approved.version)
        except Exception:
            continue
        for dep in manifest.compatible_tools:
            spec = tool_registry.resolve(dep.tool_id, dep.version)
            if spec is None:
                continue
            if all(principal.permits(permission) for permission in spec.required_permissions):
                permitted.add((dep.tool_id, dep.version))
    return frozenset(permitted)


def _build_prompt(request: InvestigationRequest) -> str:
    lines = [f"Investigation question: {request.question}"]
    if request.entity_id:
        lines.append(f"The caller believes the relevant starting entity id is: {request.entity_id} "
                     f"(unverified hint — confirm it with a tool before relying on it).")
    return "\n".join(lines)


def _final_text(events: list) -> str | None:
    for event in reversed(events):
        if event.content and event.content.parts:
            texts = [p.text for p in event.content.parts if getattr(p, "text", None)]
            if texts and not event.get_function_calls():
                return "\n".join(texts)
    return None


async def run_investigation(request: InvestigationRequest, exec_context: AgentExecutionContext,
                            model_gateway: ModelGateway, tool_registry: ToolRegistry,
                            tool_executor: ToolExecutor, skill_registry: SkillRegistry) -> InvestigationResult:
    """Runs exactly one bounded investigation and returns a structured,
    independently-validated result. Never raises for an ordinary provider/
    budget/timeout/authorization failure — those become a PARTIAL/
    INCOMPLETE/BLOCKED ``InvestigationResult`` instead.

    ``tool_executor`` is the caller's already-configured
    ``ToolExecutor`` (carrying the real ``ToolDependencies`` — graph store,
    authority rules, etc.) — never constructed here, so this function never
    duplicates how tools reach their backing services.
    """
    permitted = _permitted_tool_ids(exec_context, tool_registry, skill_registry)
    tools = build_investigation_tools(tool_registry, permitted)

    run_state = RunState(exec_context=exec_context, tool_executor=tool_executor, skill_registry=skill_registry)
    token = CURRENT_RUN.set(run_state)

    user_id = f"run:{exec_context.run_id}"  # a label only — never an authentication credential
    session_id = exec_context.run_id
    session_service = InMemorySessionService()  # development-only, in-memory; see module docstring

    try:
        await session_service.create_session(app_name=_APP_NAME, user_id=user_id, session_id=session_id)

        agent = LlmAgent(
            name="nodus_investigator",
            description="General-purpose, single-agent NODUS company-data investigator.",
            model=GatewayLlm(model_gateway),
            instruction=_INSTRUCTION,
            tools=tools,
        )
        runner = Runner(app_name=_APP_NAME, agent=agent, session_service=session_service)
        run_config = RunConfig(max_llm_calls=exec_context.limits.max_llm_calls)
        new_message = types.Content(role="user", parts=[types.Part(text=_build_prompt(request))])

        events: list = []
        timed_out = False
        turn_limit_exceeded = False
        try:
            async def _drive():
                async for event in runner.run_async(
                        user_id=user_id, session_id=session_id, new_message=new_message, run_config=run_config):
                    events.append(event)

            await asyncio.wait_for(_drive(), timeout=exec_context.limits.overall_timeout_seconds)
        except asyncio.TimeoutError:
            timed_out = True
        except LlmCallsLimitExceededError:
            # ADK enforces RunConfig.max_llm_calls by raising rather than ending the generator
            # cleanly; a bounded-turn-limit investigation is INCOMPLETE, never an application error.
            turn_limit_exceeded = True

        return _build_result(request, exec_context, run_state, events, timed_out=timed_out,
                             turn_limit_exceeded=turn_limit_exceeded)
    finally:
        CURRENT_RUN.reset(token)
        try:
            await session_service.delete_session(app_name=_APP_NAME, user_id=user_id, session_id=session_id)
        except Exception:
            pass  # best-effort cleanup only; never let cleanup failure mask the real result


def _build_result(request: InvestigationRequest, exec_context: AgentExecutionContext, run_state: RunState,
                  events: list, *, timed_out: bool, turn_limit_exceeded: bool = False) -> InvestigationResult:
    summary = _final_text(events)
    skill_used = None
    if run_state.selected_skill_descriptor is not None:
        descriptor = run_state.selected_skill_descriptor
        skill_used = SkillUsageRecord(
            skill_id=descriptor.skill_id, version=descriptor.version,
            content_digest=descriptor.content_digest, trust_status=descriptor.trust_status.value,
        )

    rejected: list[str] = []
    if request.entity_id and request.entity_id not in run_state.observed_entity_ids:
        if request.entity_id in run_state.not_found_entity_ids:
            rejected.append(f"entity_id {request.entity_id!r} was looked up and not found")
        else:
            rejected.append(f"entity_id {request.entity_id!r} was never confirmed by an authorized tool call")

    model_usage: ModelUsageSummary = run_state.usage_summary()
    error_message = None

    if timed_out:
        status = InvestigationStatus.INCOMPLETE
        error_message = f"Investigation exceeded its {exec_context.limits.overall_timeout_seconds}s time budget."
        summary = summary or "The investigation did not complete within its time budget."
    elif turn_limit_exceeded:
        status = InvestigationStatus.INCOMPLETE
        error_message = f"Investigation exceeded its {exec_context.limits.max_llm_calls}-call LLM turn limit."
        summary = summary or "The investigation did not reach a final answer within its bounded turn limit."
    elif run_state.hard_stopped_reason is not None:
        error_message = f"Model call stopped: {run_state.hard_stopped_reason}"
        if run_state.hard_stopped_reason == ModelErrorCategory.AUTHORIZATION_DENIED.value:
            status = InvestigationStatus.BLOCKED
        elif run_state.hard_stopped_reason in _RETRYABLE_CATEGORIES:
            status = InvestigationStatus.PARTIAL if summary or run_state.tool_calls_made else \
                InvestigationStatus.INCOMPLETE
        else:
            status = InvestigationStatus.PARTIAL if summary or run_state.tool_calls_made else \
                InvestigationStatus.BLOCKED
        summary = summary or f"The investigation stopped early ({run_state.hard_stopped_reason})."
    elif run_state.selected_skill_descriptor is None:
        status = InvestigationStatus.INCOMPLETE
        summary = summary or "No trusted skill was selected, so no authorized investigation was performed."
        error_message = error_message or "; ".join(run_state.skill_load_errors) or None
    elif summary is None:
        status = InvestigationStatus.INCOMPLETE
        summary = "The agent did not produce a final answer within its bounded turn limit."
    elif run_state.observed_conflicts or run_state.observed_source_limitations or rejected:
        status = InvestigationStatus.PARTIAL
    else:
        status = InvestigationStatus.COMPLETE

    return InvestigationResult(
        run_id=exec_context.run_id,
        status=status,
        summary=summary[:4000],
        entity_ids=tuple(sorted(run_state.observed_entity_ids)),
        relationship_ids=tuple(sorted(run_state.observed_relationship_ids)),
        evidence_refs=tuple(sorted(run_state.observed_evidence_refs)),
        graph_revision=run_state.observed_graph_revision,
        unresolved_conflicts=tuple(run_state.observed_conflicts),
        source_coverage_limitations=tuple(run_state.observed_source_limitations),
        rejected_unverified_claims=tuple(rejected),
        skill_used=skill_used,
        model_usage=model_usage,
        tool_calls=tuple(run_state.tool_call_records),
        error_message=error_message,
    )
