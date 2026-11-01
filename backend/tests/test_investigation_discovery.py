"""Stage 11 (ND-11A/B/C): objective-driven incremental relationship discovery.

Follows the exact test harness convention of
``test_agent_investigation_integration.py`` (same ``isolated_neo4j``
fixture, same offline scripted ``FakeProviderClient`` -- no live model
required). Every tenant/workspace id used here is a fresh ``uuid4().hex``,
never the shared ``nexus-tenant``/``nexus-workspace`` demo dataset, per the
task owner's explicit constraint that the shared graph may not yet reflect
the Stage 10 commit-authorship fix.
"""
from __future__ import annotations

import asyncio
import os
import secrets
import socket
import subprocess
import time
import uuid

import pytest
from neo4j import GraphDatabase

from backend.app.agent.contracts import InvestigationStatus
from backend.app.agent.discovery import (
    CandidateRejection, RelationshipCandidate, build_candidates, effective_source_scope,
    run_scoped_investigation, validate_and_commit,
)
from backend.app.config import Settings
from backend.app.gateway.contracts import ModelRole
from backend.app.gateway.gateway import ModelGateway
from backend.app.gateway.provider import ProviderResponse, RawFunctionCall, RawUsage
from backend.app.gateway.routing import RoleRouter
from backend.app.graph.driver import Neo4jStore
from backend.app.graph.models import AssertionOrigin, EntityInput, RelationInput, RelationType, SourceRecordInput
from backend.app.graph.repository import GraphRepository
from backend.app.security import Permission, Principal
from backend.app.skills.catalog import build_production_skill_registry
from backend.app.tools import register_all_tools
from backend.app.tools.executor import ToolExecutor
from backend.app.tools.registry import ToolDependencies, ToolRegistry

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
    container = f"gh-neo4j-discovery-{suffix}"
    volume = f"gh-neo4j-discovery-data-{suffix}"
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
            pytest.fail("BLOCKED: isolated Neo4j did not become ready (environment, not code)")
        driver.close()
        yield uri, password
    finally:
        subprocess.run(["docker", "rm", "-f", container], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(["docker", "volume", "rm", volume], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


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
def tool_registry() -> ToolRegistry:
    registry = ToolRegistry()
    register_all_tools(registry)
    return registry


@pytest.fixture
def skill_registry(tool_registry):
    return build_production_skill_registry(tool_registry=tool_registry)


class FakeProviderClient:
    def __init__(self):
        self.calls = 0
        self.queue: list = []

    def count_tokens(self, model, contents) -> int:
        return 10

    def generate(self, model, contents, config) -> ProviderResponse:
        self.calls += 1
        return self.queue.pop(0)

    def available_models(self):
        return frozenset({"fake-lite", "fake-default"})


def _success(text=None, function_calls=()) -> ProviderResponse:
    return ProviderResponse(
        text=text, parsed=None,
        usage=RawUsage(input_tokens=10, output_tokens=5, thinking_tokens=0, total_tokens=15),
        model_version="fake-default-001", finish_reason="STOP", safety_blocked=False, function_calls=function_calls,
    )


def _router() -> RoleRouter:
    return RoleRouter(role_models={ModelRole.LIGHTWEIGHT: "fake-lite", ModelRole.DEFAULT: "fake-default"})


def _lookup_sequence(*entity_ids: str, final_text: str) -> list:
    """skill_discover -> skill_load_instructions(graph-trace) -> one
    graph_entity_lookup per entity_id -> final answer."""
    queue = [
        _success(function_calls=(RawFunctionCall(id="skd", name="skill_discover", arguments={}),)),
        _success(function_calls=(RawFunctionCall(id="skl", name="skill_load_instructions",
                                                 arguments={"skill_id": "graph-trace"}),)),
    ]
    for i, entity_id in enumerate(entity_ids):
        queue.append(_success(function_calls=(
            RawFunctionCall(id=f"e{i}", name="graph_entity_lookup", arguments={"entity_id": entity_id}),)))
    queue.append(_success(text=final_text))
    return queue


def _ingest_cooccurrence_fixture(store, tenant, workspace):
    """alice AUTHORED file1 (github); alice and bob co-occur in the same
    github pr-1 record with no direct relationship between them (the Stage 11
    discovery candidate); carol co-occurs with alice only in a *confidential*
    source that a restricted test principal never selects/authorizes."""
    repo = GraphRepository(store, tenant, workspace, "writer", allow_all=True, can_evidence=True)
    repo.ingest(SourceRecordInput(
        source_id="github", record_id="pr-1", title="pr-1",
        entities=[
            EntityInput(external_id="alice", canonical_id="alice", type="PERSON", name="Alice"),
            EntityInput(external_id="bob", canonical_id="bob", type="PERSON", name="Bob"),
            EntityInput(external_id="file1", canonical_id="file1", type="FILE", name="File One"),
        ],
        relationships=[RelationInput(source_external_id="alice", target_external_id="file1",
                                     type="AUTHORED", origin="source_asserted")],
    ))
    repo.ingest(SourceRecordInput(
        source_id="confidential", record_id="secret-1", title="secret-1",
        entities=[
            EntityInput(external_id="alice", canonical_id="alice", type="PERSON", name="Alice"),
            EntityInput(external_id="carol", canonical_id="carol", type="PERSON", name="Carol"),
        ],
    ))
    return repo


def _principal(tenant, workspace, *, source_ids=()) -> Principal:
    permissions = frozenset({Permission.READ_GRAPH, Permission.READ_EVIDENCE, Permission.EXECUTE_TOOL})
    return Principal(subject="reader", tenant_id=tenant, workspaces=(workspace,), permissions=permissions,
                     source_ids=source_ids)


def _run(objective, *, provider_queue, store, tool_registry, skill_registry, tenant, workspace,
        selected_source_ids, principal):
    provider = FakeProviderClient()
    provider.queue = provider_queue
    gateway = ModelGateway(provider, _router())
    tool_executor = ToolExecutor(tool_registry, ToolDependencies(graph_store=store))
    return asyncio.run(run_scoped_investigation(
        objective=objective, selected_source_ids=selected_source_ids, principal=principal,
        workspace_id=workspace, run_id=f"run-{_name()}", graph_store=store, model_gateway=gateway,
        tool_registry=tool_registry, tool_executor=tool_executor, skill_registry=skill_registry,
    ))


# 1. Full investigation: graph-first retrieval + evidence-backed candidate validation + commit.
def test_scoped_investigation_discovers_evidence_backed_relationship(store, tool_registry, skill_registry):
    tenant, workspace = _name(), _name()
    _ingest_cooccurrence_fixture(store, tenant, workspace)
    principal = _principal(tenant, workspace, source_ids=("github", "confidential"))

    result = _run(
        "How does Alice relate to other people in the repository?",
        provider_queue=_lookup_sequence("alice", "bob", final_text="Alice authored File One; Bob is also present."),
        store=store, tool_registry=tool_registry, skill_registry=skill_registry, tenant=tenant, workspace=workspace,
        selected_source_ids=("github",), principal=principal,
    )

    assert result.status in (InvestigationStatus.COMPLETE, InvestigationStatus.PARTIAL)
    assert {"alice", "bob"} <= set(result.entity_ids)
    assert result.newly_discovered_relationship_ids, "expected a new discovered relationship on first run"
    assert set(result.newly_discovered_relationship_ids) <= set(result.relationship_ids)
    assert result.graph_revision is not None

    # Repeat the exact same objective over unchanged evidence: idempotent reuse, not duplication.
    result2 = _run(
        "How does Alice relate to other people in the repository?",
        provider_queue=_lookup_sequence("alice", "bob", final_text="Alice authored File One; Bob is also present."),
        store=store, tool_registry=tool_registry, skill_registry=skill_registry, tenant=tenant, workspace=workspace,
        selected_source_ids=("github",), principal=principal,
    )
    assert not result2.newly_discovered_relationship_ids
    assert set(result2.reused_relationship_ids) == set(result.newly_discovered_relationship_ids)


# 2. Two different objectives against the same company produce different relevant expansions.
def test_different_objectives_produce_different_expansions(store, tool_registry, skill_registry):
    tenant, workspace = _name(), _name()
    _ingest_cooccurrence_fixture(store, tenant, workspace)
    principal = _principal(tenant, workspace, source_ids=("github", "confidential"))

    result_authorship = _run(
        "Who authored File One?",
        provider_queue=_lookup_sequence("alice", "file1", final_text="Alice authored File One."),
        store=store, tool_registry=tool_registry, skill_registry=skill_registry, tenant=tenant, workspace=workspace,
        selected_source_ids=("github",), principal=principal,
    )
    result_people = _run(
        "Who else is connected to Alice?",
        provider_queue=_lookup_sequence("alice", "bob", final_text="Alice and Bob are both present in pr-1."),
        store=store, tool_registry=tool_registry, skill_registry=skill_registry, tenant=tenant, workspace=workspace,
        selected_source_ids=("github",), principal=principal,
    )

    assert set(result_authorship.entity_ids) != set(result_people.entity_ids)
    assert "file1" in result_authorship.entity_ids and "file1" not in result_people.entity_ids
    assert "bob" in result_people.entity_ids and "bob" not in result_authorship.entity_ids


# 3. Authorization: an unselected/forbidden source never enters entity_ids/relationship_ids/evidence_refs.
def test_unselected_source_never_enters_results(store, tool_registry, skill_registry):
    tenant, workspace = _name(), _name()
    _ingest_cooccurrence_fixture(store, tenant, workspace)
    # The principal IS authorized for "confidential" too, but this run only selects "github" --
    # ND-11A must drop the unselected source from the effective scope regardless.
    principal = _principal(tenant, workspace, source_ids=("github", "confidential"))

    result = _run(
        "Who is connected to Alice?",
        provider_queue=_lookup_sequence("alice", "carol", final_text="Looked up Alice and Carol."),
        store=store, tool_registry=tool_registry, skill_registry=skill_registry, tenant=tenant, workspace=workspace,
        selected_source_ids=("github",), principal=principal,
    )

    # carol only exists via the unselected "confidential" source -- never resolved, never leaked.
    assert "carol" not in result.entity_ids
    assert "carol" not in " ".join(result.rejected_unverified_claims)
    assert all("confidential" not in call.model_dump_json() for call in result.tool_calls)
    assert all("confidential" not in ref for ref in result.evidence_refs)

    # Direct scope check: effective_source_scope itself drops the unselected source.
    scope = effective_source_scope(("github",), principal)
    assert scope == frozenset({"github"})
    assert "confidential" not in scope


# 4. Deterministic candidate validation: ontology-invalid/unsupported-evidence candidates are
#    rejected (never promoted), a valid candidate is committed, and re-validation reuses it.
def test_candidate_validation_rejects_invalid_and_reuses_valid(store, tool_registry):
    tenant, workspace = _name(), _name()
    repo = _ingest_cooccurrence_fixture(store, tenant, workspace)
    scope = frozenset({"github"})
    observed = frozenset({"alice", "bob"})
    observed_refs = {"github:pr-1"}

    read_repo = GraphRepository(store, tenant, workspace, "reader", allow_all=False, can_evidence=True,
                                authorized_source_ids=scope)
    write_repo = GraphRepository(store, tenant, workspace, "nodus-discovery", allow_all=True, can_evidence=True)

    invalid_ontology = RelationshipCandidate(
        source_entity_id="alice", target_entity_id="bob", relation_type=RelationType.OWNS,
        origin=AssertionOrigin.OBSERVED, evidence_refs=("github:pr-1",), supporting_source_ids=("github",))
    unobserved_evidence = RelationshipCandidate(
        source_entity_id="alice", target_entity_id="bob", relation_type=RelationType.RELATED_TO,
        origin=AssertionOrigin.OBSERVED, evidence_refs=("github:never-fetched",),
        supporting_source_ids=("github",))
    out_of_scope_evidence = RelationshipCandidate(
        source_entity_id="alice", target_entity_id="bob", relation_type=RelationType.RELATED_TO,
        origin=AssertionOrigin.OBSERVED, evidence_refs=("confidential:secret-1",),
        supporting_source_ids=("confidential",))
    valid = RelationshipCandidate(
        source_entity_id="alice", target_entity_id="bob", relation_type=RelationType.RELATED_TO,
        origin=AssertionOrigin.OBSERVED, evidence_refs=("github:pr-1",), supporting_source_ids=("github",))

    outcome = validate_and_commit(read_repo, write_repo,
                                  [invalid_ontology, unobserved_evidence, out_of_scope_evidence, valid],
                                  observed, scope, frozenset(observed_refs))

    assert len(outcome.rejected) == 3
    reasons = "\n".join(r.reason for r in outcome.rejected)
    assert "OWNS" in reasons or "PERSON" in reasons  # ontology rejection names the disallowed combo
    assert any("never actually observed" in r.reason for r in outcome.rejected)
    assert any("outside the effective" in r.reason for r in outcome.rejected)
    assert len(outcome.committed_relationship_ids) == 1
    first_id = outcome.committed_relationship_ids[0]

    # Re-validating the identical valid candidate reuses the same relationship id (idempotent).
    outcome2 = validate_and_commit(read_repo, write_repo, [valid], observed, scope, frozenset(observed_refs))
    assert not outcome2.committed_relationship_ids
    assert outcome2.reused_relationship_ids == (first_id,)


# Also exercises build_candidates directly: unauthorized-source co-occurrence (alice/carol via
# "confidential") must never be proposed when the effective scope excludes that source.
def test_build_candidates_excludes_unauthorized_source_cooccurrence(store, tool_registry):
    tenant, workspace = _name(), _name()
    _ingest_cooccurrence_fixture(store, tenant, workspace)
    scope = frozenset({"github"})
    read_repo = GraphRepository(store, tenant, workspace, "reader", allow_all=False, can_evidence=True,
                                authorized_source_ids=scope)
    observed_refs: set[str] = set()

    candidates = build_candidates(read_repo, frozenset({"alice", "bob", "carol"}), scope, observed_refs)

    pairs = {frozenset((c.source_entity_id, c.target_entity_id)) for c in candidates}
    assert frozenset({"alice", "bob"}) in pairs
    assert frozenset({"alice", "carol"}) not in pairs
    assert all(ref.startswith("github:") for c in candidates for ref in c.evidence_refs)
