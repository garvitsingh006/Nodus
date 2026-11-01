"""Unit tests for the deterministic tool registry/executor (no I/O)."""
from __future__ import annotations

import pytest
from pydantic import BaseModel, Field, ValidationError

from backend.app.security import AuthorizationContext, Permission, Principal
from backend.app.tools.contracts import ToolInvocation, ToolStatus
from backend.app.tools.executor import ToolExecutor
from backend.app.tools.registry import (
    SideEffect, ToolDependencies, ToolRegistrationError, ToolRegistry, ToolSpec,
)


class EchoInput(BaseModel):
    value: str = Field(min_length=1, max_length=20)


class EchoOutput(BaseModel):
    value: str


def echo_handler(context, input_model, dependencies):
    return {"value": input_model.value}


def broken_output_handler(context, input_model, dependencies):
    return {"wrong_field": input_model.value}


def raises_permission_handler(context, input_model, dependencies):
    raise PermissionError("denied inside handler")


def not_found_handler(context, input_model, dependencies):
    return None


def crashes_handler(context, input_model, dependencies):
    raise RuntimeError("boom")


def echo_spec(*, tool_id="echo", version="1.0.0", required_permissions=None,
              side_effect=SideEffect.READ_ONLY, requires_approval=False, handler=echo_handler,
              output_model=EchoOutput, **limit_overrides):
    from backend.app.tools.contracts import ExecutionLimits
    return ToolSpec(
        tool_id=tool_id, version=version, description="Echo tool for tests",
        input_model=EchoInput, output_model=output_model, handler=handler,
        required_permissions=(frozenset({Permission.READ_GRAPH}) if required_permissions is None
                              else required_permissions),
        side_effect=side_effect, requires_approval=requires_approval,
        limits=ExecutionLimits(**limit_overrides) if limit_overrides else ExecutionLimits(),
    )


def principal(**overrides):
    defaults = dict(subject="user", tenant_id="tenant", workspaces=("space",),
                    permissions=frozenset({Permission.READ_GRAPH, Permission.EXECUTE_TOOL}))
    defaults.update(overrides)
    return Principal(**defaults)


def context(principal_obj=None, **overrides):
    principal_obj = principal_obj or principal()
    defaults = dict(principal=principal_obj, tenant_id=principal_obj.tenant_id,
                    workspace_id=principal_obj.workspaces[0], task_id="task-1",
                    permissions=principal_obj.permissions)
    defaults.update(overrides)
    return AuthorizationContext(**defaults)


def invocation(tool_id="echo", version="1.0.0", arguments=None, ctx=None, correlation_id=None):
    return ToolInvocation(tool_id=tool_id, version=version, arguments=arguments or {"value": "hi"},
                          context=ctx or context(), correlation_id=correlation_id)


# 1. Valid tool registration
def test_valid_tool_registration():
    registry = ToolRegistry()
    registry.register(echo_spec())
    assert registry.resolve("echo", "1.0.0") is not None


# 2. Duplicate registration rejection
def test_duplicate_registration_rejected():
    registry = ToolRegistry()
    registry.register(echo_spec())
    with pytest.raises(ToolRegistrationError):
        registry.register(echo_spec())


# 3. Unknown tool rejection
def test_unknown_tool_rejection():
    registry = ToolRegistry()
    executor = ToolExecutor(registry)
    result = executor.execute(invocation(tool_id="does-not-exist"))
    assert result.status == ToolStatus.NOT_FOUND
    assert result.data is None


def test_unsupported_schema_version_rejected():
    registry = ToolRegistry()
    registry.register(echo_spec())
    executor = ToolExecutor(registry)
    result = executor.execute(invocation(version="9.9.9"))
    assert result.status == ToolStatus.NOT_FOUND


# 4. Invalid input schema
def test_invalid_input_schema_rejected():
    registry = ToolRegistry()
    registry.register(echo_spec())
    executor = ToolExecutor(registry)
    result = executor.execute(invocation(arguments={"value": ""}))
    assert result.status == ToolStatus.INVALID_INPUT
    assert result.data is None
    assert result.error_category == "schema_validation_failed"


# 5. Invalid output schema
def test_invalid_output_schema_caught():
    registry = ToolRegistry()
    registry.register(echo_spec(handler=broken_output_handler))
    executor = ToolExecutor(registry)
    result = executor.execute(invocation())
    assert result.status == ToolStatus.EXECUTION_FAILED
    assert result.data is None
    assert result.error_category == "invalid_output_schema"


