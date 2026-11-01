"""Single shared deterministic tool execution interface.

No LLM is involved: tool selection (``tool_id``/``version``) is always
explicit, supplied by the caller before this executor runs.

Timeout note (verified, GH-005C): ``ExecutionLimits.timeout_seconds`` bounds
how long ``execute()`` *waits* for a handler via ``Future.result(timeout=...)``.
It does not, and cannot, forcibly kill a running Python thread — a handler
that ignores its deadline keeps running in the pool after this method
returns ``UNAVAILABLE`` to the caller (confirmed: a handler sleeping past its
timeout still completes afterward, on its own thread). This executor never
claims otherwise. Real cancellation for the tools in this library comes from
the *underlying* service's own enforced deadline: ``Neo4jStore`` runs every
query under a server-side transaction timeout (``read_timeout``/
``write_timeout``, enforced by Neo4j itself via ``neo4j.unit_of_work``), and
``PostgresStore`` bounds its connection attempts with ``connect_timeout``.
Each read-only tool's ``timeout_seconds`` is set comfortably above its
backing store's own timeout so this wrapper's bound is a safety margin that
fires only for a handler that is not itself making bounded calls, not a
replacement for the store-level deadline.
"""
from __future__ import annotations

import json
import uuid
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError

from pydantic import BaseModel, ValidationError

from backend.app.tools.contracts import ToolInvocation, ToolResult, ToolStatus
from backend.app.tools.registry import SideEffect, ToolDependencies, ToolRegistry, ToolSpec
from backend.app.security import ToolPolicy

_EXECUTOR_POOL = ThreadPoolExecutor(max_workers=32, thread_name_prefix="tool-exec")


def _result(invocation: ToolInvocation, status: ToolStatus, *, data: dict | None = None,
            error_category: str | None = None, evidence_refs: tuple[str, ...] = (),
            graph_revision: int | None = None, truncated: bool = False) -> ToolResult:
    return ToolResult(
        tool_id=invocation.tool_id, version=invocation.version, status=status,
        correlation_id=invocation.correlation_id or uuid.uuid4().hex,
        data=data, error_category=error_category, evidence_refs=evidence_refs,
        graph_revision=graph_revision, truncated=truncated,
    )


def _authorize(spec: ToolSpec, invocation: ToolInvocation) -> None:
    """Raises PermissionError on denial. Default-deny: a tool with no
    verified mutation policy path is always rejected, independent of the
    caller's permissions."""
    if spec.side_effect is SideEffect.MUTATION:
        raise PermissionError("Mutation-capable tools have no verified policy path yet")
    for permission in spec.required_permissions:
        ToolPolicy(name=spec.tool_id, required_permission=permission,
                   requires_approval=spec.requires_approval).authorize(invocation.context)


class ToolExecutor:
    """The only path through which a registered tool may run."""

    def __init__(self, registry: ToolRegistry, dependencies: ToolDependencies | None = None) -> None:
        self.registry = registry
        self.dependencies = dependencies or ToolDependencies()

    def execute(self, invocation: ToolInvocation) -> ToolResult:
        spec = self.registry.resolve(invocation.tool_id, invocation.version)
        if spec is None:
            return _result(invocation, ToolStatus.NOT_FOUND)

        try:
            _authorize(spec, invocation)
        except PermissionError:
            return _result(invocation, ToolStatus.FORBIDDEN)

        try:
            encoded = json.dumps(invocation.arguments, sort_keys=True, default=str)
        except (TypeError, ValueError):
            return _result(invocation, ToolStatus.INVALID_INPUT, error_category="unserializable_arguments")
        if len(encoded.encode("utf-8")) > spec.limits.max_input_bytes:
            return _result(invocation, ToolStatus.INVALID_INPUT, error_category="input_too_large")

        try:
            validated_input = spec.input_model(**invocation.arguments)
        except ValidationError:
            return _result(invocation, ToolStatus.INVALID_INPUT, error_category="schema_validation_failed")

        try:
            future = _EXECUTOR_POOL.submit(spec.handler, invocation.context, validated_input, self.dependencies)
            raw = future.result(timeout=spec.limits.timeout_seconds)
        except FutureTimeoutError:
            future.cancel()
            return _result(invocation, ToolStatus.UNAVAILABLE, error_category="execution_timeout")
        except PermissionError:
            return _result(invocation, ToolStatus.FORBIDDEN)
        except ValueError:
            return _result(invocation, ToolStatus.INVALID_INPUT, error_category="handler_rejected_input")
        except LookupError:
            return _result(invocation, ToolStatus.NOT_FOUND)
        except Exception:
            return _result(invocation, ToolStatus.EXECUTION_FAILED)

        if raw is None:
            return _result(invocation, ToolStatus.NOT_FOUND)

        payload = raw.model_dump(mode="json") if isinstance(raw, BaseModel) else raw
        graph_revision = payload.pop("graph_revision", None) if isinstance(payload, dict) else None
        evidence_refs = tuple(payload.pop("evidence_refs", ())) if isinstance(payload, dict) else ()
        truncated = bool(payload.pop("truncated", False)) if isinstance(payload, dict) else False

        try:
            validated_output = spec.output_model.model_validate(payload)
        except ValidationError:
            return _result(invocation, ToolStatus.EXECUTION_FAILED, error_category="invalid_output_schema")

        try:
            encoded_output = validated_output.model_dump_json()
        except (TypeError, ValueError):
            return _result(invocation, ToolStatus.EXECUTION_FAILED, error_category="unserializable_output")
        if len(encoded_output.encode("utf-8")) > spec.limits.max_output_bytes:
            return _result(invocation, ToolStatus.INCOMPLETE,
                           data=validated_output.model_dump(mode="json"), truncated=True,
                           graph_revision=graph_revision, evidence_refs=evidence_refs)

        status = ToolStatus.INCOMPLETE if truncated else ToolStatus.SUCCESS
        return _result(invocation, status, data=validated_output.model_dump(mode="json"),
                       graph_revision=graph_revision, evidence_refs=evidence_refs, truncated=truncated)
