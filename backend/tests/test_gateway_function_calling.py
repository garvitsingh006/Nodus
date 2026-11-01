"""GH-007A: offline unit coverage of the ModelGateway's function-calling
extension (tool declarations, model-generated function calls, multi-turn
history) — fully offline via the existing ``FakeProviderClient`` pattern
(``test_model_gateway.py``). Proves the extension is additive: a plain
``ModelRequest(prompt=...)`` with no ``tools``/``history`` behaves exactly as
before (covered by the unmodified ``test_model_gateway.py`` suite still
passing), and the new fields carry real budget/usage accounting through the
same single orchestration path.
"""
from __future__ import annotations

import pytest

from backend.app.gateway.budget import SharedTokenBudgetTracker
from backend.app.gateway.contracts import (
    ConversationRole, ConversationTurn, FunctionCall, FunctionDeclaration, FunctionResponse, ModelErrorCategory,
    ModelRequest, ModelResultStatus, ModelRole,
)
from backend.app.gateway.gateway import ModelGateway
from backend.app.gateway.provider import ProviderResponse, RawFunctionCall, RawUsage
from backend.app.gateway.routing import RoleRouter
from backend.app.security import AuthorizationContext, Permission, Principal


def _context() -> AuthorizationContext:
    principal = Principal(subject="agent", tenant_id="t1", workspaces=("w1",),
                          permissions=frozenset({Permission.EXECUTE_TOOL}))
    return AuthorizationContext(principal=principal, tenant_id="t1", workspace_id="w1", task_id="task-1",
                               permissions=principal.permissions)


def _router() -> RoleRouter:
    return RoleRouter(role_models={ModelRole.LIGHTWEIGHT: "fake-lite", ModelRole.DEFAULT: "fake-default"})


class FakeProviderClient:
    def __init__(self, input_tokens: int = 5):
        self.input_tokens = input_tokens
        self.calls: list = []
        self.queue: list = []

    def count_tokens(self, model, contents) -> int:
        return self.input_tokens

    def generate(self, model, contents, config) -> ProviderResponse:
        self.calls.append((model, contents, config))
        item = self.queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def available_models(self):
        return frozenset({"fake-lite", "fake-default"})


def _success(text=None, function_calls=(), input_tokens=5, output_tokens=3) -> ProviderResponse:
    return ProviderResponse(
        text=text, parsed=None,
        usage=RawUsage(input_tokens=input_tokens, output_tokens=output_tokens, thinking_tokens=0,
                       total_tokens=input_tokens + output_tokens),
        model_version="fake-default-001", finish_reason="STOP", safety_blocked=False,
        function_calls=function_calls,
    )


# 1. Plain prompt (no tools/history) is unaffected by the extension.
def test_plain_prompt_still_sends_a_bare_string_to_the_provider():
    provider = FakeProviderClient()
    provider.queue.append(_success(text="hi"))
    gateway = ModelGateway(provider, _router())
    request = ModelRequest(context=_context(), correlation_id="c1", role=ModelRole.DEFAULT, prompt="hello")
    response = gateway.generate(request)
    assert response.status == ModelResultStatus.SUCCESS
    model, contents, config = provider.calls[0]
    assert contents == "hello"  # byte-for-byte unchanged shape
    assert config.tools == ()


# 2. Tool declarations are forwarded to the provider.
def test_tool_declarations_are_forwarded_to_the_provider():
    provider = FakeProviderClient()
    provider.queue.append(_success(text="ok"))
    gateway = ModelGateway(provider, _router())
    tool = FunctionDeclaration(name="graph_entity_lookup", description="Look up an entity",
                               parameters={"type": "object", "properties": {"entity_id": {"type": "string"}}})
    request = ModelRequest(context=_context(), correlation_id="c1", role=ModelRole.DEFAULT,
                           prompt="find alice", tools=(tool,))
    response = gateway.generate(request)
    assert response.status == ModelResultStatus.SUCCESS
    _, contents, config = provider.calls[0]
    assert isinstance(contents, list)  # structured contents once tools/history are used
    assert config.tools == (tool,)


