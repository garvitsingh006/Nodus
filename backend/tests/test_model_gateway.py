"""GH-006A acceptance: Gemini model gateway, fully offline via an injectable
fake ``ProviderClient`` — no network access, no SDK/credential dependency."""
from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from pydantic import BaseModel

from backend.app.gateway.budget import BudgetExceededError, BudgetNotConfiguredError, SharedTokenBudgetTracker
from backend.app.gateway.contracts import ModelErrorCategory, ModelRequest, ModelResultStatus, ModelRole
from backend.app.gateway.gateway import ModelGateway
from backend.app.gateway.gemini_client import GeminiProviderClient
from backend.app.gateway.pricing import ModelPricing, PricingTable
from backend.app.gateway.provider import (
    AuthenticationFailedError, MissingCredentialError, ProviderResponse, RateLimitedError, RawUsage,
)
from backend.app.gateway.retry import RetryPolicy
from backend.app.gateway.routing import RoleRouter
from backend.app.security import AuthorizationContext, Permission, Principal


def _context(permissions=frozenset({Permission.EXECUTE_TOOL})) -> AuthorizationContext:
    principal = Principal(subject="agent", tenant_id="t1", workspaces=("w1",), permissions=permissions)
    return AuthorizationContext(principal=principal, tenant_id="t1", workspace_id="w1", task_id="task-1",
                               permissions=permissions)


def _request(**overrides) -> ModelRequest:
    defaults = dict(context=_context(), correlation_id="corr-1", role=ModelRole.DEFAULT, prompt="hello")
    defaults.update(overrides)
    return ModelRequest(**defaults)


def _router(**models) -> RoleRouter:
    mapping = {ModelRole.LIGHTWEIGHT: "fake-lite", ModelRole.DEFAULT: "fake-default"}
    mapping.update(models)
    return RoleRouter(role_models=mapping)


class FakeProviderClient:
    """Injectable test double: scripted responses/exceptions, call counting."""

    def __init__(self, input_tokens: int = 5):
        self.input_tokens = input_tokens
        self.calls: list[str] = []
        self.queue: list = []  # list of ProviderResponse | Exception
        self._models = frozenset({"fake-lite", "fake-default", "fake-escalation"})

    def count_tokens(self, model: str, contents: str) -> int:
        return self.input_tokens

    def generate(self, model: str, contents: str, config) -> ProviderResponse:
        self.calls.append(model)
        item = self.queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def available_models(self) -> frozenset[str]:
        return self._models


def _success(text="ok", parsed=None, input_tokens=5, output_tokens=3, model_version="fake-default-001"):
    return ProviderResponse(
        text=text, parsed=parsed,
        usage=RawUsage(input_tokens=input_tokens, output_tokens=output_tokens, thinking_tokens=0,
                       total_tokens=input_tokens + output_tokens),
        model_version=model_version, finish_reason="STOP", safety_blocked=False,
    )


# 1. Model-role selection
def test_role_selection_is_deterministic_and_preserves_actual_model():
    provider = FakeProviderClient()
    provider.queue.append(_success())
    gateway = ModelGateway(provider, _router())
    response = gateway.generate(_request(role=ModelRole.DEFAULT))
    assert response.status == ModelResultStatus.SUCCESS
    assert response.requested_model == "fake-default"
    assert response.actual_model == "fake-default-001"
    assert provider.calls == ["fake-default"]


# 2. Unknown/unavailable model handling
def test_unknown_role_is_rejected_without_a_provider_call():
    provider = FakeProviderClient()
    gateway = ModelGateway(provider, _router())
    response = gateway.generate(_request(role=ModelRole.ESCALATION))
    assert response.status == ModelResultStatus.ERROR
    assert response.error_category == ModelErrorCategory.INVALID_REQUEST
    assert provider.calls == []


def test_model_outside_allowlist_fails_clearly_at_configuration_time():
    """Routing never silently substitutes a model: a role mapped to a model
    outside the explicit allowlist is rejected when the router is built,
    not discovered later mid-request."""
    with pytest.raises(ValueError):
        RoleRouter(role_models={ModelRole.LIGHTWEIGHT: "fake-lite", ModelRole.DEFAULT: "not-allowed"},
                  allowlist=frozenset({"fake-lite"}))


# 3. Missing credential behavior
def test_missing_credential_fails_clearly_before_any_sdk_use():
    with pytest.raises(MissingCredentialError):
        GeminiProviderClient(api_key=None)
    with pytest.raises(MissingCredentialError):
        GeminiProviderClient(api_key="")


# 4. Typed request validation
def test_invalid_arguments_rejected_before_any_provider_call():
    with pytest.raises(Exception):
        _request(prompt="")  # empty prompt
    with pytest.raises(Exception):
        _request(max_output_tokens=10_000, max_total_tokens=100)  # output exceeds total budget


