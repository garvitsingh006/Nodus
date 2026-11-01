"""ND-12A integration coverage: PR change-impact resolution and propagation
against a real isolated Neo4j instance, through registry -> executor ->
security -> GraphRepository (same pattern as test_tool_graph_integration.py).
"""
from __future__ import annotations

import os
import secrets
import socket
import subprocess
import time
import uuid
from pathlib import Path

import pytest
from neo4j import GraphDatabase

from backend.app.config import Settings
from backend.app.governance.loader import ImportPlan, _NameCache, load_7c
from backend.app.graph.driver import Neo4jStore
from backend.app.graph.models import EntityInput, RelationInput, RelationType, SourceRecordInput
from backend.app.graph.repository import GraphRepository
from backend.app.security import AuthorizationContext, Permission, Principal
from backend.app.tools.contracts import ToolInvocation, ToolStatus
from backend.app.tools.pr_impact_tools import (
    PR_CHANGE_IMPACT_TOOL_ID, PR_CHANGE_IMPACT_VERSION, register_pr_impact_tools,
)
from backend.app.tools.executor import ToolExecutor
from backend.app.tools.registry import ToolDependencies, ToolRegistry

pytestmark = pytest.mark.integration

STAGE_7C_ROOT = Path(__file__).resolve().parents[2] / "nexus_src" / "7C" / "NEXUS_Stage_7C"


def _name() -> str:
    return uuid.uuid4().hex


@pytest.fixture(scope="module")
def isolated_neo4j():
    uri = os.getenv("GRAPH_HACKS_TEST_NEO4J_URI")
    password = os.getenv("GRAPH_HACKS_TEST_NEO4J_PASSWORD")
    if uri and password:
        yield uri, password
        return
    suffix = _name()[:12]
    container = f"gh-neo4j-prtools-{suffix}"
    volume = f"gh-neo4j-prtools-data-{suffix}"
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
    register_pr_impact_tools(reg)
    return reg


def _context(tenant: str, workspace: str, permissions=None, source_ids: tuple[str, ...] = ()) -> AuthorizationContext:
    permissions = permissions or frozenset({Permission.READ_GRAPH, Permission.READ_EVIDENCE})
    user = Principal(subject="reader", tenant_id=tenant, workspaces=(workspace,), permissions=permissions,
                     source_ids=source_ids)
    return AuthorizationContext(principal=user, tenant_id=tenant, workspace_id=workspace, task_id="t1",
                               permissions=permissions)


def _invoke(executor: ToolExecutor, arguments: dict, context: AuthorizationContext):
    return executor.execute(ToolInvocation(tool_id=PR_CHANGE_IMPACT_TOOL_ID, version=PR_CHANGE_IMPACT_VERSION,
                                           arguments=arguments, context=context))


def _seed_pr_change_scope(repo: GraphRepository, *, source_id: str = "src", acl: list[str] | None = None) -> None:
    """A minimal but realistic change-impact chain: PR modifies a repo that
    is part of SVC-CHANGED; SVC-CONSUMER depends directly on SVC-CHANGED;
    SVC-TRANSITIVE depends on SVC-CONSUMER (two hops upstream of the change).
    """
    repo.ingest(SourceRecordInput(
        source_id=source_id, record_id="rec-pr", title="PR",
        acl_subjects=acl or [],
        entities=[
            EntityInput(external_id="pr-1", canonical_id="pr-1", type="PULL_REQUEST", name="PR 1"),
            EntityInput(external_id="repo-1", canonical_id="repo-1", type="REPOSITORY", name="Repo 1"),
        ],
        relationships=[RelationInput(source_external_id="pr-1", target_external_id="repo-1",
                                     type=RelationType.MODIFIES)],
    ))
    repo.ingest(SourceRecordInput(
        source_id=source_id, record_id="rec-repo", title="Repo", acl_subjects=acl or [],
        entities=[
            EntityInput(external_id="repo-1", canonical_id="repo-1", type="REPOSITORY", name="Repo 1"),
            EntityInput(external_id="svc-changed", canonical_id="svc-changed", type="SERVICE", name="Changed"),
        ],
        relationships=[RelationInput(source_external_id="repo-1", target_external_id="svc-changed",
                                     type=RelationType.PART_OF)],
    ))
    repo.ingest(SourceRecordInput(
        source_id=source_id, record_id="rec-dep-1", title="Dependency 1", acl_subjects=acl or [],
        entities=[
            EntityInput(external_id="svc-consumer", canonical_id="svc-consumer", type="SERVICE", name="Consumer"),
            EntityInput(external_id="svc-changed", canonical_id="svc-changed", type="SERVICE", name="Changed"),
        ],
        relationships=[RelationInput(source_external_id="svc-consumer", target_external_id="svc-changed",
                                     type=RelationType.DEPENDS_ON)],
    ))
    repo.ingest(SourceRecordInput(
        source_id=source_id, record_id="rec-dep-2", title="Dependency 2", acl_subjects=acl or [],
        entities=[
            EntityInput(external_id="svc-transitive", canonical_id="svc-transitive", type="SERVICE",
                       name="Transitive"),
            EntityInput(external_id="svc-consumer", canonical_id="svc-consumer", type="SERVICE", name="Consumer"),
        ],
        relationships=[RelationInput(source_external_id="svc-transitive", target_external_id="svc-consumer",
                                     type=RelationType.DEPENDS_ON)],
    ))


