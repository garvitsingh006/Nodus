"""The single orchestration path for a model call: authorize, route,
preflight, reserve budget, call the provider with bounded retries, validate
structured output, account usage/cost, reconcile budget.

No agent loop: this executes exactly one request and returns exactly one
``ModelResponse``. It never executes a tool-call suggestion a model returns,
and it never lets a model-generated assertion modify the canonical graph —
callers remain responsible for anything done with ``ModelResponse.text`` or
``.structured``.
"""
from __future__ import annotations

import re
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError

from pydantic import ValidationError

from backend.app.gateway.budget import BudgetExceededError, BudgetNotConfiguredError, SharedTokenBudgetTracker
from backend.app.gateway.contracts import (
    ConversationRole, ConversationTurn, FunctionCall, ModelErrorCategory, ModelRequest, ModelResponse,
    ModelResultStatus, ModelUsage,
)
from backend.app.gateway.pricing import PricingTable
from backend.app.gateway.provider import (
    GenerationConfig, ProviderClient, ProviderError, ProviderResponse, TimeoutProviderError,
)
from backend.app.gateway.retry import RetryPolicy
from backend.app.gateway.routing import ModelNotAllowlistedError, RoleRouter, UnknownModelRoleError
from backend.app.security import Permission

_POOL = ThreadPoolExecutor(max_workers=16, thread_name_prefix="model-gateway")
_SECRET_PATTERN = re.compile(r"(AIza[0-9A-Za-z_\-]{10,}|key=[^&\s\"']+|Bearer\s+\S+)", re.IGNORECASE)


def _safe_message(exc: Exception | None) -> str:
    if exc is None:
        return "Unknown provider error"
    return _SECRET_PATTERN.sub("[redacted]", str(exc)[:500])


def _category(exc: Exception | None) -> ModelErrorCategory:
    name = getattr(exc, "category", None)
    try:
        return ModelErrorCategory(name)
    except ValueError:
        return ModelErrorCategory.UNKNOWN


def _turn_payload(turn: ConversationTurn) -> dict:
    """Maps one ``ConversationTurn`` onto a plain ``{"role", "parts"}`` dict
    shaped like the google-genai SDK's own ``Content``/``Part`` — the real
    SDK accepts this dict form directly (see ``gemini_client.py``), so no
    SDK-specific type is constructed here.

    ``thought_signature`` is a ``Part``-level field in the SDK's schema, a
    sibling of ``function_call`` — never nested inside it (verified against
    ``google.genai.types.Part``/``FunctionCall``'s own field lists). Gemini
    3.x rejects a replayed function-call turn missing it outright (see
    ``backend.app.gateway.provider.RawFunctionCall``)."""
    parts: list[dict] = []
    if turn.text:
        parts.append({"text": turn.text})
    for call in turn.function_calls:
        part = {"name": call.name, "args": call.arguments}
        if call.id:
            part["id"] = call.id
        part_payload: dict = {"function_call": part}
        if call.thought_signature:
            part_payload["thought_signature"] = call.thought_signature
        parts.append(part_payload)
    for response in turn.function_responses:
        part = {"name": response.name, "response": response.response}
        if response.id:
            part["id"] = response.id
        parts.append({"function_response": part})
    return {"role": turn.role.value, "parts": parts}


def _contents_for_provider(request: ModelRequest):
    """Fully backward-compatible: a request with no ``history``/``tools``
    sends exactly ``request.prompt`` as a plain string, byte-for-byte what
    every pre-GH-007A caller already does. Only a request that actually uses
    the function-calling extension pays for building structured contents."""
    if not request.history and not request.tools:
        return request.prompt
    turns = list(request.history)
    if request.prompt:
        turns.append(ConversationTurn(role=ConversationRole.USER, text=request.prompt))
    return [_turn_payload(turn) for turn in turns]