# 5. Structured output validation
class Verdict(BaseModel):
    ok: bool
    reason: str


def test_structured_output_is_validated_against_the_schema():
    provider = FakeProviderClient()
    provider.queue.append(_success(text=None, parsed={"ok": True, "reason": "looks fine"}))
    gateway = ModelGateway(provider, _router())
    response = gateway.generate(_request(output_schema=Verdict))
    assert response.status == ModelResultStatus.SUCCESS
    assert response.structured == {"ok": True, "reason": "looks fine"}
    assert response.text is None


# 6. Malformed response rejection (distinguished from provider failure)
def test_malformed_structured_response_is_rejected_not_a_provider_failure():
    provider = FakeProviderClient()
    provider.queue.append(_success(text=None, parsed={"ok": "not-a-bool"}))
    gateway = ModelGateway(provider, _router())
    response = gateway.generate(_request(output_schema=Verdict))
    assert response.status == ModelResultStatus.ERROR
    assert response.error_category == ModelErrorCategory.INVALID_STRUCTURED_RESPONSE


# 7. Token preflight limits
def test_token_preflight_rejects_before_network_call_when_budget_insufficient():
    provider = FakeProviderClient(input_tokens=4000)
    gateway = ModelGateway(provider, _router())
    response = gateway.generate(_request(max_total_tokens=4096, max_output_tokens=1024))
    assert response.status == ModelResultStatus.ERROR
    assert response.error_category == ModelErrorCategory.BUDGET_EXHAUSTED
    assert provider.calls == []  # no network call was made


# 8. Budget exhaustion before network execution (shared budget)
def test_shared_budget_exhausted_before_network_execution():
    provider = FakeProviderClient(input_tokens=10)
    tracker = SharedTokenBudgetTracker()
    tracker.configure("tenant-1", limit=20)
    gateway = ModelGateway(provider, _router(), budget_tracker=tracker)
    response = gateway.generate(_request(budget_key="tenant-1", max_output_tokens=50, max_total_tokens=100))
    assert response.status == ModelResultStatus.ERROR
    assert response.error_category == ModelErrorCategory.BUDGET_EXHAUSTED
    assert provider.calls == []


def test_unconfigured_shared_budget_key_fails_clearly():
    provider = FakeProviderClient()
    tracker = SharedTokenBudgetTracker()
    gateway = ModelGateway(provider, _router(), budget_tracker=tracker)
    response = gateway.generate(_request(budget_key="unknown-key"))
    assert response.status == ModelResultStatus.ERROR
    assert response.error_category == ModelErrorCategory.INVALID_REQUEST


# 9. Usage and cost accounting
def test_usage_and_cost_accounting_uses_actual_tokens_when_reported():
    provider = FakeProviderClient(input_tokens=5)
    provider.queue.append(_success(input_tokens=7, output_tokens=11))
    pricing = PricingTable(version="v1", prices={"fake-default": ModelPricing(input_per_million=1.0,
                                                                              output_per_million=2.0)})
    gateway = ModelGateway(provider, _router(), pricing=pricing)
    response = gateway.generate(_request())
    assert response.usage.is_estimated is False
    assert response.usage.input_tokens == 7
    assert response.usage.output_tokens == 11
    assert response.usage.total_tokens == 18
    expected_cost = 7 / 1_000_000 * 1.0 + 11 / 1_000_000 * 2.0
    assert response.usage.estimated_cost_usd == pytest.approx(expected_cost)


def test_cost_is_unknown_without_configured_pricing():
    provider = FakeProviderClient()
    provider.queue.append(_success())
    gateway = ModelGateway(provider, _router())  # default empty PricingTable
    response = gateway.generate(_request())
    assert response.usage.estimated_cost_usd is None


# 10. Concurrent shared-budget reservation is atomic
def test_concurrent_shared_budget_reservations_never_overdraw():
    tracker = SharedTokenBudgetTracker()
    tracker.configure("shared", limit=100)
    successes = []
    failures = []
    lock = threading.Lock()

    def attempt():
        try:
            reservation = tracker.reserve("shared", 30)
            time.sleep(0.01)
            tracker.reconcile(reservation, actual_tokens=30)
            with lock:
                successes.append(reservation)
        except BudgetExceededError:
            with lock:
                failures.append(1)

    with ThreadPoolExecutor(max_workers=10) as pool:
        list(pool.map(lambda _: attempt(), range(10)))

    # At most 3 concurrent 30-token reservations fit in a 100-token budget; the
    # atomic lock must prevent any overdraw regardless of thread interleaving.
    # Reconciled (spent) tokens stay deducted — this is a consumption budget,
    # not a lock that frees back up once a call finishes.
    assert len(successes) <= 3
    assert len(successes) + len(failures) == 10
    assert tracker.remaining("shared") == 100 - len(successes) * 30


