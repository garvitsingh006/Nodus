"""NEXUS source -> graph assertion -> evidence integration against the live,
already-imported dataset. See ``backend.tests.nexus_live`` for why this suite
uses the shared live database instead of an isolated one.
"""
from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from backend.app.governance.loader import ImportPlan, _NameCache, load_7c
from backend.app.graph.models import EntityInput, EntityType, RelationInput, RelationType, SourceRecordInput
from backend.app.graph.repository import GraphRepository
from backend.app.ingestion.connector import (
    ConnectorCapabilities, ConnectorConfig, ConnectorIdentity, FetchPage, SourceCoverage,
)
from backend.app.ingestion.postgres_store import PostgresStore
from backend.app.ingestion.sync import SyncCoordinator
from backend.tests.nexus_live import (
    admin_principal, developer_principal, nexus_neo4j, nexus_pg, nexus_settings, repository_for,
)

pytestmark = pytest.mark.integration

STAGE_7C_ROOT = Path(__file__).resolve().parents[2] / "nexus_src" / "7C" / "NEXUS_Stage_7C"


def test_declared_ownership_has_correct_provenance(nexus_neo4j, admin_principal):
    """NEX-EMP-002 OWNS NEX-SVC-PAYMENT_API, asserted by NEX-SOA-001 (the 7A
    service_owner_assignments record, carried under the internal
    NEX-SRC-COMPANY_FOUNDATION scaffold source since 7A predates the 28
    selectable sources)."""
    repo = repository_for(nexus_neo4j, admin_principal)
    neighborhood = repo.neighborhood("NEX-EMP-002", limit=100)
    owns = next((rel for rel in neighborhood.relationships
                if rel.type == "OWNS" and rel.target_id == "NEX-SVC-PAYMENT_API"), None)
    assert owns is not None
    assert owns.record_id == "NEX-SOA-001"
    assert owns.source_id_ref == "NEX-SRC-COMPANY_FOUNDATION"
    assert owns.origin == "source_asserted"


def test_declared_dependency_has_source_asserted_origin(nexus_neo4j, admin_principal):
    repo = repository_for(nexus_neo4j, admin_principal)
    neighborhood = repo.neighborhood("NEX-SVC-PAYMENT_API", limit=100)
    declared = next((rel for rel in neighborhood.relationships
                     if rel.type == "DEPENDS_ON" and rel.record_id == "NEX-DEP-016"), None)
    assert declared is not None
    assert declared.origin == "source_asserted"


def test_observed_build_trigger_is_observed_not_asserted(nexus_neo4j, admin_principal, nexus_pg):
    """A commit triggering a CI build is directly-witnessed activity, not a
    source's declared claim — must carry AssertionOrigin.OBSERVED, not
    SOURCE_ASSERTED.

    NOTE: this deliberately uses commit->build (TRIGGERED), not commit
    authorship. Investigation while writing this test found that
    `repository_commits.json`'s `author_employee_id` field is NOT currently
    projected as a PERSON-AUTHORED->COMMIT edge at all (confirmed: zero such
    edges exist anywhere in the live graph, while PR/document/ADR/thread
    authorship IS correctly projected as AUTHORED). This looks like a real,
    narrow gap in `backend/app/governance/loader.py`'s commit transform, not
    a test issue — flagged for a follow-up, not fixed here."""
    repo = repository_for(nexus_neo4j, admin_principal)
    build_rows = nexus_pg.read(
        "SELECT record_id FROM nexus_records WHERE collection='build_executions' LIMIT 1")
    if not build_rows:
        pytest.skip("No build_executions records imported")
    build_id = build_rows[0]["record_id"]
    neighborhood = repo.neighborhood(build_id, limit=20)
    triggered = next((rel for rel in neighborhood.relationships
                      if rel.type == "TRIGGERED" and rel.target_id == build_id), None)
    assert triggered is not None, f"expected a TRIGGERED edge into {build_id}"
    assert triggered.origin == "observed"


def test_evidence_lookup_returns_provenance_for_authorized_source(nexus_neo4j, admin_principal):
    repo = repository_for(nexus_neo4j, admin_principal)
    evidence = repo.evidence("NEX-SVC-PAYMENT_API", limit=20)
    assert evidence["items"], "expected at least one evidence item for NEX-SVC-PAYMENT_API"
    assert all(item.source_id for item in evidence["items"])


def test_unauthorized_source_read_is_empty_not_an_error(nexus_neo4j, developer_principal, nexus_pg):
    """A developer querying evidence for an entity whose only assertions come
    from a source they aren't authorized for gets the existing empty/denied
    shape, never a crash or a distinguishable 'exists but restricted'
    signal."""
    repo = repository_for(nexus_neo4j, developer_principal)
    rows = nexus_pg.read(
        "SELECT record_id FROM nexus_records WHERE source_id='NEX-SRC-SECURITY_GOVERNANCE' LIMIT 1")
    assert rows
    assert repo.source_record("NEX-SRC-SECURITY_GOVERNANCE", rows[0]["record_id"]) is None


