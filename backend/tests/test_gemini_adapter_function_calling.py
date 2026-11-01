"""GH-007A: verifies ``GeminiProviderClient``'s function-calling extension
against the actually *installed* ``google-genai`` SDK (same pattern as
``test_gemini_adapter_compatibility.py``) — real types, the transport
monkeypatched so no network call is made. Skips if ``google-genai`` is not
installed.
"""
from __future__ import annotations

import pytest

genai = pytest.importorskip("google.genai", reason="google-genai is not installed in this environment")
from google.genai import types  # noqa: E402

from backend.app.gateway.contracts import FunctionDeclaration  # noqa: E402
from backend.app.gateway.gemini_client import GeminiProviderClient  # noqa: E402
from backend.app.gateway.provider import GenerationConfig  # noqa: E402


@pytest.fixture
def client() -> GeminiProviderClient:
    return GeminiProviderClient(api_key="dummy-key-for-offline-adapter-tests")


def test_tool_declarations_are_mapped_onto_real_sdk_types(client, monkeypatch):
    captured = {}

    def fake_generate_content(*, model, contents, config):
        captured["config"] = config
        return types.GenerateContentResponse(
            candidates=[types.Candidate(
                content=types.Content(parts=[types.Part(
                    function_call=types.FunctionCall(id="call-1", name="graph_entity_lookup",
                                                      args={"entity_id": "alice"}))], role="model"),
                finish_reason=types.FinishReason.STOP,
            )],
            usage_metadata=types.GenerateContentResponseUsageMetadata(
                prompt_token_count=10, candidates_token_count=5, total_token_count=15),
            model_version="gemini-3.8-flash-001",
        )

    monkeypatch.setattr(client._client.models, "generate_content", fake_generate_content)
    tool = FunctionDeclaration(name="graph_entity_lookup", description="Look up an entity",
                               parameters={"type": "object", "properties": {"entity_id": {"type": "string"}},
                                          "required": ["entity_id"]})
    config = GenerationConfig(max_output_tokens=64, temperature=None, response_schema=None,
                              system_instruction=None, timeout_seconds=10.0, tools=(tool,))
    response = client.generate("gemini-3.8-flash", "find alice", config)

    sent_tools = captured["config"].tools
    assert isinstance(sent_tools, list) and len(sent_tools) == 1
    declaration = sent_tools[0].function_declarations[0]
    assert declaration.name == "graph_entity_lookup"
    assert declaration.parameters_json_schema["properties"]["entity_id"]["type"] == "string"

    assert len(response.function_calls) == 1
    assert response.function_calls[0].name == "graph_entity_lookup"
    assert response.function_calls[0].arguments == {"entity_id": "alice"}
    assert response.function_calls[0].id == "call-1"


def test_structured_contents_list_is_passed_through_to_generate_content(client, monkeypatch):
    captured = {}

    def fake_generate_content(*, model, contents, config):
        captured["contents"] = contents
        return types.GenerateContentResponse(
            candidates=[types.Candidate(
                content=types.Content(parts=[types.Part(text="done")], role="model"),
                finish_reason=types.FinishReason.STOP,
            )],
            usage_metadata=types.GenerateContentResponseUsageMetadata(
                prompt_token_count=4, candidates_token_count=1, total_token_count=5),
            model_version="gemini-3.8-flash-001",
        )

    monkeypatch.setattr(client._client.models, "generate_content", fake_generate_content)
    contents = [
        {"role": "user", "parts": [{"text": "find alice"}]},
        {"role": "model", "parts": [{"function_call": {"name": "graph_entity_lookup", "args": {"entity_id": "alice"}}}]},
        {"role": "user", "parts": [{"function_response": {"name": "graph_entity_lookup", "response": {"ok": True}}}]},
    ]
    config = GenerationConfig(max_output_tokens=32, temperature=None, response_schema=None,
                              system_instruction=None, timeout_seconds=10.0)
    response = client.generate("gemini-3.8-flash", contents, config)
    assert captured["contents"] is contents  # passed through untouched; the real SDK accepts this shape
    assert response.text == "done"
