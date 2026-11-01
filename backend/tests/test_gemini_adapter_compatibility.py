"""GH-006-FINAL G1: verifies ``GeminiProviderClient`` against the actually
*installed* ``google-genai`` SDK — real types, real config objects, real
error classes — with the transport (``Models.generate_content`` /
``count_tokens`` / ``list``) monkeypatched so no network call is made and no
real API key is required. Mocked ``ModelGateway`` tests
(``test_model_gateway.py``) only prove the gateway's own orchestration
logic; they say nothing about whether this adapter matches the SDK's actual
interfaces — that is what this file checks.

Skips (not "passes vacuously") if ``google-genai`` is not installed.
"""
from __future__ import annotations

import pytest

genai = pytest.importorskip("google.genai", reason="google-genai is not installed in this environment")
from google.genai import errors, types  # noqa: E402

from backend.app.gateway.gemini_client import GeminiProviderClient  # noqa: E402
from backend.app.gateway.provider import (  # noqa: E402
    AuthenticationFailedError, GenerationConfig, InvalidRequestProviderError, ModelUnavailableError,
    MissingCredentialError, ProviderUnavailableError, RateLimitedError,
)


@pytest.fixture
def client() -> GeminiProviderClient:
    return GeminiProviderClient(api_key="dummy-key-for-offline-adapter-tests")


def test_sdk_is_installed_and_importable():
    assert hasattr(genai, "Client")
    assert hasattr(genai, "__version__") or True  # version attr is best-effort across SDK releases


def test_missing_credential_rejected_before_touching_the_sdk():
    with pytest.raises(MissingCredentialError):
        GeminiProviderClient(api_key=None)


def test_client_construction_and_configuration(client):
    assert client._client is not None
    assert client._types is types


def test_count_tokens_interface_mapping(client, monkeypatch):
    captured = {}

    def fake_count_tokens(*, model, contents):
        captured["model"] = model
        captured["contents"] = contents
        return types.CountTokensResponse(total_tokens=42)

    monkeypatch.setattr(client._client.models, "count_tokens", fake_count_tokens)
    result = client.count_tokens("gemini-3.8-flash", "hello world")
    assert result == 42
    assert captured == {"model": "gemini-3.8-flash", "contents": "hello world"}


def test_generate_request_schema_and_structured_output_configuration(client, monkeypatch):
    captured = {}

    def fake_generate_content(*, model, contents, config):
        captured["model"] = model
        captured["contents"] = contents
        captured["config"] = config
        return types.GenerateContentResponse(
            candidates=[types.Candidate(
                content=types.Content(parts=[types.Part(text='{"ok": true}')], role="model"),
                finish_reason=types.FinishReason.STOP,
            )],
            usage_metadata=types.GenerateContentResponseUsageMetadata(
                prompt_token_count=11, candidates_token_count=7, thoughts_token_count=2, total_token_count=20,
            ),
            model_version="gemini-3.8-flash-001",
        )

    monkeypatch.setattr(client._client.models, "generate_content", fake_generate_content)

    gen_config = GenerationConfig(max_output_tokens=64, temperature=0.2, response_schema=dict,
                                  system_instruction="Be terse.", timeout_seconds=10.0)
    response = client.generate("gemini-3.8-flash", "say ok", gen_config)

    # Request schema compatibility: our config really is the SDK's own type.
    assert isinstance(captured["config"], types.GenerateContentConfig)
    assert captured["config"].max_output_tokens == 64
    assert captured["config"].temperature == 0.2
    assert captured["config"].system_instruction == "Be terse."
    assert captured["config"].response_mime_type == "application/json"
    assert captured["config"].response_schema is dict

    # Response/usage metadata mapping.
    assert response.model_version == "gemini-3.8-flash-001"
    assert response.usage.input_tokens == 11
    assert response.usage.output_tokens == 7
    assert response.usage.thinking_tokens == 2
    assert response.usage.total_tokens == 20
    assert response.finish_reason == "STOP"
    assert response.safety_blocked is False


def test_generate_without_structured_output_returns_plain_text(client, monkeypatch):
    def fake_generate_content(*, model, contents, config):
        assert config.response_schema is None
        return types.GenerateContentResponse(
            candidates=[types.Candidate(
                content=types.Content(parts=[types.Part(text="hello")], role="model"),
                finish_reason=types.FinishReason.STOP,
            )],
            usage_metadata=types.GenerateContentResponseUsageMetadata(
                prompt_token_count=3, candidates_token_count=1, total_token_count=4),
            model_version="gemini-3.5-flash-lite-001",
        )

    monkeypatch.setattr(client._client.models, "generate_content", fake_generate_content)
    gen_config = GenerationConfig(max_output_tokens=16, temperature=None, response_schema=None,
                                  system_instruction=None, timeout_seconds=5.0)
    response = client.generate("gemini-3.5-flash-lite", "hi", gen_config)
    assert response.text == "hello"
    assert response.parsed is None


def test_safety_blocked_finish_reason_is_detected(client, monkeypatch):
    def fake_generate_content(*, model, contents, config):
        return types.GenerateContentResponse(
            candidates=[types.Candidate(
                content=types.Content(parts=[], role="model"), finish_reason=types.FinishReason.SAFETY)],
            usage_metadata=types.GenerateContentResponseUsageMetadata(
                prompt_token_count=5, candidates_token_count=0, total_token_count=5),
            model_version="gemini-3.8-flash-001",
        )

    monkeypatch.setattr(client._client.models, "generate_content", fake_generate_content)
    gen_config = GenerationConfig(max_output_tokens=16, temperature=None, response_schema=None,
                                  system_instruction=None, timeout_seconds=5.0)
    response = client.generate("gemini-3.8-flash", "hi", gen_config)
    assert response.safety_blocked is True
    assert response.finish_reason == "SAFETY"


@pytest.mark.parametrize("status_code, expected_error", [
    (401, AuthenticationFailedError),
    (403, AuthenticationFailedError),
    (400, InvalidRequestProviderError),
    (404, ModelUnavailableError),
    (429, RateLimitedError),
    (503, ProviderUnavailableError),
])
def test_provider_error_status_mapping_uses_real_sdk_exception_types(client, monkeypatch, status_code,
                                                                      expected_error):
    error_cls = errors.ServerError if status_code >= 500 else errors.ClientError
    real_exception = error_cls(code=status_code, response_json={"error": {"message": "synthetic"}})

    def fake_generate_content(*, model, contents, config):
        raise real_exception

    monkeypatch.setattr(client._client.models, "generate_content", fake_generate_content)
    gen_config = GenerationConfig(max_output_tokens=16, temperature=None, response_schema=None,
                                  system_instruction=None, timeout_seconds=5.0)
    with pytest.raises(expected_error):
        client.generate("gemini-3.8-flash", "hi", gen_config)


def test_client_resource_cleanup(client, monkeypatch):
    closed = {"called": False}
    monkeypatch.setattr(client._client, "close", lambda: closed.__setitem__("called", True))
    client.close()
    assert closed["called"] is True
