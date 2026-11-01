from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from backend.app.graph.authority import AuthorityRule, AuthorityScope, resolve_entity_field_authority
from backend.app.graph.models import AssertionOrigin, EntityType, RelationType

NOW = datetime.now(timezone.utc)


def rule(**overrides) -> AuthorityRule:
    defaults = dict(rule_id="r1", scope=AuthorityScope.TENANT, tenant_id="t1",
                    entity_type=EntityType.SERVICE, source_id="catalog", precedence=10)
    defaults.update(overrides)
    return AuthorityRule(**defaults)


def test_rule_validation_enforces_exclusive_scope_and_target():
    with pytest.raises(ValidationError):
        AuthorityRule(rule_id="bad", scope=AuthorityScope.WORKSPACE, tenant_id="t1",
                     entity_type=EntityType.SERVICE, source_id="s", precedence=1)  # workspace scope needs workspace_id
    with pytest.raises(ValidationError):
        AuthorityRule(rule_id="bad", scope=AuthorityScope.TENANT, tenant_id="t1", workspace_id="w1",
                     entity_type=EntityType.SERVICE, source_id="s", precedence=1)  # tenant scope forbids workspace_id
    with pytest.raises(ValidationError):
        AuthorityRule(rule_id="bad", scope=AuthorityScope.TENANT, tenant_id="t1", source_id="s", precedence=1)
    with pytest.raises(ValidationError):
        AuthorityRule(rule_id="bad", scope=AuthorityScope.TENANT, tenant_id="t1",
                     entity_type=EntityType.SERVICE, relationship_type=RelationType.OWNS,
                     source_id="s", precedence=1)  # exactly one of entity_type/relationship_type
    with pytest.raises(ValidationError):
        rule(valid_from=NOW, valid_to=NOW - timedelta(days=1))


def test_no_configured_rule_is_explicitly_unresolved():
    verdict = resolve_entity_field_authority(
        tenant_id="t1", workspace_id="w1", entity_type=EntityType.SERVICE,
        candidate_source_ids=frozenset({"github", "jira"}), rules=())
    assert verdict.status == "no_rule" and verdict.winning_source_id is None


def test_rule_grants_precedence_only_among_actual_candidates():
    rules = (rule(source_id="catalog", precedence=10),)
    # catalog has a rule, but it isn't one of the sources actually in conflict here.
    verdict = resolve_entity_field_authority(
        tenant_id="t1", workspace_id="w1", entity_type=EntityType.SERVICE,
        candidate_source_ids=frozenset({"github", "jira"}), rules=rules)
    assert verdict.status == "no_rule"

    verdict2 = resolve_entity_field_authority(
        tenant_id="t1", workspace_id="w1", entity_type=EntityType.SERVICE,
        candidate_source_ids=frozenset({"github", "catalog"}), rules=rules)
    assert verdict2.status == "authoritative" and verdict2.winning_source_id == "catalog"


def test_tied_precedence_is_unresolved_never_guessed():
    rules = (rule(rule_id="r1", source_id="catalog", precedence=10),
             rule(rule_id="r2", source_id="github", precedence=10))
    verdict = resolve_entity_field_authority(
        tenant_id="t1", workspace_id="w1", entity_type=EntityType.SERVICE,
        candidate_source_ids=frozenset({"github", "catalog"}), rules=rules)
    assert verdict.status == "unresolved" and verdict.winning_source_id is None


def test_workspace_scoped_rule_does_not_leak_to_another_workspace():
    rules = (rule(scope=AuthorityScope.WORKSPACE, workspace_id="w1", source_id="catalog", precedence=5),)
    same_workspace = resolve_entity_field_authority(
        tenant_id="t1", workspace_id="w1", entity_type=EntityType.SERVICE,
        candidate_source_ids=frozenset({"catalog"}), rules=rules)
    other_workspace = resolve_entity_field_authority(
        tenant_id="t1", workspace_id="w2", entity_type=EntityType.SERVICE,
        candidate_source_ids=frozenset({"catalog"}), rules=rules)
    assert same_workspace.status == "authoritative"
    assert other_workspace.status == "no_rule"


def test_relationship_type_rule_never_applies_to_an_entity_field_query():
    rules = (rule(entity_type=None, relationship_type=RelationType.OWNS, source_id="catalog", precedence=5),)
    verdict = resolve_entity_field_authority(
        tenant_id="t1", workspace_id="w1", entity_type=EntityType.SERVICE,
        candidate_source_ids=frozenset({"catalog"}), rules=rules)
    assert verdict.status == "no_rule"


def test_temporal_validity_window_is_respected():
    expired = rule(valid_to=NOW - timedelta(days=1))
    future = rule(rule_id="future", valid_from=NOW + timedelta(days=1))
    current = rule(rule_id="current", valid_from=NOW - timedelta(days=1), valid_to=NOW + timedelta(days=1))
    verdict = resolve_entity_field_authority(
        tenant_id="t1", workspace_id="w1", entity_type=EntityType.SERVICE,
        candidate_source_ids=frozenset({"catalog"}), rules=(expired, future, current), as_of=NOW)
    assert verdict.status == "authoritative" and verdict.rule_id == "current"


def test_a_rule_targeting_the_wrong_entity_type_never_applies():
    rules = (rule(entity_type=EntityType.REPOSITORY),)
    verdict = resolve_entity_field_authority(
        tenant_id="t1", workspace_id="w1", entity_type=EntityType.SERVICE,
        candidate_source_ids=frozenset({"catalog"}), rules=rules)
    assert verdict.status == "no_rule"
