"""ND-12B integration coverage: engineering-issue blocker-chain resolution
and propagation against a real isolated Neo4j instance, through registry ->
executor -> security -> GraphRepository (same pattern as
test_pr_impact_tools.py / test_tool_graph_integration.py).

Real NEXUS data has zero BLOCKS relationships of any kind (confirmed: no
blocker/blocked_by field exists anywhere in nexus_src), so every chain
scenario here uses isolated, synthetic fixtures ingested directly via
GraphRepository rather than fabricating production source records, per the
Stage 12 plan's explicit guidance. The shared NEXUS demo graph is never
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
from backend.app.tools.executor import ToolExecutor
from backend.app.tools.registry import ToolDependencies, ToolRegistry
from backend.app.tools.work_blocker_tools import (
    WORK_BLOCKER_CHAIN_TOOL_ID, WORK_BLOCKER_CHAIN_VERSION, register_work_blocker_tools,
)

pytestmark = pytest.mark.integration


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
    container = f"gh-neo4j-blocktools-{suffix}"
    volume = f"gh-neo4j-blocktools-data-{suffix}"
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
    register_work_blocker_tools(reg)
    return reg


def _context(tenant: str, workspace: str, permissions=None, source_ids: tuple[str, ...] = (),
            subject: str = "reader") -> AuthorizationContext:
    permissions = permissions or frozenset({Permission.READ_GRAPH, Permission.READ_EVIDENCE})
    user = Principal(subject=subject, tenant_id=tenant, workspaces=(workspace,), permissions=permissions,
                     source_ids=source_ids)
    return AuthorizationContext(principal=user, tenant_id=tenant, workspace_id=workspace, task_id="t1",
                               permissions=permissions)


def _invoke(executor: ToolExecutor, arguments: dict, context: AuthorizationContext):
    return executor.execute(ToolInvocation(tool_id=WORK_BLOCKER_CHAIN_TOOL_ID, version=WORK_BLOCKER_CHAIN_VERSION,
                                           arguments=arguments, context=context))


def _issue(issue_id: str, status: str, team_id: str | None = None) -> EntityInput:
    return EntityInput(external_id=issue_id, canonical_id=issue_id, type="TASK", name=issue_id,
                       description=status)


def _seed_chain(repo: GraphRepository, *, source_id: str = "src", acl: list[str] | None = None) -> None:
    """issue-c (OPEN, team B) BLOCKS issue-b (OPEN, team A) BLOCKS issue-a
    (OPEN, team A) -- a two-hop upstream chain with one cross-team hop."""
    repo.ingest(SourceRecordInput(
        source_id=source_id, record_id="rec-issues", title="Issues", acl_subjects=acl or [],
        entities=[
            _issue("issue-a", "OPEN"), _issue("issue-b", "OPEN"), _issue("issue-c", "OPEN"),
            EntityInput(external_id="team-a", canonical_id="team-a", type="TEAM", name="Team A"),
            EntityInput(external_id="team-b", canonical_id="team-b", type="TEAM", name="Team B"),
        ],
        relationships=[
            RelationInput(source_external_id="issue-a", target_external_id="team-a", type=RelationType.ASSIGNED_TO),
            RelationInput(source_external_id="issue-b", target_external_id="team-a", type=RelationType.ASSIGNED_TO),
            RelationInput(source_external_id="issue-c", target_external_id="team-b", type=RelationType.ASSIGNED_TO),
        ],
    ))
    repo.ingest(SourceRecordInput(
        source_id=source_id, record_id="rec-block-1", title="Blocker 1", acl_subjects=acl or [],
        entities=[_issue("issue-b", "OPEN"), _issue("issue-a", "OPEN")],
        relationships=[RelationInput(source_external_id="issue-b", target_external_id="issue-a",
                                     type=RelationType.BLOCKS)],
    ))
    repo.ingest(SourceRecordInput(
        source_id=source_id, record_id="rec-block-2", title="Blocker 2", acl_subjects=acl or [],
        entities=[_issue("issue-c", "OPEN"), _issue("issue-b", "OPEN")],
        relationships=[RelationInput(source_external_id="issue-c", target_external_id="issue-b",
                                     type=RelationType.BLOCKS)],
    ))


# Scenario 1 (combined with scenario 2): real evidence-backed, correctly
# directed blocker chain, plus direction/status correctness -- a reversed
# BLOCKS edge never creates a false blocker, and a resolved upstream issue
# is reported as RESOLVED rather than an active obstacle.
def test_blocker_chain_resolves_direct_and_transitive_with_direction_and_status(store, registry):
    tenant, workspace = _name(), _name()
    repo = GraphRepository(store, tenant, workspace, "writer", allow_all=True, can_evidence=True)
    _seed_chain(repo)
    # Reversed edge: issue-a (wrongly) declares it blocks issue-d -- must
    # never surface as "issue-d is blocked by issue-a" from issue-a's own
    # blocker-chain query, nor pollute issue-a's own blocker list.
    repo.ingest(SourceRecordInput(
        source_id="src", record_id="rec-reversed", title="Reversed",
        entities=[_issue("issue-a", "OPEN"), _issue("issue-d", "OPEN")],
        relationships=[RelationInput(source_external_id="issue-a", target_external_id="issue-d",
                                     type=RelationType.BLOCKS)],
    ))
    executor = ToolExecutor(registry, ToolDependencies(graph_store=store))
    context = _context(tenant, workspace)

    result = _invoke(executor, {"issue_id": "issue-a", "max_depth": 3}, context)
    assert result.status == ToolStatus.SUCCESS
    assert result.data["direct_blocker_ids"] == ["issue-b"]

    findings_by_blocker = {f["blocking_issue"]: f for f in result.data["findings"]}
    assert findings_by_blocker["issue-b"]["status"] == "UNRESOLVED"
    assert findings_by_blocker["issue-b"]["chain_length"] == 1
    assert findings_by_blocker["issue-b"]["cross_team"] is False  # both team-a
    assert findings_by_blocker["issue-b"]["evidence_refs"]
    assert findings_by_blocker["issue-c"]["status"] == "UNRESOLVED"
    assert findings_by_blocker["issue-c"]["chain_length"] == 2
    assert findings_by_blocker["issue-c"]["cross_team"] is True  # team-b vs team-a
    assert findings_by_blocker["issue-c"]["chain_path"] == ["issue-c", "issue-b", "issue-a"]
    assert set(result.data["unresolved_blocker_ids"]) == {"issue-b", "issue-c"}
    assert "issue-d" not in findings_by_blocker  # reversed edge never surfaces as a blocker here

    # Now resolve issue-b: its own BLOCKS edge on issue-a is unchanged
    # (still structurally current), but it must stop being reported as an
    # active obstacle. Every record that mentions issue-b's description must
    # be kept consistent (exactly like the real loader, where a single
    # source of truth asserts an issue's status) -- a stale stub claim from
    # an unrelated record would otherwise read as a genuine conflicting
    # claim and correctly fall back to unresolved, which is not what this
    # scenario is testing.
    repo.ingest(SourceRecordInput(
        source_id="src", record_id="rec-issues", title="Issues",
        entities=[
            _issue("issue-a", "OPEN"), _issue("issue-b", "DONE"), _issue("issue-c", "OPEN"),
            EntityInput(external_id="team-a", canonical_id="team-a", type="TEAM", name="Team A"),
            EntityInput(external_id="team-b", canonical_id="team-b", type="TEAM", name="Team B"),
        ],
        relationships=[
            RelationInput(source_external_id="issue-a", target_external_id="team-a", type=RelationType.ASSIGNED_TO),
            RelationInput(source_external_id="issue-b", target_external_id="team-a", type=RelationType.ASSIGNED_TO),
            RelationInput(source_external_id="issue-c", target_external_id="team-b", type=RelationType.ASSIGNED_TO),
        ],
    ))
    repo.ingest(SourceRecordInput(
        source_id="src", record_id="rec-block-1", title="Blocker 1",
        entities=[_issue("issue-b", "DONE"), _issue("issue-a", "OPEN")],
        relationships=[RelationInput(source_external_id="issue-b", target_external_id="issue-a",
                                     type=RelationType.BLOCKS)],
    ))
    repo.ingest(SourceRecordInput(
        source_id="src", record_id="rec-block-2", title="Blocker 2",
        entities=[_issue("issue-c", "OPEN"), _issue("issue-b", "DONE")],
        relationships=[RelationInput(source_external_id="issue-c", target_external_id="issue-b",
                                     type=RelationType.BLOCKS)],
    ))
    updated = _invoke(executor, {"issue_id": "issue-a", "max_depth": 3}, context)
    updated_findings = {f["blocking_issue"]: f for f in updated.data["findings"]}
    assert updated_findings["issue-b"]["status"] == "RESOLVED"
    assert "issue-b" not in updated.data["unresolved_blocker_ids"]
    assert updated_findings["issue-c"]["status"] == "UNRESOLVED"  # chain continues past a resolved hop


# Scenario 3: authorization -- a restricted upstream blocker must not leak
# through paths, counts or findings for an unauthorized caller, and must
# become visible once authorized (same ACL mechanism as graph.* tools).
def test_blocker_chain_respects_source_authorization(store, registry):
    tenant, workspace = _name(), _name()
    repo = GraphRepository(store, tenant, workspace, "writer", allow_all=True, can_evidence=True)
    _seed_chain(repo, source_id="restricted-src", acl=["someone-else"])
    executor = ToolExecutor(registry, ToolDependencies(graph_store=store))

    unauthorized = _context(tenant, workspace)
    result = _invoke(executor, {"issue_id": "issue-a"}, unauthorized)
    assert result.status == ToolStatus.NOT_FOUND  # the issue itself is behind the ACL

    authorized = _context(tenant, workspace, source_ids=("restricted-src",), subject="someone-else")
    result = _invoke(executor, {"issue_id": "issue-a"}, authorized)
    assert result.status == ToolStatus.SUCCESS
    assert result.data["direct_blocker_ids"] == ["issue-b"]
    assert any(f["blocking_issue"] == "issue-c" for f in result.data["findings"])


# Scenario 4: revision and cycle handling -- retracting a blocker makes it
# disappear from current results (never retained as stale-current) while a
# cyclic BLOCKS chain terminates instead of looping forever.
def test_blocker_chain_revision_and_cycle_termination(store, registry):
    tenant, workspace = _name(), _name()
    repo = GraphRepository(store, tenant, workspace, "writer", allow_all=True, can_evidence=True)
    _seed_chain(repo)
    # Close the loop: issue-a BLOCKS issue-c (so a-> blocked by b -> blocked
    # by c -> blocked by a), which must terminate rather than looping.
    repo.ingest(SourceRecordInput(
        source_id="src", record_id="rec-cycle", title="Cycle",
        entities=[_issue("issue-a", "OPEN"), _issue("issue-c", "OPEN")],
        relationships=[RelationInput(source_external_id="issue-a", target_external_id="issue-c",
                                     type=RelationType.BLOCKS)],
    ))
    executor = ToolExecutor(registry, ToolDependencies(graph_store=store))
    context = _context(tenant, workspace)

    before = _invoke(executor, {"issue_id": "issue-a", "max_depth": 5}, context)
    assert before.status == ToolStatus.SUCCESS  # terminates rather than hanging/erroring
    assert {f["blocking_issue"] for f in before.data["findings"]} == {"issue-b", "issue-c"}

    retracted = repo.retract_record("src", "rec-block-1")
    assert retracted is True

    after = _invoke(executor, {"issue_id": "issue-a", "max_depth": 5}, context)
    assert after.status == ToolStatus.SUCCESS
    assert all(f["blocking_issue"] != "issue-b" for f in after.data["findings"])
    assert after.graph_revision > before.graph_revision
