"""Thin adapter over the official ``google-genai`` SDK.

Import of this module never requires ``google-genai`` to be installed; the
SDK is imported lazily inside ``GeminiProviderClient.__init__`` so offline
tests (which inject a fake ``ProviderClient``) never need it. Only
constructing a real client, or running the opt-in live smoke test, requires
the package and a configured ``GEMINI_API_KEY``.
"""
from __future__ import annotations

from backend.app.gateway.provider import (
    AuthenticationFailedError, InvalidRequestProviderError, MissingCredentialError, ModelUnavailableError,
    ProviderError, ProviderResponse, RateLimitedError, RawFunctionCall, RawUsage, ProviderUnavailableError,
)

_SAFETY_FINISH_REASONS = {"SAFETY", "PROHIBITED_CONTENT", "BLOCKLIST", "RECITATION"}


def _status_code(exc: Exception) -> int | None:
    for attr in ("code", "status_code"):
        value = getattr(exc, "code", None) if attr == "code" else getattr(exc, "status_code", None)
        if isinstance(value, int):
            return value
    return None


def _translate(exc: Exception) -> ProviderError:
    status = _status_code(exc)
    message = str(exc)
    class_name = exc.__class__.__name__
    if status in (401, 403):
        return AuthenticationFailedError(message)
    if status == 400:
        return InvalidRequestProviderError(message)
    if status == 404:
        return ModelUnavailableError(message)
    if status == 429:
        return RateLimitedError(message)
    if status is not None and 500 <= status < 600:
        return ProviderUnavailableError(message)
    if "ServerError" in class_name:
        return ProviderUnavailableError(message)
    if "ClientError" in class_name:
        return InvalidRequestProviderError(message)
    return ProviderError(message)


class GeminiProviderClient:
    """Backend-only credential loading: the API key is never accepted from a
    request field, only from this constructor (ultimately sourced from
    ``GEMINI_API_KEY`` via ``GatewayConfig``)."""

    def __init__(self, api_key: str | None) -> None:
        if not api_key:
            raise MissingCredentialError("GEMINI_API_KEY is not configured")
        try:
            from google import genai
            from google.genai import types
        except ImportError as exc:
            raise RuntimeError(
                "google-genai is not installed; add it to requirements.txt and install it") from exc
        self._types = types
        self._client = genai.Client(api_key=api_key)

    def count_tokens(self, model: str, contents) -> int:
        try:
            result = self._client.models.count_tokens(model=model, contents=contents)
        except Exception as exc:
            raise _translate(exc) from exc
        return int(result.total_tokens)

    def generate(self, model: str, contents, config) -> ProviderResponse:
        kwargs: dict = {"max_output_tokens": config.max_output_tokens}
        if config.temperature is not None:
            kwargs["temperature"] = config.temperature
        if config.system_instruction:
            kwargs["system_instruction"] = config.system_instruction
        if config.response_schema is not None:
            kwargs["response_mime_type"] = "application/json"
            kwargs["response_schema"] = config.response_schema
        if config.tools:
            declarations = [
                self._types.FunctionDeclaration(
                    name=tool.name, description=tool.description,
                    parameters_json_schema=tool.parameters or {"type": "object", "properties": {}},
                )
                for tool in config.tools
            ]
            kwargs["tools"] = [self._types.Tool(function_declarations=declarations)]

        try:
            response = self._client.models.generate_content(
                model=model, contents=contents, config=self._types.GenerateContentConfig(**kwargs))
        except Exception as exc:
            raise _translate(exc) from exc

        usage_meta = getattr(response, "usage_metadata", None)
        usage = RawUsage(
            input_tokens=getattr(usage_meta, "prompt_token_count", None),
            output_tokens=getattr(usage_meta, "candidates_token_count", None),
            thinking_tokens=getattr(usage_meta, "thoughts_token_count", None),
            total_tokens=getattr(usage_meta, "total_token_count", None),
        )

        parsed = None
        if config.response_schema is not None:
            parsed_obj = getattr(response, "parsed", None)
            if parsed_obj is not None:
                parsed = parsed_obj.model_dump(mode="json") if hasattr(parsed_obj, "model_dump") else parsed_obj

        finish_reason = None
        safety_blocked = False
        function_calls: tuple[RawFunctionCall, ...] = ()
        candidates = getattr(response, "candidates", None) or []
        if candidates:
            raw_reason = getattr(candidates[0], "finish_reason", None)
            finish_reason = getattr(raw_reason, "name", raw_reason)
            safety_blocked = finish_reason in _SAFETY_FINISH_REASONS
            content = getattr(candidates[0], "content", None)
            parts = getattr(content, "parts", None) or []
            collected = []
            for part in parts:
                call = getattr(part, "function_call", None)
                if call is not None:
                    collected.append(RawFunctionCall(
                        id=getattr(call, "id", None), name=call.name, arguments=dict(call.args or {}),
                        thought_signature=getattr(part, "thought_signature", None)))
            function_calls = tuple(collected)

        text = None
        if config.response_schema is None:
            try:
                text = response.text
            except Exception:  # pragma: no cover - defensive: text-less (tool-only) responses
                text = None

        return ProviderResponse(
            text=text, parsed=parsed, usage=usage, model_version=getattr(response, "model_version", None),
            finish_reason=finish_reason, safety_blocked=safety_blocked, function_calls=function_calls,
        )

    def close(self) -> None:
        """Releases the underlying HTTP session. The SDK itself does not
        require this for correctness, but a long-lived process should call
        it (e.g. on application shutdown) rather than leaking the session."""
        self._client.close()

    def available_models(self) -> frozenset[str]:
        try:
            listed = self._client.models.list()
        except Exception as exc:
            raise _translate(exc) from exc
        names = set()
        for item in listed:
            name = getattr(item, "name", "") or ""
            names.add(name.rsplit("/", 1)[-1])
        return frozenset(names)
