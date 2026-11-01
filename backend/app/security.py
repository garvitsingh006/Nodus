"""Trusted authorization context for API and future tool execution."""
from __future__ import annotations

from enum import StrEnum
from pydantic import BaseModel, ConfigDict, Field, model_validator


class Permission(StrEnum):
    READ_GRAPH = "READ_GRAPH"
    READ_EVIDENCE = "READ_EVIDENCE"
    MANAGE_INGESTION = "MANAGE_INGESTION"
    WRITE_GRAPH = "WRITE_GRAPH"
    EXECUTE_TOOL = "EXECUTE_TOOL"
    APPROVE_ACTION = "APPROVE_ACTION"
    ADMIN = "ADMIN"


class Principal(BaseModel):
    model_config = ConfigDict(frozen=True)
    subject: str = Field(min_length=1)
    tenant_id: str = Field(min_length=1)
    workspaces: tuple[str, ...] = Field(min_length=1)
    permissions: frozenset[Permission] = Field(default_factory=lambda: frozenset({Permission.READ_GRAPH}))
    source_ids: tuple[str, ...] = ()

    @model_validator(mode="after")
    def valid_scope(self):
        if any(not value for value in (*self.workspaces, *self.source_ids)):
            raise ValueError("Scope identifiers cannot be empty")
        return self

    def permits(self, permission: Permission | str) -> bool:
        return Permission.ADMIN in self.permissions or Permission(permission) in self.permissions

    def permits_source(self, source_id: str) -> bool:
        return source_id in self.source_ids or Permission.ADMIN in self.permissions


class AuthorizationContext(BaseModel):
    """Created from a trusted principal and task record, never from model output."""
    model_config = ConfigDict(frozen=True)
    principal: Principal
    tenant_id: str = Field(min_length=1)
    workspace_id: str = Field(min_length=1)
    task_id: str = Field(min_length=1)
    permissions: frozenset[Permission] = Field(default_factory=lambda: frozenset({Permission.READ_GRAPH}))
    approved_permissions: frozenset[Permission] = Field(default_factory=frozenset)

    @model_validator(mode="after")
    def restricted(self):
        if self.tenant_id != self.principal.tenant_id or self.workspace_id not in self.principal.workspaces:
            raise ValueError("Authorization scope exceeds principal scope")
        if not all(self.principal.permits(permission) for permission in self.permissions):
            raise ValueError("Delegated permissions exceed principal permissions")
        if not self.approved_permissions <= self.permissions:
            raise ValueError("Approvals must be within delegated permissions")
        return self

    def delegate(self, permissions: frozenset[Permission]) -> "AuthorizationContext":
        if not permissions <= self.permissions:
            raise ValueError("Delegation cannot expand permissions")
        return self.model_copy(update={"permissions": permissions,
                                       "approved_permissions": self.approved_permissions & permissions})


class ToolPolicy(BaseModel):
    model_config = ConfigDict(frozen=True)
    name: str = Field(min_length=1)
    required_permission: Permission = Permission.READ_GRAPH
    requires_approval: bool = False

    def authorize(self, context: AuthorizationContext) -> None:
        if self.required_permission not in context.permissions:
            raise PermissionError("Tool permission denied")
        if self.requires_approval and self.required_permission not in context.approved_permissions:
            raise PermissionError("Tool approval required")
