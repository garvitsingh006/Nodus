"""Typed, versioned deterministic tool registry.

Tool definitions are plain Python objects (they hold types and a callable),
not wire contracts — ``ToolResult``/``ToolInvocation`` in ``contracts.py``
are the wire contracts. Registration is explicit; there is no plugin
discovery, dynamic import or dynamic code execution.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from pydantic import BaseModel

from backend.app.security import AuthorizationContext, Permission
from backend.app.tools.contracts import ExecutionLimits, SideEffect


class ToolRegistrationError(ValueError):
    pass


@dataclass(frozen=True)
class ToolDependencies:
    """Server-owned handles a tool handler may need. Never constructed from
    caller-supplied data; assembled once at process startup."""
    graph_store: object | None = None
    authority_rules: tuple = ()
    postgres_store: object | None = None
    governance_store: object | None = None


ToolHandler = Callable[[AuthorizationContext, BaseModel, ToolDependencies], object | None]


@dataclass(frozen=True)
class ToolSpec:
    tool_id: str
    version: str
    description: str
    input_model: type[BaseModel]
    output_model: type[BaseModel]
    handler: ToolHandler
    required_permissions: frozenset[Permission]
    side_effect: SideEffect = SideEffect.READ_ONLY
    limits: ExecutionLimits = field(default_factory=ExecutionLimits)
    requires_approval: bool = False
    capabilities: frozenset[str] = frozenset()
    dependencies: frozenset[str] = frozenset()

    def __post_init__(self):
        if not self.tool_id:
            raise ToolRegistrationError("tool_id is required")
        if not self.version:
            raise ToolRegistrationError("version is required")
        if not self.required_permissions:
            raise ToolRegistrationError("At least one required permission is required")


class ToolRegistry:
    """Explicit registration only. Unknown tools and duplicate/conflicting
    registrations are rejected."""

    def __init__(self) -> None:
        self._tools: dict[tuple[str, str], ToolSpec] = {}

    def register(self, spec: ToolSpec) -> None:
        key = (spec.tool_id, spec.version)
        if key in self._tools:
            raise ToolRegistrationError(
                f"Tool {spec.tool_id!r} version {spec.version!r} is already registered")
        self._tools[key] = spec

    def resolve(self, tool_id: str, version: str) -> ToolSpec | None:
        return self._tools.get((tool_id, version))

    def __contains__(self, key: tuple[str, str]) -> bool:
        return key in self._tools

    def tool_ids(self) -> frozenset[str]:
        """All registered tool IDs, independent of version — used by callers
        (e.g. skill compatibility checks) that need to distinguish an
        entirely unknown tool from a known tool at an unregistered version."""
        return frozenset(tool_id for tool_id, _version in self._tools)
