"""Real Neo4j verification that authority is purely additive metadata:
conflicting claims stay fully preserved and inspectable regardless of
whether/how an authority rule resolves them."""
from __future__ import annotations

from backend.app.graph.authority import AuthorityRule, AuthorityScope
from backend.app.graph.models import EntityInput, EntityType, SourceRecordInput
from backend.tests.test_ingestion_sync import graph, isolated_neo4j  # noqa: F401


def record(source_id: str, record_id: str, name: str, canonical_id: str) -> SourceRecordInput:
    return SourceRecordInput(source_id=source_id, record_id=record_id, title=record_id,
                             entities=[EntityInput(external_id="svc", canonical_id=canonical_id,
                                                   type=EntityType.SERVICE, name=name)])


def test_authority_resolves_without_deleting_the_conflict(graph):
    canonical = f"service:{graph.workspace_id}"
    graph.ingest(record("github-acme", "r1", "Checkout (GitHub)", canonical))
    graph.ingest(record("catalog-acme", "r2", "Checkout (Catalog)", canonical))

    rule = AuthorityRule(rule_id="catalog-wins-service-name", scope=AuthorityScope.WORKSPACE,
                         tenant_id=graph.tenant_id, workspace_id=graph.workspace_id,
                         entity_type=EntityType.SERVICE, source_id="catalog-acme", precedence=10)
    with_rule = graph.__class__(graph.store, graph.tenant_id, graph.workspace_id, "writer",
                                allow_all=True, authority_rules=(rule,))
    resolved = with_rule.get_entity(canonical)
    assert resolved.conflicts["name"] == ["Checkout (Catalog)", "Checkout (GitHub)"]  # both still present
    assert resolved.authority["name"]["status"] == "authoritative"
    assert resolved.authority["name"]["winning_source_id"] == "catalog-acme"

    without_rule = graph.__class__(graph.store, graph.tenant_id, graph.workspace_id, "writer", allow_all=True)
    bare = without_rule.get_entity(canonical)
    assert bare.conflicts["name"] == resolved.conflicts["name"]  # same facts, no rule-dependent mutation
    assert bare.authority["name"]["status"] == "no_rule"  # no rules configured -> explicitly unresolved


def test_tied_authority_remains_unresolved_in_the_live_graph(graph):
    canonical = f"service:{graph.workspace_id}:tied"
    graph.ingest(record("github-acme", "r1", "Checkout (GitHub)", canonical))
    graph.ingest(record("catalog-acme", "r2", "Checkout (Catalog)", canonical))
    rules = (
        AuthorityRule(rule_id="a", scope=AuthorityScope.TENANT, tenant_id=graph.tenant_id,
                     entity_type=EntityType.SERVICE, source_id="github-acme", precedence=5),
        AuthorityRule(rule_id="b", scope=AuthorityScope.TENANT, tenant_id=graph.tenant_id,
                     entity_type=EntityType.SERVICE, source_id="catalog-acme", precedence=5),
    )
    repo = graph.__class__(graph.store, graph.tenant_id, graph.workspace_id, "writer",
                           allow_all=True, authority_rules=rules)
    entity = repo.get_entity(canonical)
    assert entity.conflicts["name"] == ["Checkout (Catalog)", "Checkout (GitHub)"]
    assert entity.authority["name"]["status"] == "unresolved"
    assert entity.authority["name"]["winning_source_id"] is None


def test_authority_never_appears_for_an_unconflicted_entity(graph):
    canonical = f"service:{graph.workspace_id}:agree"
    graph.ingest(record("github-acme", "r1", "Checkout", canonical))
    rule = AuthorityRule(rule_id="irrelevant", scope=AuthorityScope.TENANT, tenant_id=graph.tenant_id,
                         entity_type=EntityType.SERVICE, source_id="github-acme", precedence=1)
    repo = graph.__class__(graph.store, graph.tenant_id, graph.workspace_id, "writer",
                           allow_all=True, authority_rules=(rule,))
    entity = repo.get_entity(canonical)
    assert entity.conflicts == {} and entity.authority == {}