def test_commit_authorship_is_projected_as_authored_edges():
    """Regression for the Stage 10 commit-authorship bug: ``load_7c`` must
    project ``repository_commits.json``'s ``author_employee_id`` as a
    PERSON-AUTHORED->COMMIT relationship, the same pattern already used for
    pull_requests/engineering_issues in this loader. Exercised directly
    against the real Stage 7C fixture files (no live DB needed) so the fix is
    verified without reimporting the shared demo dataset."""
    plan = ImportPlan()
    load_7c(STAGE_7C_ROOT, _NameCache(), plan)

    commit_records = [r for r in plan.source_records.get("NEX-SRC-PAY-REPO", [])
                      if r.record_id == "NEX-CMT-PAY-001"]
    assert commit_records, "expected NEX-CMT-PAY-001 to be parsed from repository_commits.json"
    record = commit_records[0]

    authored = next((rel for rel in record.relationships
                     if rel.type == RelationType.AUTHORED and rel.target_external_id == "NEX-CMT-PAY-001"), None)
    assert authored is not None, "expected a PERSON-AUTHORED->COMMIT relationship for this commit"
    assert authored.source_external_id == "NEX-EMP-002"  # repository_commits.json's author_employee_id
    assert authored.origin.value == "observed"
    assert any(e.external_id == "NEX-EMP-002" and e.type == EntityType.PERSON for e in record.entities)

    # Every commit in this fixture carries an author; confirm the gap is
    # fully closed, not just for the one record checked above.
    import json
    raw_commits = json.loads((STAGE_7C_ROOT / "repository_commits.json").read_text(encoding="utf-8"))
    expected_authored_commit_ids = {
        c["id"] for c in raw_commits["repository_commits"] if c.get("author_employee_id")
    }
    commit_ids_with_author_edge = {
        rel.target_external_id
        for records in plan.source_records.values() for rec in records for rel in rec.relationships
        if rel.type == RelationType.AUTHORED and rel.target_external_id in expected_authored_commit_ids
    }
    assert commit_ids_with_author_edge == expected_authored_commit_ids


