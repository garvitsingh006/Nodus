"""Provider-facing types and the injectable ``ProviderClient`` boundary.

The gateway never talks to ``google-genai`` directly — it calls through this
Protocol, so offline tests can inject a fake adapter instead of hitting the
network or requiring the SDK/credentials to be installed/present.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class RawUsage:
    input_tokens: int | None
    output_tokens: int | None
    thinking_tokens: int | None
    total_tokens: int | None


@dataclass(frozen=True)
class RawFunctionCall:
    """A model-requested tool call as the provider reported it, before the
    gateway maps it onto ``backend.app.gateway.contracts.FunctionCall``.

    ``thought_signature`` (Gemini 3.x "thinking" models only) is an opaque
    signature the provider must be sent back unchanged alongside this exact
    function call in the next turn's history, or the live API rejects the
    request outright (HTTP 400: "Function call is missing a
    thought_signature in functionCall parts") — verified against the real
    API during ND-13C live acceptance; the offline ``FakeProviderClient``-
    backed test suite never exercises this because it never round-trips a
    real provider response. ``None`` for providers/models that don't use it."""
    id: str | None
    name: str
    arguments: dict
    thought_signature: bytes | None = None


@dataclass(frozen=True)
class ProviderResponse:
    text: str | None
    parsed: dict | None
    usage: RawUsage
    model_version: str | None
    finish_reason: str | None
    safety_blocked: bool = False
    function_calls: tuple[RawFunctionCall, ...] = ()


@dataclass(frozen=True)
class GenerationConfig:
    max_output_tokens: int
    temperature: float | None
    response_schema: type | None
    system_instruction: str | None
    timeout_seconds: float
    tools: tuple = ()
    """Tuple of ``backend.app.gateway.contracts.FunctionDeclaration`` — kept
    untyped here (rather than importing gateway.contracts) to avoid a
    circular import; empty by default, so existing callers are unaffected."""


@runtime_checkable
class ProviderClient(Protocol):
    def count_tokens(self, model: str, contents: "str | list") -> int: ...
    def generate(self, model: str, contents: "str | list", config: GenerationConfig) -> ProviderResponse: ...
    def available_models(self) -> frozenset[str]: ...


class ProviderError(Exception):
    category = "UNKNOWN"


class MissingCredentialError(ProviderError):
    category = "AUTHENTICATION_FAILED"


class AuthenticationFailedError(ProviderError):
    category = "AUTHENTICATION_FAILED"


class InvalidRequestProviderError(ProviderError):
    category = "INVALID_REQUEST"


class ModelUnavailableError(ProviderError):
    category = "MODEL_UNAVAILABLE"


class ProviderUnavailableError(ProviderError):
    category = "PROVIDER_UNAVAILABLE"

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class RateLimitedError(ProviderError):
    category = "RATE_LIMITED"

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class QuotaExhaustedError(ProviderError):
    category = "QUOTA_EXHAUSTED"


class SafetyBlockedError(ProviderError):
    category = "SAFETY_BLOCKED"


class TimeoutProviderError(ProviderError):
    category = "TIMEOUT"
