"""ND-12C integration coverage: architecture decision genealogy against both
the real, already-imported NEXUS dataset (rationale/alternatives/related
resources -- data that actually exists) and isolated synthetic fixtures
(explicit SUPERSEDES genealogy -- real NEXUS has none, confirmed by
exhaustive search across nexus_src/**/*.json; per the Stage 12 plan's
guidance, that scenario is covered with isolated fixtures instead of
fabricated production records). The shared NEXUS demo graph is never
written to by these tests.
"""
from __future__ import annotations

import os
import secrets
import socket
import subprocess
import time
import uuid

import pytest
from neo4j import GraphDatabase

from backend.app.config import Settings
from backend.app.graph.driver import Neo4jStore
from backend.app.graph.models import EntityInput, RelationInput, RelationType, SourceRecordInput
from backend.app.graph.repository import GraphRepository
from backend.app.security import AuthorizationContext, Permission, Principal
from backend.app.tools.contracts import ToolInvocation, ToolStatus
from backend.app.tools.decision_genealogy_tools import (
    DECISION_GENEALOGY_TOOL_ID, DECISION_GENEALOGY_VERSION, register_decision_genealogy_tools,
)
from backend.app.tools.executor import ToolExecutor
from backend.app.tools.registry import ToolDependencies, ToolRegistry
from backend.tests.nexus_live import NEXUS_WORKSPACE_ID, admin_principal, nexus_neo4j, nexus_pg, nexus_settings

pytestmark = pytest.mark.integration


def _name() -> str:
    return uuid.uuid4().hex


def _nexus_context(principal) -> AuthorizationContext:
    # ToolPolicy.authorize checks literal membership in context.permissions
    # (not principal.permits()'s ADMIN bypass), so the tool's two required
    # permissions must be explicitly delegated here -- `AuthorizationContext`
    # still validates this delegation never exceeds what the (admin)
    # principal actually permits.
    permissions = frozenset({Permission.READ_GRAPH, Permission.READ_EVIDENCE})
    return AuthorizationContext(principal=principal, tenant_id=principal.tenant_id, workspace_id=NEXUS_WORKSPACE_ID,
                               task_id="t1", permissions=permissions)


# Scenario 1: real NEXUS decision -- documented rationale, alternatives,
# evidence-backed related resources, and an honest empty genealogy (no real
# NEXUS architecture decision declares a SUPERSEDES relationship).
def test_decision_genealogy_resolves_real_nexus_decision(nexus_neo4j, admin_principal):
    registry = ToolRegistry()
    register_decision_genealogy_tools(registry)
    executor = ToolExecutor(registry, ToolDependencies(graph_store=nexus_neo4j))
    result = executor.execute(ToolInvocation(
        tool_id=DECISION_GENEALOGY_TOOL_ID, version=DECISION_GENEALOGY_VERSION,
        arguments={"decision_id": "NEX-ADR-001"}, context=_nexus_context(admin_principal)))

    assert result.status == ToolStatus.SUCCESS
    data = result.data
    assert data["structured_fields_available"] is True
    assert data["decision_status"] == "ACCEPTED"
    assert "deterministic" in data["rationale"].lower()
    assert any(alt["disposition"] == "SELECTED" for alt in data["alternatives_considered"])
    related_ids = {item["entity_id"] for item in data["related_resources"]}
    assert "NEX-SVC-PAYMENT_API" in related_ids
    assert all(item["evidence_refs"] for item in data["related_resources"])
    # Honest, not manufactured: real NEXUS has no documented supersession.
    assert data["genealogy"] == []
    assert result.evidence_refs


