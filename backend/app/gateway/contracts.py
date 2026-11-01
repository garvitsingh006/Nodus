"""Typed gateway contracts: requests, responses, usage and error categories.

Reuses ``backend.app.contracts.Record`` (frozen Pydantic base) and the Stage
01 security contract ``AuthorizationContext`` rather than inventing parallel
trust/identity types. ``ModelRequest.context`` must always originate from
trusted server-side construction (see ``AuthorizationContext`` docstring) —
never from model-generated text. No secret or credential is ever a field on
these contracts; the gateway reads ``GEMINI_API_KEY`` itself.
"""
from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from backend.app.contracts import Record
from backend.app.security import AuthorizationContext


class ModelRole(StrEnum):
    """Deterministic role identifiers. The role-to-model mapping lives in
    ``RoleRouter``, not here — this enum is stable even as models change."""
    LIGHTWEIGHT = "LIGHTWEIGHT"
    DEFAULT = "DEFAULT"
    ESCALATION = "ESCALATION"


class ModelResultStatus(StrEnum):
    SUCCESS = "SUCCESS"
    ERROR = "ERROR"


class ModelErrorCategory(StrEnum):
    AUTHORIZATION_DENIED = "AUTHORIZATION_DENIED"
    INVALID_REQUEST = "INVALID_REQUEST"
    AUTHENTICATION_FAILED = "AUTHENTICATION_FAILED"
    MODEL_UNAVAILABLE = "MODEL_UNAVAILABLE"
    PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"
    RATE_LIMITED = "RATE_LIMITED"
    QUOTA_EXHAUSTED = "QUOTA_EXHAUSTED"
    BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"
    SAFETY_BLOCKED = "SAFETY_BLOCKED"
    INVALID_STRUCTURED_RESPONSE = "INVALID_STRUCTURED_RESPONSE"
    RESPONSE_TOO_LARGE = "RESPONSE_TOO_LARGE"
    TIMEOUT = "TIMEOUT"
    UNKNOWN = "UNKNOWN"


class FunctionDeclaration(Record):
    """A tool the model may call, described independently of any particular
    SDK's schema type (see ``backend/app/agent/tool_bridge.py`` for the ADK
    translation). ``parameters`` is a plain JSON-Schema-shaped dict (e.g. a
    Pydantic ``model_json_schema()`` output) — never executable code."""
    name: str = Field(min_length=1, max_length=64)
    description: str = Field(default="", max_length=2000)
    parameters: dict = Field(default_factory=dict)


class FunctionCall(Record):
    """A model-requested tool invocation. ``arguments`` are whatever the
    model produced — never trusted as authorized input until validated and
    executed by ``backend.app.tools.executor.ToolExecutor``.

    ``thought_signature`` (Gemini 3.x "thinking" models) must be carried
    forward unchanged alongside this exact call when it is replayed as
    history in a later turn — see ``backend.app.gateway.provider.
    RawFunctionCall`` for why. Opaque; never inspected or modified, only
    round-tripped."""
    id: str | None = None
    name: str = Field(min_length=1)
    arguments: dict = Field(default_factory=dict)
    thought_signature: bytes | None = None


class FunctionResponse(Record):
    """The result of a previously requested ``FunctionCall``, fed back to the
    model as conversation history. ``response`` is always a tool's own
    structured output — never raw database/filesystem access."""
    id: str | None = None
    name: str = Field(min_length=1)
    response: dict = Field(default_factory=dict)


class ConversationRole(StrEnum):
    USER = "user"
    MODEL = "model"


class ConversationTurn(Record):
    """One prior turn of a multi-turn tool-calling conversation. Carries at
    least one of text / function_calls / function_responses — an empty turn
    is never meaningful history."""
    role: ConversationRole
    text: str | None = Field(default=None, max_length=200_000)
    function_calls: tuple[FunctionCall, ...] = ()
    function_responses: tuple[FunctionResponse, ...] = ()

    @model_validator(mode="after")
    def non_empty(self):
        if self.text is None and not self.function_calls and not self.function_responses:
            raise ValueError("A conversation turn must carry text, function_calls or function_responses")
        return self


class ModelRequest(Record):
    """A single typed model call. ``output_schema``, when set, is validated
    twice: once by the provider's structured-output mode, once again here
    with Pydantic — schema-valid JSON is never treated as factually correct,
    only as structurally correct.

    ``tools``/``history`` are an additive, backward-compatible extension
    (GH-007A) for the function-calling round trip an ADK-driven agent needs:
    a request with neither populated behaves exactly as before (a single
    plain-text ``prompt``). When either is populated, the gateway assembles
    the full ``history`` (plus ``prompt`` as a trailing user turn, if
    non-empty) into the structured multi-turn ``contents`` the provider
    receives — never bypassing budget/usage accounting, which still runs
    over the same preflight/reservation/reconciliation path."""
    model_config = ConfigDict(arbitrary_types_allowed=True)

    context: AuthorizationContext
    correlation_id: str = Field(min_length=1)
    role: ModelRole
    prompt: str = Field(default="", max_length=200_000)
    system_instruction: str | None = Field(default=None, max_length=20_000)
    output_schema: type[BaseModel] | None = None
    tools: tuple[FunctionDeclaration, ...] = ()
    history: tuple[ConversationTurn, ...] = ()
    max_output_tokens: int = Field(default=1024, ge=1, le=65_536)
    max_total_tokens: int = Field(default=4096, ge=1)
    max_output_bytes: int = Field(default=65_536, ge=1)
    timeout_seconds: float = Field(default=30.0, gt=0, le=300)
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    budget_key: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def budget_covers_output(self):
        if self.max_output_tokens > self.max_total_tokens:
            raise ValueError("max_output_tokens cannot exceed max_total_tokens")
        return self

    @model_validator(mode="after")
    def has_content(self):
        if not self.prompt and not self.history:
            raise ValueError("ModelRequest requires a non-empty prompt or at least one history turn")
        return self


class ModelUsage(Record):
    requested_model: str = Field(min_length=1)
    actual_model: str | None = None
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    thinking_tokens: int | None = Field(default=None, ge=0)
    total_tokens: int | None = Field(default=None, ge=0)
    is_estimated: bool = True
    estimated_cost_usd: float | None = Field(default=None, ge=0)
    pricing_version: str | None = None


class ModelResponse(Record):
    status: ModelResultStatus
    correlation_id: str = Field(min_length=1)
    role: ModelRole
    requested_model: str = Field(min_length=1)
    actual_model: str | None = None
    text: str | None = None
    structured: dict | None = None
    function_calls: tuple[FunctionCall, ...] = ()
    usage: ModelUsage
    error_category: ModelErrorCategory | None = None
    error_message: str | None = Field(default=None, max_length=1000)
    retries: int = Field(default=0, ge=0)
    latency_ms: float | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def consistent(self):
        if self.status is ModelResultStatus.SUCCESS:
            if self.error_category is not None or self.error_message is not None:
                raise ValueError("Successful responses must not carry error details")
        else:
            if self.text is not None or self.structured is not None or self.function_calls:
                raise ValueError("Error responses must not carry generated content")
            if self.error_category is None:
                raise ValueError("Error responses require an error category")
        return self
