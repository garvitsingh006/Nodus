"""ND-12D integration coverage: engineering expert finder against both the
real, already-imported NEXUS dataset (formal owners/contributors with real
evidence) and isolated synthetic fixtures (ownership distinction, evidence
deduplication/retraction, authorization). The shared NEXUS demo graph is
never written to by these tests.
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
from backend.app.tools.people_expert_tools import (
    PEOPLE_FIND_EXPERTS_TOOL_ID, PEOPLE_FIND_EXPERTS_VERSION, register_people_expert_tools,
)
from backend.app.tools.registry import ToolDependencies, ToolRegistry
from backend.tests.nexus_live import NEXUS_WORKSPACE_ID, admin_principal, nexus_neo4j, nexus_pg, nexus_settings

pytestmark = pytest.mark.integration


def _name() -> str:
    return uuid.uuid4().hex


def _nexus_context(principal) -> AuthorizationContext:
    permissions = frozenset({Permission.READ_GRAPH, Permission.READ_EVIDENCE})
    return AuthorizationContext(principal=principal, tenant_id=principal.tenant_id, workspace_id=NEXUS_WORKSPACE_ID,
                               task_id="t1", permissions=permissions)


# Scenario 1: real NEXUS workflow -- a real service's actual, evidence-backed
# contributors resolve correctly, with formal owners distinguished from
# other contributors by real OWNS records, not inferred from activity alone.
def test_find_experts_resolves_real_nexus_service(nexus_neo4j, admin_principal):
    registry = ToolRegistry()
    register_people_expert_tools(registry)
    executor = ToolExecutor(registry, ToolDependencies(graph_store=nexus_neo4j))
    result = executor.execute(ToolInvocation(
        tool_id=PEOPLE_FIND_EXPERTS_TOOL_ID, version=PEOPLE_FIND_EXPERTS_VERSION,
        arguments={"target_id": "NEX-SVC-PAYMENT_API"}, context=_nexus_context(admin_principal)))

    assert result.status == ToolStatus.SUCCESS
    data = result.data
    assert data["insufficient_evidence"] is False
    by_employee = {c["employee_id"]: c for c in data["candidates"]}
    assert by_employee["NEX-EMP-002"]["classification"] == "FORMAL_OWNER"
    assert any(item["role"] == "OWNS" for item in by_employee["NEX-EMP-002"]["evidence"])
    assert any(item["evidence_refs"] for item in by_employee["NEX-EMP-002"]["evidence"])
    assert result.evidence_refs


@pytest.fixture(scope="module")
def isolated_neo4j():
    uri = os.getenv("GRAPH_HACKS_TEST_NEO4J_URI")
    password = os.getenv("GRAPH_HACKS_TEST_NEO4J_PASSWORD")
    if uri and password:
        yield uri, password
        return
    suffix = _name()[:12]
    container = f"gh-neo4j-experttools-{suffix}"
    volume = f"gh-neo4j-experttools-data-{suffix}"
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
    register_people_expert_tools(reg)
    return reg


def _context(tenant: str, workspace: str, permissions=None, source_ids: tuple[str, ...] = (),
            subject: str = "reader") -> AuthorizationContext:
    permissions = permissions or frozenset({Permission.READ_GRAPH, Permission.READ_EVIDENCE})
    user = Principal(subject=subject, tenant_id=tenant, workspaces=(workspace,), permissions=permissions,
                     source_ids=source_ids)
    return AuthorizationContext(principal=user, tenant_id=tenant, workspace_id=workspace, task_id="t1",
                               permissions=permissions)


def _invoke(executor: ToolExecutor, arguments: dict, context: AuthorizationContext):
    return executor.execute(ToolInvocation(tool_id=PEOPLE_FIND_EXPERTS_TOOL_ID, version=PEOPLE_FIND_EXPERTS_VERSION,
                                           arguments=arguments, context=context))


def _seed(repo: GraphRepository, *, source_id: str = "src", acl: list[str] | None = None) -> None:
    """svc-x, built by repo-x. owner (OWNS svc-x, no activity) must be
    FORMAL_OWNER. contributor (authors pr-1, which reaches svc-x via BOTH a
    direct REFERENCES edge and MODIFIES repo-x -> PART_OF svc-x -- the same
    PR record reached by two graph paths, which must dedupe to one evidence
    item) must be CONTRIBUTOR, never FORMAL_OWNER."""
    repo.ingest(SourceRecordInput(
        source_id=source_id, record_id="rec-svc", title="Service", acl_subjects=acl or [],
        entities=[
            EntityInput(external_id="svc-x", canonical_id="svc-x", type="SERVICE", name="X"),
            EntityInput(external_id="repo-x", canonical_id="repo-x", type="REPOSITORY", name="Repo X"),
            EntityInput(external_id="owner", canonical_id="owner", type="PERSON", name="Owner"),
        ],
        relationships=[
            RelationInput(source_external_id="repo-x", target_external_id="svc-x", type=RelationType.PART_OF),
            RelationInput(source_external_id="owner", target_external_id="svc-x", type=RelationType.OWNS),
        ],
    ))
    repo.ingest(SourceRecordInput(
        source_id=source_id, record_id="pr-1", title="PR 1", acl_subjects=acl or [],
        entities=[
            EntityInput(external_id="pr-1", canonical_id="pr-1", type="PULL_REQUEST", name="PR 1"),
            EntityInput(external_id="repo-x", canonical_id="repo-x", type="REPOSITORY", name="Repo X"),
            EntityInput(external_id="svc-x", canonical_id="svc-x", type="SERVICE", name="X"),
            EntityInput(external_id="contributor", canonical_id="contributor", type="PERSON", name="Contributor"),
        ],
        relationships=[
            RelationInput(source_external_id="pr-1", target_external_id="repo-x", type=RelationType.MODIFIES),
            RelationInput(source_external_id="pr-1", target_external_id="svc-x", type=RelationType.REFERENCES),
            RelationInput(source_external_id="contributor", target_external_id="pr-1", type=RelationType.AUTHORED),
        ],
    ))


# Scenario 2: ownership distinction -- a contributor with real activity but
# no OWNS record is never labeled the formal owner, while the actual owner
# is; combined with evidence correctness -- the same PR reached via two
# graph paths counts once, and retracting it removes the stale contribution
# rather than continuing to count it.
def test_find_experts_ownership_distinction_and_evidence_correctness(store, registry):
    tenant, workspace = _name(), _name()
    repo = GraphRepository(store, tenant, workspace, "writer", allow_all=True, can_evidence=True)
    _seed(repo)
    executor = ToolExecutor(registry, ToolDependencies(graph_store=store))
    context = _context(tenant, workspace)

    result = _invoke(executor, {"target_id": "svc-x"}, context)
    assert result.status == ToolStatus.SUCCESS
    by_employee = {c["employee_id"]: c for c in result.data["candidates"]}
    assert by_employee["owner"]["classification"] == "FORMAL_OWNER"
    assert by_employee["contributor"]["classification"] == "CONTRIBUTOR"
    assert by_employee["contributor"]["formal_owner"] is False
    # Deduplicated: pr-1 reaches svc-x via two paths (direct REFERENCES, and
    # MODIFIES repo-x -> PART_OF svc-x) but is the same PR record.
    assert by_employee["contributor"]["distinct_evidence_count"] == 1

    retracted = repo.retract_record("src", "pr-1")
    assert retracted is True
    after = _invoke(executor, {"target_id": "svc-x"}, context)
    after_employees = {c["employee_id"] for c in after.data["candidates"]}
    assert "contributor" not in after_employees  # stale contribution no longer counted
    assert "owner" in after_employees  # formal ownership unaffected by unrelated PR retraction


# Scenario 3: authorization -- a restricted contributor cannot be discovered
# through candidate identity, evidence or counts for an unauthorized caller.
def test_find_experts_respects_source_authorization(store, registry):
    tenant, workspace = _name(), _name()
    repo = GraphRepository(store, tenant, workspace, "writer", allow_all=True, can_evidence=True)
    _seed(repo, source_id="restricted-src", acl=["someone-else"])
    executor = ToolExecutor(registry, ToolDependencies(graph_store=store))

    unauthorized = _context(tenant, workspace)
    blocked = _invoke(executor, {"target_id": "svc-x"}, unauthorized)
    assert blocked.status == ToolStatus.NOT_FOUND  # svc-x itself is behind the ACL

    authorized = _context(tenant, workspace, source_ids=("restricted-src",), subject="someone-else")
    allowed = _invoke(executor, {"target_id": "svc-x"}, authorized)
    assert allowed.status == ToolStatus.SUCCESS
    employee_ids = {c["employee_id"] for c in allowed.data["candidates"]}
    assert {"owner", "contributor"} <= employee_ids