@pytest.fixture(scope="module")
def isolated_neo4j():
    uri = os.getenv("GRAPH_HACKS_TEST_NEO4J_URI")
    password = os.getenv("GRAPH_HACKS_TEST_NEO4J_PASSWORD")
    if uri and password:
        yield uri, password
        return
    suffix = _name()[:12]
    container = f"gh-neo4j-adrtools-{suffix}"
    volume = f"gh-neo4j-adrtools-data-{suffix}"
    password = secrets.token_urlsafe(24)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    uri = f"bolt://127.0.0.1:{port}"
    try:
        subprocess.run(["docker", "run", "-d", "--name", container,
                        "-p", f"127.0.0.1:{port}:7687",
                        "-e", f"NEO4J_AUTH=neo4j/{password}",
                        "-v", f"{volume}:/data", "neo4j:5.26.32-community"],
                       check=True, stdout=subprocess.DEVNULL)
        driver = GraphDatabase.driver(uri, auth=("neo4j", password))
        for _ in range(180):
            try:
                driver.verify_connectivity()
                break
            except Exception:
                time.sleep(1)
        else:
            pytest.fail("Isolated Neo4j did not become ready")
        driver.close()
        yield uri, password
    finally:
        subprocess.run(["docker", "rm", "-f", container],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(["docker", "volume", "rm", volume],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


@pytest.fixture
def store(isolated_neo4j):
    uri, password = isolated_neo4j
    settings = Settings(neo4j_uri=uri, neo4j_user="neo4j", neo4j_password=password)
    neo4j_store = Neo4jStore(settings)
    neo4j_store.start()
    try:
        yield neo4j_store
    finally:
        neo4j_store.close()


@pytest.fixture
def registry():
    reg = ToolRegistry()
    register_decision_genealogy_tools(reg)
    return reg


def _context(tenant: str, workspace: str, permissions=None, source_ids: tuple[str, ...] = (),
            subject: str = "reader") -> AuthorizationContext:
    permissions = permissions or frozenset({Permission.READ_GRAPH, Permission.READ_EVIDENCE})
    user = Principal(subject=subject, tenant_id=tenant, workspaces=(workspace,), permissions=permissions,
                     source_ids=source_ids)
    return AuthorizationContext(principal=user, tenant_id=tenant, workspace_id=workspace, task_id="t1",
                               permissions=permissions)


def _invoke(executor: ToolExecutor, arguments: dict, context: AuthorizationContext):
    return executor.execute(ToolInvocation(tool_id=DECISION_GENEALOGY_TOOL_ID, version=DECISION_GENEALOGY_VERSION,
                                           arguments=arguments, context=context))


def _decision(decision_id: str) -> EntityInput:
    return EntityInput(external_id=decision_id, canonical_id=decision_id, type="DOCUMENT", name=decision_id)


def _seed_genealogy(repo: GraphRepository, *, source_id: str = "src", acl: list[str] | None = None) -> None:
    """adr-new SUPERSEDES adr-old -- a correctly directed, single-hop
    genealogy link, plus an unrelated adr-other decision that must never
    appear in either side's genealogy."""
    repo.ingest(SourceRecordInput(
        source_id=source_id, record_id="rec-decisions", title="Decisions", acl_subjects=acl or [],
        entities=[_decision("adr-new"), _decision("adr-old"), _decision("adr-other")],
        relationships=[],
    ))
    repo.ingest(SourceRecordInput(
        source_id=source_id, record_id="rec-supersedes", title="Supersedes", acl_subjects=acl or [],
        entities=[_decision("adr-new"), _decision("adr-old")],
        relationships=[RelationInput(source_external_id="adr-new", target_external_id="adr-old",
                                     type=RelationType.SUPERSEDES)],
    ))


# Scenario 2: genealogy correctness -- an explicit SUPERSEDES link produces
# the correct predecessor/successor chronology on both ends, and an
# unrelated decision never appears as a false genealogy link; combined with
# authorization -- a restricted genealogy link cannot leak.
def test_decision_genealogy_correct_direction_and_authorization(store, registry):
    tenant, workspace = _name(), _name()
    repo = GraphRepository(store, tenant, workspace, "writer", allow_all=True, can_evidence=True)
    _seed_genealogy(repo)
    executor = ToolExecutor(registry, ToolDependencies(graph_store=store))
    context = _context(tenant, workspace)

    new_result = _invoke(executor, {"decision_id": "adr-new"}, context)
    assert new_result.status == ToolStatus.SUCCESS
    new_links = {link["decision_id"]: link for link in new_result.data["genealogy"]}
    assert new_links["adr-old"]["relation"] == "PREDECESSOR"
    assert new_links["adr-old"]["status"] == "CURRENT"
    assert "adr-other" not in new_links

    old_result = _invoke(executor, {"decision_id": "adr-old"}, context)
    old_links = {link["decision_id"]: link for link in old_result.data["genealogy"]}
    assert old_links["adr-new"]["relation"] == "SUCCESSOR"
    assert "adr-other" not in old_links

    other_result = _invoke(executor, {"decision_id": "adr-other"}, context)
    assert other_result.data["genealogy"] == []  # accurate single-decision history, nothing manufactured

    # Authorization: the same genealogy, restricted, must not leak.
    tenant2, workspace2 = _name(), _name()
    restricted_repo = GraphRepository(store, tenant2, workspace2, "writer", allow_all=True, can_evidence=True)
    _seed_genealogy(restricted_repo, source_id="restricted-src", acl=["someone-else"])
    unauthorized = _context(tenant2, workspace2)
    blocked = _invoke(executor, {"decision_id": "adr-new"}, unauthorized)
    assert blocked.status == ToolStatus.NOT_FOUND

    authorized = _context(tenant2, workspace2, source_ids=("restricted-src",), subject="someone-else")
    allowed = _invoke(executor, {"decision_id": "adr-new"}, authorized)
    assert allowed.status == ToolStatus.SUCCESS
    assert any(link["decision_id"] == "adr-old" for link in allowed.data["genealogy"])


# Scenario 3: revision -- retracting the SUPERSEDES assertion removes it
# from the current genealogy (never retained as stale-current), and
# repeated evaluation of an unchanged graph never duplicates findings.
def test_decision_genealogy_revision_and_no_duplication(store, registry):
    tenant, workspace = _name(), _name()
    repo = GraphRepository(store, tenant, workspace, "writer", allow_all=True, can_evidence=True)
    _seed_genealogy(repo)
    executor = ToolExecutor(registry, ToolDependencies(graph_store=store))
    context = _context(tenant, workspace)

    first = _invoke(executor, {"decision_id": "adr-new"}, context)
    second = _invoke(executor, {"decision_id": "adr-new"}, context)
    assert len(first.data["genealogy"]) == len(second.data["genealogy"]) == 1  # no duplication on repeat

    retracted = repo.retract_record("src", "rec-supersedes")
    assert retracted is True

    after = _invoke(executor, {"decision_id": "adr-new"}, context)
    assert after.status == ToolStatus.SUCCESS
    # Historical traceability is preserved (the link is still reported), but
    # it must no longer read as a current predecessor.
    after_links = {link["decision_id"]: link for link in after.data["genealogy"]}
    assert after_links["adr-old"]["status"] == "HISTORICAL"
    assert after.graph_revision > first.graph_revision