# 6. Permission denial before tool execution
def test_permission_denial_before_execution():
    registry = ToolRegistry()
    registry.register(echo_spec(required_permissions=frozenset({Permission.ADMIN})))
    executor = ToolExecutor(registry)
    caller = principal(permissions=frozenset({Permission.READ_GRAPH}))
    result = executor.execute(invocation(ctx=context(caller, permissions=frozenset({Permission.READ_GRAPH}))))
    assert result.status == ToolStatus.FORBIDDEN
    assert result.data is None
    assert result.error_category is None  # no internal detail leaked


# 7. Delegated permission narrowing
def test_delegated_permission_narrowing_enforced():
    registry = ToolRegistry()
    registry.register(echo_spec(required_permissions=frozenset({Permission.EXECUTE_TOOL})))
    executor = ToolExecutor(registry)
    owner = principal(permissions=frozenset({Permission.READ_GRAPH, Permission.EXECUTE_TOOL}))
    full_context = context(owner, permissions=owner.permissions)
    narrowed = full_context.delegate(frozenset({Permission.READ_GRAPH}))
    result = executor.execute(invocation(ctx=narrowed))
    assert result.status == ToolStatus.FORBIDDEN
    with pytest.raises(ValueError):
        full_context.delegate(frozenset({Permission.ADMIN}))


# 8. Caller-supplied tenant/principal spoofing rejection
def test_tenant_principal_spoofing_rejected_at_context_construction():
    attacker = principal(tenant_id="tenant-a", workspaces=("space",))
    with pytest.raises(ValidationError):
        AuthorizationContext(principal=attacker, tenant_id="tenant-b", workspace_id="space", task_id="t")


# 9. Mutation request rejection
def test_mutation_tool_rejected():
    registry = ToolRegistry()
    registry.register(echo_spec(side_effect=SideEffect.MUTATION))
    executor = ToolExecutor(registry)
    result = executor.execute(invocation())
    assert result.status == ToolStatus.FORBIDDEN
    assert result.data is None


# 10. Execution limits
def test_input_size_limit_enforced():
    registry = ToolRegistry()
    registry.register(echo_spec(max_input_bytes=5))
    executor = ToolExecutor(registry)
    result = executor.execute(invocation(arguments={"value": "this value is too long"}))
    assert result.status == ToolStatus.INVALID_INPUT
    assert result.error_category == "input_too_large"


def test_timeout_enforced():
    import time

    def slow_handler(context, input_model, dependencies):
        time.sleep(1)
        return {"value": input_model.value}

    registry = ToolRegistry()
    registry.register(echo_spec(handler=slow_handler, timeout_seconds=0.05))
    executor = ToolExecutor(registry)
    result = executor.execute(invocation())
    assert result.status == ToolStatus.UNAVAILABLE
    assert result.error_category == "execution_timeout"


# 11. Structured failure responses
def test_unhandled_exception_becomes_structured_execution_failed():
    registry = ToolRegistry()
    registry.register(echo_spec(handler=crashes_handler))
    executor = ToolExecutor(registry)
    result = executor.execute(invocation())
    assert result.status == ToolStatus.EXECUTION_FAILED
    assert result.data is None


def test_not_found_result_is_structured():
    registry = ToolRegistry()
    registry.register(echo_spec(handler=not_found_handler))
    executor = ToolExecutor(registry)
    result = executor.execute(invocation())
    assert result.status == ToolStatus.NOT_FOUND
    assert result.data is None


def test_handler_permission_error_becomes_forbidden():
    registry = ToolRegistry()
    registry.register(echo_spec(handler=raises_permission_handler))
    executor = ToolExecutor(registry)
    result = executor.execute(invocation())
    assert result.status == ToolStatus.FORBIDDEN


# 12. Restricted evidence not exposed through errors/results
def test_not_found_and_forbidden_carry_no_distinguishing_detail():
    registry = ToolRegistry()
    registry.register(echo_spec(handler=not_found_handler))
    registry.register(echo_spec(tool_id="echo-forbidden", required_permissions=frozenset({Permission.ADMIN})))
    executor = ToolExecutor(registry)
    not_found = executor.execute(invocation())
    forbidden = executor.execute(invocation(tool_id="echo-forbidden"))
    assert not_found.data is None and not_found.error_category is None
    assert forbidden.data is None and forbidden.error_category is None


def test_tool_spec_requires_at_least_one_permission():
    with pytest.raises(ToolRegistrationError):
        echo_spec(required_permissions=frozenset())
