"""The ADK ``BaseLlm`` adapter: the *only* way this agent reaches a model,
and it reaches one exclusively through the existing
``backend.app.gateway.gateway.ModelGateway`` — authorization, model routing,
token preflight, shared-budget reservation, bounded retries, usage/cost
accounting and provider-error classification all still run exactly as they
do for any other ``ModelGateway`` caller (see ``test_model_gateway.py``).

This module does not talk to ``google-genai`` directly and does not
duplicate a Gemini client, a budget policy or retry logic — it only
translates between ADK's ``LlmRequest``/``LlmResponse`` (genai types) and the
gateway's own SDK-agnostic ``ModelRequest``/``ModelResponse`` contracts
(extended in GH-007A to carry tool declarations, function calls, function
responses and multi-turn history — see ``backend/app/gateway/contracts.py``).

Injectable for offline tests: construct a ``GatewayLlm`` over a
``ModelGateway`` backed by the existing ``FakeProviderClient`` test double
(see ``test_model_gateway.py``) to get genuine ADK ``Runner`` events without
any network access or installed credential.
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator

from google.adk.models import BaseLlm, LlmRequest, LlmResponse
from google.genai import types

from backend.app.agent.run_state import RunState, current_run
from backend.app.gateway.contracts import (
    ConversationRole, ConversationTurn, FunctionCall as GatewayFunctionCall,
    FunctionDeclaration, FunctionResponse as GatewayFunctionResponse, ModelRequest, ModelResultStatus,
)
from backend.app.gateway.gateway import ModelGateway


def _extract_system_instruction(config: types.GenerateContentConfig) -> str | None:
    instruction = config.system_instruction
    if instruction is None:
        return None
    if isinstance(instruction, str):
        return instruction
    parts = getattr(instruction, "parts", None)
    if parts:
        texts = [part.text for part in parts if getattr(part, "text", None)]
        if texts:
            return "\n".join(texts)
    return None


def _extract_tool_declarations(config: types.GenerateContentConfig) -> tuple[FunctionDeclaration, ...]:
    declarations: list[FunctionDeclaration] = []
    for tool in config.tools or []:
        for decl in getattr(tool, "function_declarations", None) or []:
            schema = decl.parameters_json_schema
            if schema is None and decl.parameters is not None:
                schema = decl.parameters.model_dump(mode="json", exclude_none=True)
            declarations.append(FunctionDeclaration(
                name=decl.name, description=decl.description or "", parameters=schema or {}))
    return tuple(declarations)


def _content_to_turn(content: types.Content) -> ConversationTurn | None:
    role = ConversationRole.MODEL if content.role == "model" else ConversationRole.USER
    text_parts: list[str] = []
    calls: list[GatewayFunctionCall] = []
    responses: list[GatewayFunctionResponse] = []
    for part in content.parts or []:
        if part.text:
            text_parts.append(part.text)
        if part.function_call is not None:
            calls.append(GatewayFunctionCall(
                id=part.function_call.id, name=part.function_call.name,
                arguments=dict(part.function_call.args or {}),
                thought_signature=getattr(part, "thought_signature", None)))
        if part.function_response is not None:
            responses.append(GatewayFunctionResponse(
                id=part.function_response.id, name=part.function_response.name,
                response=dict(part.function_response.response or {})))
    text = "\n".join(text_parts) if text_parts else None
    if text is None and not calls and not responses:
        return None
    return ConversationTurn(role=role, text=text, function_calls=tuple(calls), function_responses=tuple(responses))


class GatewayLlm(BaseLlm):
    """ADK model implementation backed entirely by ``ModelGateway``.

    ``model`` (required by ``BaseLlm``) is carried only for ADK's own
    bookkeeping/logging; the actual model identity used for a call is
    whatever ``ModelGateway``/``RoleRouter`` resolves for
    ``AgentExecutionContext.model_role`` — this adapter never selects a model
    string itself.
    """

    model_config = {"arbitrary_types_allowed": True}

    model_gateway: ModelGateway

    def __init__(self, model_gateway: ModelGateway, model_label: str = "nodus-gateway") -> None:
        super().__init__(model=model_label, model_gateway=model_gateway)

    @classmethod
    def supported_models(cls) -> list[str]:
        return [r"nodus-gateway.*"]

    async def generate_content_async(
        self, llm_request: LlmRequest, stream: bool = False
    ) -> AsyncGenerator[LlmResponse, None]:
        run_state: RunState = current_run()

        if run_state.hard_stopped_reason is not None:
            yield LlmResponse(error_code=run_state.hard_stopped_reason,
                              error_message="Model calls stopped earlier in this run; not retrying.")
            return

        self._maybe_append_user_content(llm_request)

        turns = []
        for content in llm_request.contents:
            turn = _content_to_turn(content)
            if turn is not None:
                turns.append(turn)

        system_instruction = _extract_system_instruction(llm_request.config)
        tools = _extract_tool_declarations(llm_request.config)
        limits = run_state.exec_context.limits

        request = ModelRequest(
            context=run_state.exec_context.authorization,
            correlation_id=f"{run_state.exec_context.run_id}:{run_state.llm_calls}",
            role=run_state.exec_context.model_role,
            prompt="",
            system_instruction=system_instruction,
            tools=tools,
            history=tuple(turns),
            max_output_tokens=limits.max_output_tokens_per_call,
            max_total_tokens=limits.max_total_tokens_per_call,
            temperature=llm_request.config.temperature,
            budget_key=limits.request_budget_key,
        )

        response = await asyncio.to_thread(self.model_gateway.generate, request)

        run_state.record_llm_usage(
            input_tokens=response.usage.input_tokens, output_tokens=response.usage.output_tokens,
            total_tokens=response.usage.total_tokens, estimated_cost_usd=response.usage.estimated_cost_usd,
            actual_model=response.actual_model,
        )

        if response.status is not ModelResultStatus.SUCCESS:
            category = response.error_category.value if response.error_category else "UNKNOWN"
            run_state.hard_stopped_reason = category
            yield LlmResponse(error_code=category, error_message=response.error_message or category)
            return

        parts: list[types.Part] = []
        if response.text:
            parts.append(types.Part(text=response.text))
        for call in response.function_calls:
            # ``thought_signature`` lives on the Part, not on FunctionCall itself (Gemini 3.x "thinking"
            # models reject a later replayed function-call turn that is missing it — see
            # backend.app.gateway.provider.RawFunctionCall).
            parts.append(types.Part(function_call=types.FunctionCall(
                id=call.id, name=call.name, args=call.arguments), thought_signature=call.thought_signature))

        usage_metadata = types.GenerateContentResponseUsageMetadata(
            prompt_token_count=response.usage.input_tokens,
            candidates_token_count=response.usage.output_tokens,
            total_token_count=response.usage.total_tokens,
        )

        yield LlmResponse(
            content=types.Content(role="model", parts=parts),
            model_version=response.actual_model,
            usage_metadata=usage_metadata,
        )