# Scenario 1: real change-impact workflow -- PR resolves through changed
# resources and valid dependency paths to evidence-backed potentially
# affected consumers (direct and transitive).
def test_pr_change_impact_resolves_direct_and_transitive_consumers(store, registry):
    tenant, workspace = _name(), _name()
    repo = GraphRepository(store, tenant, workspace, "writer", allow_all=True, can_evidence=True)
    _seed_pr_change_scope(repo)
    executor = ToolExecutor(registry, ToolDependencies(graph_store=store))
    context = _context(tenant, workspace)

    result = _invoke(executor, {"pr_id": "pr-1", "max_depth": 2}, context)
    assert result.status == ToolStatus.SUCCESS

    changed = {item["entity_id"]: item for item in result.data["changed_resources"]}
    assert changed["svc-changed"]["via"] == "modifies_repository"

    findings_by_consumer = {f["affected_resource"]: f for f in result.data["findings"]}
    assert findings_by_consumer["svc-consumer"]["classification"] == "DIRECT_DEPENDENCY_POTENTIALLY_AFFECTED"
    assert findings_by_consumer["svc-consumer"]["evidence_refs"]
    assert findings_by_consumer["svc-consumer"]["relationship_path"] == ["svc-changed", "svc-consumer"]
    assert findings_by_consumer["svc-transitive"]["classification"] == "TRANSITIVE_DEPENDENCY_POTENTIALLY_AFFECTED"
    assert result.data["unmapped_changed_files"] == []
    assert result.graph_revision is not None
    assert result.evidence_refs  # evidence_refs surfaced at the ToolResult level too


# Scenario 2: direction and uncertainty -- a reversed dependency never
# produces a false impact finding, and a changed file that cannot be mapped
# to any service is reported as an explicit uncertainty, not silently
# dropped or guessed at.
def test_pr_change_impact_direction_and_unmapped_file_uncertainty(store, registry):
    tenant, workspace = _name(), _name()
    repo = GraphRepository(store, tenant, workspace, "writer", allow_all=True, can_evidence=True)
    repo.ingest(SourceRecordInput(
        source_id="src", record_id="rec-pr", title="PR",
        entities=[
            EntityInput(external_id="pr-2", canonical_id="pr-2", type="PULL_REQUEST", name="PR 2"),
            EntityInput(external_id="orphan-file.go", canonical_id="orphan-file.go", type="FILE",
                       name="orphan-file.go"),
        ],
        relationships=[RelationInput(source_external_id="pr-2", target_external_id="orphan-file.go",
                                     type=RelationType.MODIFIES)],
    ))
    repo.ingest(SourceRecordInput(
        source_id="src", record_id="rec-pr-service", title="PR affects service",
        entities=[
            EntityInput(external_id="pr-2", canonical_id="pr-2", type="PULL_REQUEST", name="PR 2"),
            EntityInput(external_id="svc-changed", canonical_id="svc-changed", type="SERVICE", name="Changed"),
        ],
        relationships=[RelationInput(source_external_id="pr-2", target_external_id="svc-changed",
                                     type=RelationType.REFERENCES)],
    ))
    # Reversed direction: svc-changed (wrongly) declares it depends on the
    # consumer -- this must NOT surface as "svc-consumer is affected".
    repo.ingest(SourceRecordInput(
        source_id="src", record_id="rec-reversed", title="Reversed dependency",
        entities=[
            EntityInput(external_id="svc-changed", canonical_id="svc-changed", type="SERVICE", name="Changed"),
            EntityInput(external_id="svc-consumer", canonical_id="svc-consumer", type="SERVICE", name="Consumer"),
        ],
        relationships=[RelationInput(source_external_id="svc-changed", target_external_id="svc-consumer",
                                     type=RelationType.DEPENDS_ON)],
    ))
    executor = ToolExecutor(registry, ToolDependencies(graph_store=store))
    result = _invoke(executor, {"pr_id": "pr-2"}, _context(tenant, workspace))

    assert result.status == ToolStatus.SUCCESS
    assert result.data["unmapped_changed_files"] == ["orphan-file.go"]
    assert result.data["findings"] == []  # the only DEPENDS_ON edge points the wrong way