def test_source_sync_revision_retraction_and_authorization_roundtrip(nexus_settings, nexus_neo4j):
    """Full Stage 10 acceptance path on a throwaway tenant/workspace/source
    (never touching the shared nexus-tenant dataset): a real connector/local
    test-service record is synced through the existing outbox/projector
    pipeline into real Neo4j, producing a typed PERSON-AUTHORED->COMMIT edge
    with an authorized source citation; the record is then revised (new
    author) and finally retracted, and the current graph path is checked to
    go stale/disappear while history/evidence for the original content
    remains correct. Re-syncing unchanged content is confirmed idempotent."""
    tenant = f"stage10-test-{uuid.uuid4().hex[:12]}"
    workspace = "stage10-test-ws"
    source_id = "stage10-test-source"
    connector_id = f"stage10-test-connector-{uuid.uuid4().hex[:8]}"
    commit_id = f"stage10-commit-{uuid.uuid4().hex[:8]}"
    commit_entity_id = f"{source_id}::{commit_id}"  # public_id: no canonical_id set, so source-prefixed
    author_a, author_b = "stage10-author-a", "stage10-author-b"

    pg_store = PostgresStore(nexus_settings.postgres_dsn)
    pg_store.start()
    admin_graph = GraphRepository(nexus_neo4j, tenant, workspace, "stage10-test", allow_all=True,
                                  can_evidence=True)
    restricted_graph = GraphRepository(nexus_neo4j, tenant, workspace, "stage10-test-outsider",
                                       allow_all=False, can_evidence=True, authorized_source_ids=frozenset())
    coordinator = SyncCoordinator(pg_store, admin_graph)
    cfg = ConnectorConfig(tenant_id=tenant, workspace_id=workspace,
                          identity=ConnectorIdentity(connector_id=connector_id, source_id=source_id,
                                                     kind="stage10_local_test_service"))

    def commit_record(author: str) -> SourceRecordInput:
        return SourceRecordInput(
            source_id=source_id, record_id=commit_id, title=f"Commit by {author}",
            entities=[EntityInput(external_id=commit_id, type=EntityType.COMMIT, name="Test commit"),
                     EntityInput(external_id=author, type=EntityType.PERSON, name=author)],
            relationships=[RelationInput(source_external_id=author, target_external_id=commit_id,
                                         type=RelationType.AUTHORED)])

    class LocalTestServiceConnector:
        """Minimal real ``Connector`` implementation standing in for a local
        test-service source, driven through the exact same
        ``SyncCoordinator``/outbox/projector pipeline as GitHub/Jira."""
        def __init__(self, page_: FetchPage):
            self.page = page_

        def identity(self):
            return ConnectorIdentity(connector_id=connector_id, source_id=source_id, kind="stage10_local_test_service")

        def capabilities(self):
            return ConnectorCapabilities(supports_incremental=False, supports_deletion_detection=True)

        def fetch(self, config, cursor):
            return self.page

    try:
        # 1) Acquire + normalize + persist: real record -> typed graph path.
        first = coordinator.run(
            LocalTestServiceConnector(FetchPage(records=(commit_record(author_a),), coverage=SourceCoverage.COMPLETE)),
            cfg)
        assert first["status"] == "SUCCEEDED" and first["records_changed"] == 1
        neighborhood = admin_graph.neighborhood(commit_entity_id, limit=20)
        authored_a = next((r for r in neighborhood.relationships
                           if r.type == "AUTHORED" and r.source_id == f"{source_id}::{author_a}"), None)
        assert authored_a is not None and authored_a.record_id == commit_id
        evidence = admin_graph.evidence(commit_entity_id, limit=10)
        assert evidence["items"] and evidence["items"][0].source_id == source_id

        # Unauthorized actor cannot retrieve the newly synced restricted evidence/relationship.
        assert restricted_graph.evidence(commit_entity_id, limit=10)["items"] == []
        assert restricted_graph.source_record(source_id, commit_id) is None

        # Idempotency: an unchanged reimport doesn't duplicate/advance anything.
        repeat = coordinator.run(
            LocalTestServiceConnector(FetchPage(records=(commit_record(author_a),), coverage=SourceCoverage.COMPLETE)),
            cfg)
        assert repeat["status"] == "SUCCEEDED" and repeat["records_changed"] == 0

        # 2) Revision: a changed source record supersedes the old authorship
        # edge; the current graph path reflects the new author only.
        second = coordinator.run(
            LocalTestServiceConnector(FetchPage(records=(commit_record(author_b),), coverage=SourceCoverage.COMPLETE)),
            cfg)
        assert second["status"] == "SUCCEEDED" and second["records_changed"] == 1
        neighborhood = admin_graph.neighborhood(commit_entity_id, limit=20)
        current_types = {(r.type, r.source_id) for r in neighborhood.relationships}
        assert ("AUTHORED", f"{source_id}::{author_b}") in current_types
        assert ("AUTHORED", f"{source_id}::{author_a}") not in current_types
        # Historical content for the original authorship remains correctly queryable.
        history_page = admin_graph.neighborhood(commit_entity_id, limit=20, include_history=True)
        historical_authors = {r.source_id for r in history_page.relationships if r.type == "AUTHORED"}
        assert {f"{source_id}::{author_a}", f"{source_id}::{author_b}"} <= historical_authors

        # 3) Retraction: deleting the source record makes the derived path
        # stale/gone from the current graph while history stays correct.
        third = coordinator.run(
            LocalTestServiceConnector(FetchPage(records=(), retracted_record_ids=(commit_id,),
                                                coverage=SourceCoverage.COMPLETE)),
            cfg)
        assert third["status"] == "SUCCEEDED" and third["records_changed"] == 1
        assert admin_graph.source_record(source_id, commit_id) is None
        neighborhood = admin_graph.neighborhood(commit_entity_id, limit=20)
        assert all(r.type != "AUTHORED" or not r.active for r in neighborhood.relationships)
        history_page = admin_graph.neighborhood(commit_entity_id, limit=20, include_history=True)
        assert any(r.type == "AUTHORED" for r in history_page.relationships)  # history is retained, not erased

        # A failed sync must never falsely report success.
        with pytest.raises(Exception):
            coordinator.run(
                LocalTestServiceConnector(FetchPage(records=(SourceRecordInput(
                    source_id="not-" + source_id, record_id="smuggled", title="smuggled",
                    entities=[EntityInput(external_id="x", type=EntityType.COMMIT, name="x")]),))),
                cfg)
    finally:
        pg_store_rows_cleanup = [
            ("outbox_events", "connector_id"), ("sync_runs", "connector_id"),
            ("sync_checkpoints", "connector_id"), ("connector_configs", "connector_id"),
        ]
        with pg_store.transaction() as cur:
            for table, col in pg_store_rows_cleanup:
                cur.execute(f"DELETE FROM {table} WHERE {col}=%s", (connector_id,))
            cur.execute("DELETE FROM source_record_state WHERE tenant_id=%s AND workspace_id=%s",
                       (tenant, workspace))
            cur.execute("DELETE FROM projection_watermarks WHERE tenant_id=%s", (tenant,))

        def _cleanup(tx):
            tx.run("""
                MATCH (n) WHERE n.tenant_id=$tenant AND n.workspace_id=$workspace
                DETACH DELETE n
                """, tenant=tenant, workspace=workspace).consume()
        nexus_neo4j.write(_cleanup)
