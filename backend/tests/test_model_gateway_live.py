"""Optional live Gemini smoke test. Requires explicit opt-in
(``GRAPH_HACKS_LIVE_GEMINI_TEST=1``) and a configured ``GEMINI_API_KEY``;
otherwise skips. Never run as part of the default/offline suite, and never
treated as proof the mocked provider tests pass in
``test_model_gateway.py`` reflect live behavior — this is the only test in
the suite that talks to the real API, and it does so once with a small
token budget."""
from __future__ import annotations

import os

import pytest
from pydantic import BaseModel

from backend.app.gateway.config import GatewayConfig
from backend.app.gateway.contracts import ModelRequest, ModelResultStatus, ModelRole
from backend.app.gateway.gateway import ModelGateway
from backend.app.gateway.gemini_client import GeminiProviderClient
from backend.app.security import AuthorizationContext, Permission, Principal


class Greeting(BaseModel):
    message: str


def _opted_in() -> bool:
    return os.getenv("GRAPH_HACKS_LIVE_GEMINI_TEST", "").strip() in {"1", "true", "yes"}


def test_gemini_sdk_import_and_constructor_are_safe_without_a_network_call():
    """GH-006C section 10: a safe, free SDK-readiness check. Client
    construction in the ``google-genai`` SDK does not itself make a network
    call, so this can run without credentials or an opt-in flag — it only
    verifies the package is importable and the adapter's constructor works,
    never that a live generation call succeeds."""
    try:
        from google import genai  # noqa: F401
    except ImportError:
        pytest.skip("google-genai is not installed; SDK import compatibility is unverified in this environment")

    client = GeminiProviderClient(api_key="dummy-key-for-constructor-check-only")
    assert client is not None


def test_live_gemini_smoke():
    if not _opted_in():
        pytest.skip("Live Gemini smoke test requires GRAPH_HACKS_LIVE_GEMINI_TEST=1")

    config = GatewayConfig.from_env()
    if not config.api_key:
        pytest.skip("GEMINI_API_KEY is not configured; cannot run the live smoke test")

    try:
        client = GeminiProviderClient(api_key=config.api_key)
    except RuntimeError as exc:
        pytest.fail(f"google-genai is required for the live smoke test: {exc}")

    gateway = ModelGateway(client, config.routing, pricing=config.pricing, retry_policy=config.retry_policy)
    principal = Principal(subject="live-smoke", tenant_id="smoke", workspaces=("smoke",),
                          permissions=frozenset({Permission.EXECUTE_TOOL}))
    context = AuthorizationContext(principal=principal, tenant_id="smoke", workspace_id="smoke",
                                   task_id="smoke-task", permissions=principal.permissions)
    request = ModelRequest(
        context=context, correlation_id="live-smoke-1", role=ModelRole.LIGHTWEIGHT,
        prompt="Reply with a one-sentence greeting as JSON.", output_schema=Greeting,
        max_output_tokens=64, max_total_tokens=256, timeout_seconds=30.0,
    )
    response = gateway.generate(request)

    if response.status != ModelResultStatus.SUCCESS:
        pytest.fail(f"Live Gemini call failed: {response.error_category} — {response.error_message}")

    assert response.actual_model is not None
    assert response.structured is not None
    Greeting.model_validate(response.structured)
    assert response.usage.input_tokens is not None
    assert response.usage.output_tokens is not None