# Scenario 3: authorization -- a downstream dependency asserted only by a
# source the caller is not authorized for/has not selected must not
# influence returned findings.
def test_pr_change_impact_respects_source_authorization(store, registry):
    tenant, workspace = _name(), _name()
    repo = GraphRepository(store, tenant, workspace, "writer", allow_all=True, can_evidence=True)
    _seed_pr_change_scope(repo, source_id="restricted-src", acl=["someone-else"])
    executor = ToolExecutor(registry, ToolDependencies(graph_store=store))

    unauthorized = _context(tenant, workspace)  # no acl match, no source selection
    result = _invoke(executor, {"pr_id": "pr-1"}, unauthorized)
    assert result.status == ToolStatus.NOT_FOUND  # the PR itself is behind the same ACL

    authorized = _context(tenant, workspace, source_ids=("restricted-src",))
    authorized = authorized.model_copy(update={
        "principal": authorized.principal.model_copy(update={"subject": "someone-else"})})
    result = _invoke(executor, {"pr_id": "pr-1"}, authorized)
    assert result.status == ToolStatus.SUCCESS
    assert any(f["affected_resource"] == "svc-consumer" for f in result.data["findings"])


# Scenario 4: revision -- removing the relevant dependency updates the
# finding on the next call instead of retaining a stale "current" impact.
def test_pr_change_impact_invalidates_stale_finding_on_revision(store, registry):
    tenant, workspace = _name(), _name()
    repo = GraphRepository(store, tenant, workspace, "writer", allow_all=True, can_evidence=True)
    _seed_pr_change_scope(repo)
    executor = ToolExecutor(registry, ToolDependencies(graph_store=store))
    context = _context(tenant, workspace)

    before = _invoke(executor, {"pr_id": "pr-1"}, context)
    assert any(f["affected_resource"] == "svc-consumer" for f in before.data["findings"])

    retracted = repo.retract_record("src", "rec-dep-1")
    assert retracted is True

    after = _invoke(executor, {"pr_id": "pr-1"}, context)
    assert all(f["affected_resource"] != "svc-consumer" for f in after.data["findings"])
    # the transitive consumer's own direct edge (svc-transitive -> svc-consumer)
    # is untouched, but it no longer connects back to the changed service at all
    assert all(f["affected_resource"] != "svc-transitive" for f in after.data["findings"])
    assert after.graph_revision > before.graph_revision


def test_loader_projects_pr_change_scope_from_real_fixture():
    """No live DB needed (same style as
    test_nexus_graph_integration.test_commit_authorship_is_projected_as_authored_edges):
    confirms ND-12A-1's data-layer fix directly against the real Stage 7C
    fixture -- NEX-PR-PAY-001's changed_file_paths/affected_service_ids/
    relevant_contract_ids/repository_id are now actually projected, closing
    the gap the Stage 12 plan requires ('Identify change scope ... using
    existing source records and graph relationships')."""
    plan = ImportPlan()
    load_7c(STAGE_7C_ROOT, _NameCache(), plan)

    pr_records = [r for r in plan.source_records.get("NEX-SRC-PAY-REPO", []) if r.record_id == "NEX-PR-PAY-001"]
    assert pr_records, "expected NEX-PR-PAY-001 to be parsed from pull_requests.json"
    record = pr_records[0]

    modifies_repo = next((rel for rel in record.relationships
                          if rel.type == RelationType.MODIFIES and rel.target_external_id == "NEX-REPO-PAY"), None)
    assert modifies_repo is not None and modifies_repo.source_external_id == "NEX-PR-PAY-001"

    modified_files = {rel.target_external_id for rel in record.relationships
                      if rel.type == RelationType.MODIFIES and rel.target_external_id != "NEX-REPO-PAY"}
    assert any(file_id.endswith("internal/gateway/route_selector.go") for file_id in modified_files)

    referenced = {rel.target_external_id for rel in record.relationships if rel.type == RelationType.REFERENCES}
    assert "NEX-SVC-GATEWAY_ROUTER" in referenced  # affected_service_ids
    assert "NEX-IFC-002" in referenced  # relevant_contract_ids
