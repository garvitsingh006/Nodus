"""Deterministic, configurable model-role routing. No LLM-based routing, no
automatic escalation, no hidden fallback to a different/more expensive
model — an unconfigured or unallowlisted model fails clearly instead."""
from __future__ import annotations

from dataclasses import dataclass, field

from backend.app.gateway.contracts import ModelRole


class UnknownModelRoleError(ValueError):
    pass


class ModelNotAllowlistedError(ValueError):
    pass


@dataclass(frozen=True)
class RoleRouter:
    role_models: dict[ModelRole, str]
    allowlist: frozenset[str] | None = None

    def __post_init__(self) -> None:
        if not self.role_models:
            raise ValueError("role_models mapping cannot be empty")
        if ModelRole.LIGHTWEIGHT not in self.role_models or ModelRole.DEFAULT not in self.role_models:
            raise ValueError("Lightweight and default roles must both be configured")
        allowlist = self.allowlist if self.allowlist is not None else frozenset(self.role_models.values())
        object.__setattr__(self, "allowlist", allowlist)
        unmapped = set(self.role_models.values()) - allowlist
        if unmapped:
            raise ValueError(f"Role mapping references models outside the allowlist: {sorted(unmapped)}")

    def resolve(self, role: ModelRole) -> str:
        """Preserves the actual selected model; raises rather than
        substituting a different model for an unknown role or an
        unallowlisted/unavailable one."""
        try:
            model = self.role_models[role]
        except KeyError:
            raise UnknownModelRoleError(f"No model configured for role {role!r}") from None
        if model not in self.allowlist:
            raise ModelNotAllowlistedError(f"Model {model!r} is not in the configured allowlist")
        return model
