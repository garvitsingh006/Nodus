"""GH-007A section 12: optional, explicitly opt-in live Gemini investigation.

Requires both:
- ``GRAPH_HACKS_LIVE_GEMINI_TEST=1`` and a configured ``GEMINI_API_KEY``
  (same opt-in convention as ``test_model_gateway_live.py``), and
- an isolated Neo4j instance (same convention as the other integration
  tests — ``GRAPH_HACKS_TEST_NEO4J_URI``/``GRAPH_HACKS_TEST_NEO4J_PASSWORD``,
  or a local Docker daemon to spin one up).

Uses a small, configured model (``GEMINI_MODEL_LIGHTWEIGHT``/``_DEFAULT`` via
``GatewayConfig.from_env()``), a tight per-call/per-run token and turn
budget, a single controlled read-only question against a tiny ingested
dataset, and real tool calling through the real ``ToolExecutor``. Never
retried in a loop — one bounded attempt, and a clear failure on anything
else so debugging never re-spends tokens automatically.

Without running this test, GH-007A is OFFLINE-INTEGRATION-VERIFIED only
(see ``test_agent_investigation_integration.py``), not LIVE-VERIFIED.
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

from backend.app.agent.contracts import (
    AgentExecutionContext, ApprovedSkill, InvestigationRequest, InvestigationStatus, RuntimeLimits,
)
from backend.app.agent.investigator import run_investigation
from backend.app.config import Settings
from backend.app.gateway.config import GatewayConfig
from backend.app.gateway.contracts import ModelRole
from backend.app.gateway.gateway import ModelGateway
from backend.app.gateway.gemini_client import GeminiProviderClient
from backend.app.graph.driver import Neo4jStore
from backend.app.graph.models import EntityInput, SourceRecordInput
from backend.app.graph.repository import GraphRepository
from backend.app.security import AuthorizationContext, Permission, Principal
from backend.app.skills.catalog import build_production_skill_registry
from backend.app.tools import register_all_tools
from backend.app.tools.executor import ToolExecutor
from backend.app.tools.registry import ToolDependencies, ToolRegistry


def _opted_in() -> bool:
    return os.getenv("GRAPH_HACKS_LIVE_GEMINI_TEST", "").strip() in {"1", "true", "yes"}


def _name() -> str:
    return uuid.uuid4().hex


@pytest.fixture(scope="module")
def isolated_neo4j():
    if not _opted_in():
        pytest.skip("Live Gemini agent test requires GRAPH_HACKS_LIVE_GEMINI_TEST=1")
    uri = os.getenv("GRAPH_HACKS_TEST_NEO4J_URI")
    password = os.getenv("GRAPH_HACKS_TEST_NEO4J_PASSWORD")
    if uri and password:
        yield uri, password
        return
    suffix = _name()[:12]
    container = f"gh-neo4j-agent-live-{suffix}"
    volume = f"gh-neo4j-agent-live-data-{suffix}"
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


def test_live_gemini_investigation_smoke(isolated_neo4j):
    config = GatewayConfig.from_env()
    if not config.api_key:
        pytest.skip("GEMINI_API_KEY is not configured; cannot run the live agent smoke test")

    uri, password = isolated_neo4j
    settings = Settings(neo4j_uri=uri, neo4j_user="neo4j", neo4j_password=password)
    store = Neo4jStore(settings)
    store.start()
    try:
        tenant, workspace = _name(), _name()
        repo = GraphRepository(store, tenant, workspace, "writer", allow_all=True, can_evidence=True)
        repo.ingest(SourceRecordInput(
            source_id="github", record_id="rec-1", title="rec-1",
            entities=[EntityInput(external_id="alice", canonical_id="alice", type="PERSON", name="Alice")],
        ))

        tool_registry = ToolRegistry()
        register_all_tools(tool_registry)
        skill_registry = build_production_skill_registry(tool_registry=tool_registry)
        tool_executor = ToolExecutor(tool_registry, ToolDependencies(graph_store=store))

        try:
            client = GeminiProviderClient(api_key=config.api_key)
        except RuntimeError as exc:
            pytest.fail(f"google-genai is required for the live agent smoke test: {exc}")
        gateway = ModelGateway(client, config.routing, pricing=config.pricing, retry_policy=config.retry_policy)

        permissions = frozenset({Permission.READ_GRAPH, Permission.READ_EVIDENCE, Permission.EXECUTE_TOOL})
        principal = Principal(subject="live-smoke", tenant_id=tenant, workspaces=(workspace,),
                              permissions=permissions)
        auth = AuthorizationContext(principal=principal, tenant_id=tenant, workspace_id=workspace,
                                    task_id="live-smoke-task", permissions=permissions)
        exec_context = AgentExecutionContext(
            authorization=auth, run_id=f"live-{_name()}", model_role=ModelRole.LIGHTWEIGHT,
            approved_skills=(ApprovedSkill(skill_id="verify-evidence", version="1.0.0"),),
            # Small, explicit limits: a controlled read-only question, bounded cost, no retry loop.
            limits=RuntimeLimits(max_llm_calls=4, max_tool_calls=4, max_output_tokens_per_call=256,
                                 max_total_tokens_per_call=2048, overall_timeout_seconds=60),
        )
        request = InvestigationRequest(
            question="Is there evidence supporting the existence of the entity with id 'alice'?",
            entity_id="alice",
        )

        result = asyncio.run(run_investigation(request, exec_context, gateway, tool_registry, tool_executor,
                                               skill_registry))

        if result.status == InvestigationStatus.BLOCKED and result.error_message:
            pytest.fail(f"Live Gemini investigation was blocked: {result.error_message}")

        assert result.model_usage.llm_calls >= 1
        assert result.summary
        print(f"LIVE-VERIFIED: status={result.status} skill={result.skill_used} "
             f"usage={result.model_usage} summary={result.summary!r}")
    finally:
        store.close()