# 3. Model-generated function call round trips through ModelResponse.
def test_model_function_call_is_mapped_onto_model_response():
    provider = FakeProviderClient()
    provider.queue.append(_success(function_calls=(RawFunctionCall(id="call-1", name="graph_entity_lookup",
                                                                    arguments={"entity_id": "alice"}),)))
    gateway = ModelGateway(provider, _router())
    request = ModelRequest(context=_context(), correlation_id="c1", role=ModelRole.DEFAULT, prompt="find alice",
                           tools=(FunctionDeclaration(name="graph_entity_lookup"),))
    response = gateway.generate(request)
    assert response.status == ModelResultStatus.SUCCESS
    assert len(response.function_calls) == 1
    assert response.function_calls[0].name == "graph_entity_lookup"
    assert response.function_calls[0].arguments == {"entity_id": "alice"}


# 4. A function_response fed back as history produces the expected structured contents.
def test_function_response_history_is_assembled_into_structured_contents():
    provider = FakeProviderClient()
    provider.queue.append(_success(text="Alice is a PERSON entity."))
    gateway = ModelGateway(provider, _router())
    history = (
        ConversationTurn(role=ConversationRole.MODEL,
                         function_calls=(FunctionCall(id="call-1", name="graph_entity_lookup",
                                                      arguments={"entity_id": "alice"}),)),
        ConversationTurn(role=ConversationRole.USER,
                         function_responses=(FunctionResponse(id="call-1", name="graph_entity_lookup",
                                                              response={"status": "SUCCESS"}),)),
    )
    request = ModelRequest(context=_context(), correlation_id="c2", role=ModelRole.DEFAULT, prompt="",
                           history=history, tools=(FunctionDeclaration(name="graph_entity_lookup"),))
    response = gateway.generate(request)
    assert response.status == ModelResultStatus.SUCCESS
    _, contents, _ = provider.calls[0]
    assert contents[0]["role"] == "model"
    assert contents[0]["parts"][0]["function_call"]["name"] == "graph_entity_lookup"
    assert contents[1]["role"] == "user"
    assert contents[1]["parts"][0]["function_response"]["response"] == {"status": "SUCCESS"}


# 5. An empty request (no prompt, no history) is still rejected.
def test_empty_prompt_and_empty_history_is_rejected():
    with pytest.raises(Exception):
        ModelRequest(context=_context(), correlation_id="c1", role=ModelRole.DEFAULT, prompt="")


# 6. Shared budget enforcement still applies to a tool-calling request.
def test_shared_budget_still_enforced_for_tool_calling_requests():
    provider = FakeProviderClient(input_tokens=50)
    tracker = SharedTokenBudgetTracker()
    tracker.configure("tenant-1", limit=20)
    gateway = ModelGateway(provider, _router(), budget_tracker=tracker)
    request = ModelRequest(context=_context(), correlation_id="c1", role=ModelRole.DEFAULT, prompt="find alice",
                           tools=(FunctionDeclaration(name="graph_entity_lookup"),), budget_key="tenant-1",
                           max_output_tokens=50, max_total_tokens=200)
    response = gateway.generate(request)
    assert response.status == ModelResultStatus.ERROR
    assert response.error_category == ModelErrorCategory.BUDGET_EXHAUSTED
    assert provider.calls == []  # never reached the provider


# 7. A function-call-only response (no text) is a valid SUCCESS.
def test_function_call_only_response_has_no_text():
    provider = FakeProviderClient()
    provider.queue.append(_success(text=None, function_calls=(
        RawFunctionCall(id="1", name="skill_discover", arguments={}),)))
    gateway = ModelGateway(provider, _router())
    request = ModelRequest(context=_context(), correlation_id="c1", role=ModelRole.DEFAULT, prompt="start",
                           tools=(FunctionDeclaration(name="skill_discover"),))
    response = gateway.generate(request)
    assert response.status == ModelResultStatus.SUCCESS
    assert response.text is None
    assert response.function_calls[0].name == "skill_discover"


# 8. Oversized function-call arguments are rejected the same way an oversized text response is.
def test_oversized_function_call_arguments_are_rejected():
    provider = FakeProviderClient()
    provider.queue.append(_success(function_calls=(
        RawFunctionCall(id="1", name="graph_entity_lookup", arguments={"blob": "x" * 1000}),)))
    gateway = ModelGateway(provider, _router())
    request = ModelRequest(context=_context(), correlation_id="c1", role=ModelRole.DEFAULT, prompt="find",
                           tools=(FunctionDeclaration(name="graph_entity_lookup"),), max_output_bytes=100)
    response = gateway.generate(request)
    assert response.status == ModelResultStatus.ERROR
    assert response.error_category == ModelErrorCategory.RESPONSE_TOO_LARGE
