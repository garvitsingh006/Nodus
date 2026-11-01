"""Deterministic source/scope authority resolution (R07).

This module only ranks which source's claim is authoritative for a given
scope — it never deletes, hides or relabels a contradictory claim (those
stay exactly as `GraphRepository._resolve`'s `conflicts` dict already
exposes them), never promotes an inferred claim into a source-asserted one,
never treats a later timestamp as proof of authority by itself, and never
touches tenant/source ACL. Authority is purely interpretive metadata layered
on top of facts that remain fully preserved and independently inspectable.

There is deliberately no single global ranking of sources: a rule only
grants precedence for one explicit (scope, entity/relationship type) pair,
so GitHub can be authoritative for one kind of claim and Jira for another
without either ever being "the" authoritative system.
"""
from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from backend.app.graph.models import AssertionOrigin, EntityType, RelationType

AUTHORITY_RULESET_VERSION = "1.0.0"


class AuthorityScope(StrEnum):
    TENANT = "tenant"
    WORKSPACE = "workspace"


class AuthorityRule(BaseModel):
    """One explicit, versioned precedence grant: within (scope[, workspace]),
    for claims about ``entity_type`` (entity-field conflicts) grant
    ``source_id`` precedence ``precedence`` over any other source asserting
    the same field, for claims whose origin is at least ``min_origin``, for
    as long as ``valid_from``/``valid_to`` hold. Never implies a permission
    grant and never ranks a source for a type/scope it has no rule for."""
    model_config = ConfigDict(frozen=True)
    rule_id: str = Field(min_length=1, max_length=200)
    scope: AuthorityScope
    tenant_id: str = Field(min_length=1)
    workspace_id: str | None = None
    entity_type: EntityType | None = None
    relationship_type: RelationType | None = None
    source_id: str = Field(min_length=1, max_length=200)
    precedence: int = Field(ge=0)
    min_origin: AssertionOrigin = AssertionOrigin.SOURCE_ASSERTED
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    ruleset_version: str = AUTHORITY_RULESET_VERSION

    @model_validator(mode="after")
    def scoped_correctly(self):
        if self.scope == AuthorityScope.WORKSPACE and not self.workspace_id:
            raise ValueError("A workspace-scoped authority rule requires workspace_id")
        if self.scope == AuthorityScope.TENANT and self.workspace_id:
            raise ValueError("A tenant-scoped authority rule must not set workspace_id")
        if self.entity_type is None and self.relationship_type is None:
            raise ValueError("An authority rule must target an entity_type or a relationship_type")
        if self.entity_type is not None and self.relationship_type is not None:
            raise ValueError("An authority rule targets exactly one of entity_type or relationship_type")
        if self.valid_from and self.valid_to and self.valid_to <= self.valid_from:
            raise ValueError("valid_to must be after valid_from")
        return self

    def applies_to(self, *, tenant_id: str, workspace_id: str, as_of: datetime) -> bool:
        if self.tenant_id != tenant_id:
            return False
        if self.scope == AuthorityScope.WORKSPACE and self.workspace_id != workspace_id:
            return False
        if self.valid_from and self.valid_from > as_of:
            return False
        if self.valid_to and self.valid_to <= as_of:
            return False
        return True


AuthorityStatus = Literal["authoritative", "unresolved", "no_rule"]


class AuthorityVerdict(BaseModel):
    model_config = ConfigDict(frozen=True)
    status: AuthorityStatus
    winning_source_id: str | None = None
    rule_id: str | None = None
    reason: str
    ruleset_version: str = AUTHORITY_RULESET_VERSION


def resolve_entity_field_authority(
    *, tenant_id: str, workspace_id: str, entity_type: EntityType | None,
    candidate_source_ids: frozenset[str], rules: tuple[AuthorityRule, ...],
    as_of: datetime | None = None,
) -> AuthorityVerdict:
    """Resolve which source (if any) is authoritative for a conflicting
    entity-field claim, among the sources that actually disagree here.
    Never guesses: ties and absent rules both come back as an explicit,
    inspectable non-answer rather than an invented winner."""
    as_of = as_of or datetime.now(timezone.utc)
    applicable = [
        rule for rule in rules
        if rule.entity_type == entity_type and rule.source_id in candidate_source_ids
        and rule.applies_to(tenant_id=tenant_id, workspace_id=workspace_id, as_of=as_of)
    ]
    if not applicable:
        return AuthorityVerdict(
            status="no_rule", reason="No configured authority rule applies to this scope and entity type; "
                                     "the conflict remains unresolved by design.")
    best = max(rule.precedence for rule in applicable)
    tied = [rule for rule in applicable if rule.precedence == best]
    if len(tied) > 1:
        sources = sorted({rule.source_id for rule in tied})
        return AuthorityVerdict(
            status="unresolved",
            reason=f"{len(tied)} rules tie at precedence {best} across sources {sources}; "
                   "authority is left unresolved rather than arbitrarily broken.")
    winner = tied[0]
    return AuthorityVerdict(
        status="authoritative", winning_source_id=winner.source_id, rule_id=winner.rule_id,
        reason=f"Rule {winner.rule_id!r} grants {winner.source_id!r} precedence {winner.precedence} "
               f"for this {winner.scope.value} scope and entity type.")