class ModelGateway:
    """The only path through which a Gemini model may be invoked."""

    def __init__(self, provider: ProviderClient, routing: RoleRouter, pricing: PricingTable | None = None,
                 retry_policy: RetryPolicy | None = None,
                 budget_tracker: SharedTokenBudgetTracker | None = None) -> None:
        self.provider = provider
        self.routing = routing
        self.pricing = pricing or PricingTable()
        self.retry_policy = retry_policy or RetryPolicy()
        self.budget_tracker = budget_tracker

    def _error(self, request: ModelRequest, model: str | None, category: ModelErrorCategory,
               message: str, started: float, retries: int, usage: ModelUsage | None = None) -> ModelResponse:
        return ModelResponse(
            status=ModelResultStatus.ERROR, correlation_id=request.correlation_id, role=request.role,
            requested_model=model or "unresolved", actual_model=None,
            usage=usage or ModelUsage(requested_model=model or "unresolved", is_estimated=True),
            error_category=category, error_message=message, retries=retries,
            latency_ms=(time.monotonic() - started) * 1000,
        )

    def _usage(self, model: str, response: ProviderResponse, preflight_input_tokens: int) -> ModelUsage:
        raw = response.usage
        input_tokens = raw.input_tokens if raw.input_tokens is not None else preflight_input_tokens
        is_estimated = raw.input_tokens is None or raw.output_tokens is None
        total = raw.total_tokens
        if total is None and raw.output_tokens is not None:
            total = input_tokens + raw.output_tokens + (raw.thinking_tokens or 0)
        cost = None if is_estimated else self.pricing.estimate_cost(model, input_tokens, raw.output_tokens)
        return ModelUsage(
            requested_model=model, actual_model=response.model_version or model,
            input_tokens=input_tokens, output_tokens=raw.output_tokens, thinking_tokens=raw.thinking_tokens,
            total_tokens=total, is_estimated=is_estimated, estimated_cost_usd=cost,
            pricing_version=self.pricing.version,
        )

    def generate(self, request: ModelRequest) -> ModelResponse:
        started = time.monotonic()

        # Trust boundary: tenant/workspace/permission identity comes only from
        # request.context (server-constructed), never from request.prompt.
        if Permission.EXECUTE_TOOL not in request.context.permissions:
            return self._error(request, None, ModelErrorCategory.AUTHORIZATION_DENIED,
                               "Principal lacks EXECUTE_TOOL permission", started, 0)

        try:
            model = self.routing.resolve(request.role)
        except UnknownModelRoleError as exc:
            return self._error(request, None, ModelErrorCategory.INVALID_REQUEST, str(exc), started, 0)
        except ModelNotAllowlistedError as exc:
            return self._error(request, None, ModelErrorCategory.MODEL_UNAVAILABLE, str(exc), started, 0)

        contents = _contents_for_provider(request)

        try:
            preflight_input_tokens = self.provider.count_tokens(model, contents)
        except ProviderError as exc:
            return self._error(request, model, _category(exc), _safe_message(exc), started, 0)
        except Exception as exc:  # pragma: no cover - defensive, unexpected adapter failure
            return self._error(request, model, ModelErrorCategory.UNKNOWN, "Token preflight failed", started, 0)

        estimated_total = preflight_input_tokens + request.max_output_tokens
        if estimated_total > request.max_total_tokens:
            return self._error(
                request, model, ModelErrorCategory.BUDGET_EXHAUSTED,
                "Estimated input+output tokens exceed the request's token budget", started, 0,
                usage=ModelUsage(requested_model=model, input_tokens=preflight_input_tokens, is_estimated=True))

        reservation_id = None
        if request.budget_key is not None:
            if self.budget_tracker is None:
                return self._error(request, model, ModelErrorCategory.INVALID_REQUEST,
                                   "Shared budget requested but no budget tracker is configured", started, 0)
            try:
                reservation_id = self.budget_tracker.reserve(request.budget_key, estimated_total)
            except BudgetNotConfiguredError as exc:
                return self._error(request, model, ModelErrorCategory.INVALID_REQUEST, str(exc), started, 0)
            except BudgetExceededError as exc:
                return self._error(request, model, ModelErrorCategory.BUDGET_EXHAUSTED, str(exc), started, 0)

        config = GenerationConfig(
            max_output_tokens=request.max_output_tokens, temperature=request.temperature,
            response_schema=request.output_schema, system_instruction=request.system_instruction,
            timeout_seconds=request.timeout_seconds, tools=request.tools,
        )

        attempt = 0
        last_error: Exception | None = None
        provider_response: ProviderResponse | None = None
        while attempt <= self.retry_policy.max_retries:
            try:
                future = _POOL.submit(self.provider.generate, model, contents, config)
                provider_response = future.result(timeout=request.timeout_seconds)
                last_error = None
                break
            except FutureTimeoutError:
                future.cancel()
                last_error = TimeoutProviderError("Provider call exceeded its timeout")
            except ProviderError as exc:
                last_error = exc
                if not self.retry_policy.is_retryable(_category(exc).value):
                    break
            except Exception as exc:  # pragma: no cover - defensive, unexpected adapter failure
                last_error = exc
                break
            if attempt >= self.retry_policy.max_retries:
                break
            time.sleep(self.retry_policy.delay_for(attempt))
            attempt += 1

        if provider_response is None:
            if reservation_id is not None:
                self.budget_tracker.release(reservation_id)
            category = _category(last_error)
            return self._error(
                request, model, category, _safe_message(last_error), started, attempt,
                usage=ModelUsage(requested_model=model, input_tokens=preflight_input_tokens, is_estimated=True))

        usage = self._usage(model, provider_response, preflight_input_tokens)
        if reservation_id is not None:
            actual = usage.total_tokens if usage.total_tokens is not None else estimated_total
            self.budget_tracker.reconcile(reservation_id, actual)

        if provider_response.safety_blocked:
            return self._error(request, model, ModelErrorCategory.SAFETY_BLOCKED,
                               "Provider blocked the response for safety reasons", started, attempt, usage=usage)

        structured = None
        if request.output_schema is not None:
            try:
                if provider_response.parsed is not None:
                    validated = request.output_schema.model_validate(provider_response.parsed)
                else:
                    validated = request.output_schema.model_validate_json(provider_response.text or "")
            except ValidationError:
                return self._error(request, model, ModelErrorCategory.INVALID_STRUCTURED_RESPONSE,
                                   "Structured response failed schema validation", started, attempt, usage=usage)
            structured = validated.model_dump(mode="json")

        text = provider_response.text if request.output_schema is None else None
        function_calls = tuple(
            FunctionCall(id=call.id, name=call.name, arguments=call.arguments,
                        thought_signature=call.thought_signature)
            for call in provider_response.function_calls
        )
        payload_bytes = len((structured and str(structured) or text or "").encode("utf-8"))
        payload_bytes += sum(len(str(call.arguments).encode("utf-8")) for call in function_calls)
        if payload_bytes > request.max_output_bytes:
            return self._error(request, model, ModelErrorCategory.RESPONSE_TOO_LARGE,
                               "Response exceeded the request's output byte limit", started, attempt, usage=usage)

        return ModelResponse(
            status=ModelResultStatus.SUCCESS, correlation_id=request.correlation_id, role=request.role,
            requested_model=model, actual_model=provider_response.model_version or model,
            text=text, structured=structured, function_calls=function_calls, usage=usage, retries=attempt,
            latency_ms=(time.monotonic() - started) * 1000,
        )

    def verify_models(self, models) -> dict[str, bool]:
        """Optional authenticated availability check. Never called from
        ``generate()`` — offline tests and ordinary requests never need a
        live round trip to the provider."""
        available = self.provider.available_models()
        return {model: model in available for model in models}