# 11. Rate-limit and transient failure classification (+ retried)
def test_rate_limited_errors_are_classified_and_retried_then_succeed():
    provider = FakeProviderClient()
    provider.queue.append(RateLimitedError("slow down"))
    provider.queue.append(_success())
    gateway = ModelGateway(provider, _router(), retry_policy=RetryPolicy(max_retries=2, base_delay_seconds=0.01))
    response = gateway.generate(_request())
    assert response.status == ModelResultStatus.SUCCESS
    assert response.retries == 1
    assert len(provider.calls) == 2


# 12. Permanent error handling (not retried)
def test_permanent_errors_are_not_retried():
    provider = FakeProviderClient()
    provider.queue.append(AuthenticationFailedError("bad key"))
    gateway = ModelGateway(provider, _router(), retry_policy=RetryPolicy(max_retries=3, base_delay_seconds=0.01))
    response = gateway.generate(_request())
    assert response.status == ModelResultStatus.ERROR
    assert response.error_category == ModelErrorCategory.AUTHENTICATION_FAILED
    assert len(provider.calls) == 1  # no retry attempted


# 13. Bounded retries and timeout behavior
def test_retries_are_bounded_and_exhausting_them_surfaces_the_last_error():
    provider = FakeProviderClient()
    provider.queue.extend([RateLimitedError("1"), RateLimitedError("2"), RateLimitedError("3")])
    gateway = ModelGateway(provider, _router(), retry_policy=RetryPolicy(max_retries=2, base_delay_seconds=0.01))
    response = gateway.generate(_request())
    assert response.status == ModelResultStatus.ERROR
    assert response.error_category == ModelErrorCategory.RATE_LIMITED
    assert len(provider.calls) == 3  # initial attempt + 2 retries, never more
    assert response.retries == 2


# 14. Tenant-context integrity
def test_generation_is_denied_without_execute_tool_permission():
    provider = FakeProviderClient()
    gateway = ModelGateway(provider, _router())
    context = _context(permissions=frozenset({Permission.READ_GRAPH}))
    response = gateway.generate(_request(context=context))
    assert response.status == ModelResultStatus.ERROR
    assert response.error_category == ModelErrorCategory.AUTHORIZATION_DENIED
    assert provider.calls == []


def test_authorization_context_cannot_be_constructed_outside_principal_scope():
    principal = Principal(subject="agent", tenant_id="t1", workspaces=("w1",),
                          permissions=frozenset({Permission.EXECUTE_TOOL}))
    with pytest.raises(Exception):
        AuthorizationContext(principal=principal, tenant_id="other-tenant", workspace_id="w1", task_id="t",
                             permissions=frozenset({Permission.EXECUTE_TOOL}))


# 15. Secret redaction
def test_error_messages_redact_api_key_like_content():
    provider = FakeProviderClient()
    provider.queue.append(AuthenticationFailedError("rejected key=AIzaSyFAKEKEY1234567890ABCDEFGHIJ"))
    gateway = ModelGateway(provider, _router())
    response = gateway.generate(_request())
    assert response.status == ModelResultStatus.ERROR
    assert "AIzaSy" not in response.error_message
    assert "[redacted]" in response.error_message


# 16. Existing tools remain independent of the gateway
def test_existing_tool_modules_do_not_import_the_gateway():
    import ast
    from pathlib import Path

    tools_dir = Path(__file__).resolve().parents[2] / "backend" / "app" / "tools"
    for path in tools_dir.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                assert "gateway" not in node.module, f"{path.name} must not depend on the model gateway"
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert "gateway" not in alias.name, f"{path.name} must not depend on the model gateway"


# Output-size enforcement
def test_response_exceeding_output_byte_limit_is_rejected():
    provider = FakeProviderClient()
    provider.queue.append(_success(text="x" * 1000))
    gateway = ModelGateway(provider, _router())
    response = gateway.generate(_request(max_output_bytes=100))
    assert response.status == ModelResultStatus.ERROR
    assert response.error_category == ModelErrorCategory.RESPONSE_TOO_LARGE


# Safety blocking is reported distinctly, not as a generic failure
def test_safety_blocked_response_is_classified_distinctly():
    provider = FakeProviderClient()
    blocked = ProviderResponse(text=None, parsed=None,
                               usage=RawUsage(input_tokens=5, output_tokens=0, thinking_tokens=0, total_tokens=5),
                               model_version="fake-default-001", finish_reason="SAFETY", safety_blocked=True)
    provider.queue.append(blocked)
    gateway = ModelGateway(provider, _router())
    response = gateway.generate(_request())
    assert response.status == ModelResultStatus.ERROR
    assert response.error_category == ModelErrorCategory.SAFETY_BLOCKED
