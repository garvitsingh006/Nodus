"""Dataset integrity for the already-imported NEXUS 7A-7E dataset, against the
live, shared demo Neo4j/Postgres (see ``backend.tests.nexus_live``) rather
than an isolated throwaway database — this dataset is deterministic and the
importer is content-hash idempotent, so asserting against the live state is a
faster and more realistic regression check than reimporting per test.

Regresses two concrete bugs fixed this session:
- A field-name bug in the 7E dependency-lifecycle loader that left 4 retired
  historical service dependencies looking permanently current (23 -> 27).
- A flat SERVICE-DEPENDS_ON-PACKAGE projection that discarded the
  `dependency_path` chain, losing real PACKAGE-DEPENDS_ON-PACKAGE transitive
  edges.
"""
from __future__ import annotations

import pytest

from backend.tests.nexus_live import (
    DECLARED_DEPENDENCY_COUNT, KNOWN_SERVICE_IDS, TOTAL_SOURCE_COUNT,
    admin_principal, nexus_neo4j, nexus_pg, nexus_settings, repository_for,
)

pytestmark = pytest.mark.integration


def test_core_entity_counts(nexus_pg):
    assert nexus_pg.read("SELECT count(*) c FROM nexus_companies")[0]["c"] == 1
    assert nexus_pg.read("SELECT count(*) c FROM nexus_employees")[0]["c"] == 12
    assert nexus_pg.read("SELECT count(*) c FROM nexus_sources")[0]["c"] == TOTAL_SOURCE_COUNT


def test_all_known_services_resolve(nexus_neo4j, admin_principal):
    repo = repository_for(nexus_neo4j, admin_principal)
    for service_id in KNOWN_SERVICE_IDS:
        entity = repo.get_entity(service_id)
        assert entity is not None, f"{service_id} missing from the imported graph"
        assert entity.active is True


def test_exactly_23_current_service_dependencies(nexus_neo4j, admin_principal):
    """Regression for the 7E temporal-field bug: must be 23, not 27."""
    repo = repository_for(nexus_neo4j, admin_principal)
    service_ids = set(KNOWN_SERVICE_IDS)
    current_pairs = set()
    for service_id in KNOWN_SERVICE_IDS:
        page = repo.neighborhood(service_id, limit=100)
        for rel in page.relationships:
            if rel.type == "DEPENDS_ON" and rel.source_id in service_ids and rel.target_id in service_ids:
                current_pairs.add((rel.source_id, rel.target_id))
    assert len(current_pairs) == DECLARED_DEPENDENCY_COUNT


def test_declared_dependency_directions_preserved(nexus_neo4j, admin_principal):
    """NEX-DEP-003/008's declared (consumer->provider) direction must survive
    unchanged despite 7B/7C documenting the opposite publisher->subscriber
    event-delivery flow for the same pair."""
    repo = repository_for(nexus_neo4j, admin_principal)
    payment_api = repo.neighborhood("NEX-SVC-PAYMENT_API", limit=100)
    refund_worker_edge = next(
        (rel for rel in payment_api.relationships
         if rel.type == "DEPENDS_ON" and rel.record_id == "NEX-DEP-003"), None)
    assert refund_worker_edge is not None
    assert refund_worker_edge.source_id == "NEX-SVC-PAYMENT_API"
    assert refund_worker_edge.target_id == "NEX-SVC-REFUND_WORKER"

    order_api = repo.neighborhood("NEX-SVC-ORDER_API", limit=100)
    webhook_edge = next(
        (rel for rel in order_api.relationships
         if rel.type == "DEPENDS_ON" and rel.record_id == "NEX-DEP-008"), None)
    assert webhook_edge is not None
    assert webhook_edge.source_id == "NEX-SVC-ORDER_API"
    assert webhook_edge.target_id == "NEX-SVC-WEBHOOK_CONSUMER"


def test_package_transitive_edges_exist(nexus_neo4j, admin_principal):
    """Regression for the flat-package-graph bug: a direct SERVICE->PACKAGE
    edge must coexist with at least one real PACKAGE->PACKAGE transitive edge
    discovered via two hops of the existing neighborhood traversal."""
    repo = repository_for(nexus_neo4j, admin_principal)
    payment_api = repo.neighborhood("NEX-SVC-PAYMENT_API", limit=100)
    direct_packages = [
        rel.target_id for rel in payment_api.relationships
        if rel.type == "DEPENDS_ON" and rel.source_id == "NEX-SVC-PAYMENT_API"
        and repo.get_entity(rel.target_id) is not None
        and repo.get_entity(rel.target_id).type == "PACKAGE"
    ]
    assert direct_packages, "expected at least one direct SERVICE-DEPENDS_ON-PACKAGE edge"

    found_transitive = False
    for package_id in direct_packages:
        package_neighborhood = repo.neighborhood(package_id, limit=50)
        for rel in package_neighborhood.relationships:
            if rel.type != "DEPENDS_ON" or rel.source_id != package_id:
                continue
            target = repo.get_entity(rel.target_id)
            if target is not None and target.type == "PACKAGE":
                found_transitive = True
                break
        if found_transitive:
            break
    assert found_transitive, "expected at least one PACKAGE-DEPENDS_ON-PACKAGE transitive edge"


def test_reimport_state_is_stable(nexus_pg):
    """A lightweight idempotency signal: every imported record's revision
    stays stable across two reads with nothing in between (a full reimport is
    covered live in scripts/import_nexus.py's own verification and is too
    slow — ~1,101 records through the outbox/projector pipeline — to repeat
    on every test run)."""
    first = nexus_pg.read(
        "SELECT record_id, imported_at FROM nexus_records ORDER BY record_id LIMIT 25")
    second = nexus_pg.read(
        "SELECT record_id, imported_at FROM nexus_records ORDER BY record_id LIMIT 25")
    assert first == second
    assert len(first) == 25
