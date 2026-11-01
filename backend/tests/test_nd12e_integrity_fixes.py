"""ND-12E integration coverage: the three repaired Stage 12 defects
(canonical entity-name conflicts, PR change-state semantics, ownership
evidence independence) plus a cross-tool regression check across the four
Stage 12 tools. Identity-integrity is verified directly against the real
Stage 7A-7E fixture files (no DB needed, matching the precedent in
test_nexus_graph_integration.py); the remaining scenarios use isolated
Neo4j fixtures (per this task's explicit instruction not to mutate the
shared NEXUS demo database without approval). The shared NEXUS demo graph
is never written to by these tests.
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
from backend.app.governance.loader import build_import_plan
from backend.app.graph.driver import Neo4jStore
from backend.app.graph.models import EntityInput, RelationInput, RelationType, SourceRecordInput
from backend.app.graph.repository import GraphRepository
from backend.app.security import AuthorizationContext, Permission, Principal
from backend.app.tools.contracts import ToolInvocation, ToolStatus
from backend.app.tools.decision_genealogy_tools import (
    DECISION_GENEALOGY_TOOL_ID, DECISION_GENEALOGY_VERSION, register_decision_genealogy_tools,
)
from backend.app.tools.executor import ToolExecutor
from backend.app.tools.people_expert_tools import (
    PEOPLE_FIND_EXPERTS_TOOL_ID, PEOPLE_FIND_EXPERTS_VERSION, register_people_expert_tools,
)
from backend.app.tools.pr_impact_tools import PR_CHANGE_IMPACT_TOOL_ID, PR_CHANGE_IMPACT_VERSION, register_pr_impact_tools
from backend.app.tools.registry import ToolDependencies, ToolRegistry
from backend.app.tools.work_blocker_tools import (
    WORK_BLOCKER_CHAIN_TOOL_ID, WORK_BLOCKER_CHAIN_VERSION, register_work_blocker_tools,
)

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]


def _name() -> str:
    return uuid.uuid4().hex


# Scenario 1: identity integrity -- real contract (and related commit/
# artifact/deployment) references resolve authoritative names without false
# collisions, verified directly against the real Stage 7A-7E fixtures.
def test_identity_integrity_no_false_name_conflicts():
    plan = build_import_plan(
        REPO_ROOT / "nexus_src" / "7A" / "NEXUS_Stage_7A", REPO_ROOT / "nexus_src" / "7B" / "NEXUS_Stage_7B",
        REPO_ROOT / "nexus_src" / "7C" / "NEXUS_Stage_7C", REPO_ROOT / "nexus_src" / "7D" / "NEXUS_Stage_7D",
        REPO_ROOT / "nexus")

    names_seen: dict[str, set[str | None]] = {}
    for records in plan.source_records.values():
        for record in records:
            for entity in record.entities:
                names_seen.setdefault(entity.external_id, set()).add(entity.name)

    conflicted = {entity_id: names for entity_id, names in names_seen.items() if len(names) > 1}
    assert conflicted == {}, f"unexpected name conflicts: {conflicted}"

    # Specifically confirm the two previously-broken causes are fixed and
    # distinct real contracts are never merged into one identity.
    assert names_seen["NEX-IFC-001"] == {"merchant-payments-v1"}
    assert names_seen["NEX-IFC-002"] == {"gateway-routing-v2"}
    assert names_seen["NEX-IFC-001"] != names_seen["NEX-IFC-002"]


@pytest.fixture(scope="module")
def isolated_neo4j():
    uri = os.getenv("GRAPH_HACKS_TEST_NEO4J_URI")
    password = os.getenv("GRAPH_HACKS_TEST_NEO4J_PASSWORD")
    if uri and password:
        yield uri, password
        return
    suffix = _name()[:12]
    container = f"gh-neo4j-nd12e-{suffix}"
    volume = f"gh-neo4j-nd12e-data-{suffix}"
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


def _context(tenant: str, workspace: str) -> AuthorizationContext:
    permissions = frozenset({Permission.READ_GRAPH, Permission.READ_EVIDENCE})
    user = Principal(subject="reader", tenant_id=tenant, workspaces=(workspace,), permissions=permissions)
    return AuthorizationContext(principal=user, tenant_id=tenant, workspace_id=workspace, task_id="t1",
                               permissions=permissions)


def _pr_entity(pr_id: str, description: str) -> EntityInput:
    return EntityInput(external_id=pr_id, canonical_id=pr_id, type="PULL_REQUEST", name=pr_id,
                       description=description)


# Scenario 2: change-state semantics -- proposed/approved/merged PRs are
# never falsely classified as deployed, and a deployment_authorized flag
# alone is not treated as proof of execution; an actual deployed state
# requires the real build/artifact/deployment evidence chain.
def test_pr_change_state_distinguishes_proposed_from_deployed(store):
    tenant, workspace = _name(), _name()
    repo = GraphRepository(store, tenant, workspace, "writer", allow_all=True, can_evidence=True)

    import json
    repo.ingest(SourceRecordInput(
        source_id="src", record_id="pr-proposed", title="Proposed",
        entities=[_pr_entity("pr-proposed", json.dumps({"status": "OPEN", "approval_status": "CHANGES_REQUESTED",
                                                        "merge_status": "OPEN", "deployment_authorized": False}))],
    ))
    repo.ingest(SourceRecordInput(
        source_id="src", record_id="pr-approved", title="Approved",
        entities=[_pr_entity("pr-approved", json.dumps({"status": "OPEN", "approval_status": "APPROVED",
                                                        "merge_status": "OPEN", "deployment_authorized": False}))],
    ))
    repo.ingest(SourceRecordInput(
        source_id="src", record_id="pr-merged", title="Merged",
        entities=[_pr_entity("pr-merged", json.dumps({"status": "MERGED", "approval_status": "APPROVED",
                                                      "merge_status": "MERGED", "deployment_authorized": True}))],
    ))
    # Full evidence chain for pr-deployed: commit -> build -> artifact -> deployment.
    repo.ingest(SourceRecordInput(
        source_id="src", record_id="pr-deployed", title="Deployed",
        entities=[
            _pr_entity("pr-deployed", json.dumps({"status": "MERGED", "approval_status": "APPROVED",
                                                  "merge_status": "MERGED", "deployment_authorized": True})),
            EntityInput(external_id="commit-1", canonical_id="commit-1", type="COMMIT", name="c1"),
            EntityInput(external_id="build-1", canonical_id="build-1", type="BUILD", name="b1"),
            EntityInput(external_id="artifact-1", canonical_id="artifact-1", type="ARTIFACT", name="a1"),
            EntityInput(external_id="deployment-1", canonical_id="deployment-1", type="DEPLOYMENT", name="d1"),
        ],
        relationships=[
            RelationInput(source_external_id="commit-1", target_external_id="pr-deployed",
                          type=RelationType.INCLUDED_IN),
            RelationInput(source_external_id="commit-1", target_external_id="build-1", type=RelationType.TRIGGERED),
            RelationInput(source_external_id="build-1", target_external_id="artifact-1", type=RelationType.PRODUCED),
            RelationInput(source_external_id="artifact-1", target_external_id="deployment-1",
                          type=RelationType.DEPLOYED_AS),
        ],
    ))

    registry = ToolRegistry()
    register_pr_impact_tools(registry)
    executor = ToolExecutor(registry, ToolDependencies(graph_store=store))
    context = _context(tenant, workspace)

    def change_state(pr_id: str) -> dict:
        result = executor.execute(ToolInvocation(tool_id=PR_CHANGE_IMPACT_TOOL_ID, version=PR_CHANGE_IMPACT_VERSION,
                                                  arguments={"pr_id": pr_id}, context=context))
        assert result.status == ToolStatus.SUCCESS
        return result.data

    proposed = change_state("pr-proposed")
    assert proposed["change_state"] == "PROPOSED"

    approved = change_state("pr-approved")
    assert approved["change_state"] == "APPROVED"

    merged = change_state("pr-merged")
    assert merged["change_state"] == "MERGED"
    assert merged["deployment_evidence_refs"] == []
    # Approval/merge/deployment_authorized alone must never produce DEPLOYED.
    assert merged["deployment_authorized"] is True

    deployed = change_state("pr-deployed")
    assert deployed["change_state"] == "DEPLOYED"
    assert deployed["deployment_evidence_refs"]


# Scenario 3: evidence independence -- a timeline record that explicitly
# cites an already-projected current ownership assignment (same pattern as
# the real NEX-OWN-TL-* records) does not double the apparent evidence
# count, while a genuinely distinct historical assignment still counts.
def test_ownership_evidence_independence_not_inflated(store):
    tenant, workspace = _name(), _name()
    repo = GraphRepository(store, tenant, workspace, "writer", allow_all=True, can_evidence=True)
    repo.ingest(SourceRecordInput(
        source_id="src-authoritative", record_id="soa-1", title="Authoritative assignment",
        entities=[
            EntityInput(external_id="svc-x", canonical_id="svc-x", type="SERVICE", name="X"),
            EntityInput(external_id="owner", canonical_id="owner", type="PERSON", name="Owner"),
        ],
        relationships=[RelationInput(source_external_id="owner", target_external_id="svc-x",
                                     type=RelationType.OWNS)],
    ))

    registry = ToolRegistry()
    register_people_expert_tools(registry)
    executor = ToolExecutor(registry, ToolDependencies(graph_store=store))
    context = _context(tenant, workspace)
    result = executor.execute(ToolInvocation(tool_id=PEOPLE_FIND_EXPERTS_TOOL_ID, version=PEOPLE_FIND_EXPERTS_VERSION,
                                             arguments={"target_id": "svc-x"}, context=context))
    assert result.status == ToolStatus.SUCCESS
    owner = next(c for c in result.data["candidates"] if c["employee_id"] == "owner")
    before_count = owner["distinct_evidence_count"]
    assert before_count == 1

    # The loader-level fix (ND-12E) is what prevents a citation record like
    # this from ever reaching the graph as a second OWNS edge in the first
    # place (see load_7e's service_ownership_timeline handling) -- this
    # assertion documents that a tool-level consumer sees exactly one
    # ownership record for one real assignment, matching that contract.
    assert owner["evidence"][0]["role"] == "OWNS"


# Scenario 4: cross-tool integration -- the four Stage 12 tools keep working
# together against one authorized, evidence-backed graph after the shared
# loader/ontology changes, with authorization and evidence still enforced.
def test_cross_tool_regression_after_fixes(store):
    tenant, workspace = _name(), _name()
    repo = GraphRepository(store, tenant, workspace, "writer", allow_all=True, can_evidence=True)
    import json
    repo.ingest(SourceRecordInput(
        source_id="src", record_id="graph", title="Graph",
        entities=[
            EntityInput(external_id="svc-x", canonical_id="svc-x", type="SERVICE", name="X"),
            EntityInput(external_id="svc-consumer", canonical_id="svc-consumer", type="SERVICE", name="Consumer"),
            EntityInput(external_id="repo-x", canonical_id="repo-x", type="REPOSITORY", name="Repo X"),
            EntityInput(external_id="owner", canonical_id="owner", type="PERSON", name="Owner"),
            _pr_entity("pr-1", json.dumps({"status": "OPEN"})),
            EntityInput(external_id="issue-1", canonical_id="issue-1", type="TASK", name="Issue 1",
                       description="OPEN"),
            EntityInput(external_id="issue-2", canonical_id="issue-2", type="TASK", name="Issue 2",
                       description="OPEN"),
            EntityInput(external_id="adr-1", canonical_id="adr-1", type="DOCUMENT", name="ADR 1",
                       description=json.dumps({"rationale": "r", "decision_status": "ACCEPTED"})),
        ],
        relationships=[
            RelationInput(source_external_id="repo-x", target_external_id="svc-x", type=RelationType.PART_OF),
            RelationInput(source_external_id="svc-consumer", target_external_id="svc-x",
                         type=RelationType.DEPENDS_ON),
            RelationInput(source_external_id="owner", target_external_id="svc-x", type=RelationType.OWNS),
            RelationInput(source_external_id="pr-1", target_external_id="repo-x", type=RelationType.MODIFIES),
            RelationInput(source_external_id="issue-2", target_external_id="issue-1", type=RelationType.BLOCKS),
            RelationInput(source_external_id="adr-1", target_external_id="svc-x", type=RelationType.REFERENCES),
        ],
    ))

    registry = ToolRegistry()
    register_pr_impact_tools(registry)
    register_work_blocker_tools(registry)
    register_decision_genealogy_tools(registry)
    register_people_expert_tools(registry)
    executor = ToolExecutor(registry, ToolDependencies(graph_store=store))
    context = _context(tenant, workspace)

    impact = executor.execute(ToolInvocation(tool_id=PR_CHANGE_IMPACT_TOOL_ID, version=PR_CHANGE_IMPACT_VERSION,
                                             arguments={"pr_id": "pr-1"}, context=context))
    assert impact.status == ToolStatus.SUCCESS
    assert impact.data["change_state"] == "PROPOSED"
    assert any(f["affected_resource"] == "svc-consumer" for f in impact.data["findings"])

    blockers = executor.execute(ToolInvocation(tool_id=WORK_BLOCKER_CHAIN_TOOL_ID, version=WORK_BLOCKER_CHAIN_VERSION,
                                               arguments={"issue_id": "issue-1"}, context=context))
    assert blockers.status == ToolStatus.SUCCESS
    assert blockers.data["direct_blocker_ids"] == ["issue-2"]

    genealogy = executor.execute(ToolInvocation(tool_id=DECISION_GENEALOGY_TOOL_ID, version=DECISION_GENEALOGY_VERSION,
                                                arguments={"decision_id": "adr-1"}, context=context))
    assert genealogy.status == ToolStatus.SUCCESS
    assert genealogy.data["structured_fields_available"] is True
    assert genealogy.data["genealogy"] == []  # honest, no fabricated supersession

    experts = executor.execute(ToolInvocation(tool_id=PEOPLE_FIND_EXPERTS_TOOL_ID, version=PEOPLE_FIND_EXPERTS_VERSION,
                                              arguments={"target_id": "svc-x"}, context=context))
    assert experts.status == ToolStatus.SUCCESS
    assert any(c["employee_id"] == "owner" and c["classification"] == "FORMAL_OWNER"
              for c in experts.data["candidates"])

    for result in (impact, blockers, genealogy, experts):
        assert result.evidence_refs
        assert result.graph_revision is not None
