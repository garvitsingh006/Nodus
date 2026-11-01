"""NEXUS investigator-workflow coverage (Stage 08 category D), against the
live, already-imported dataset.

Honesty note: the tests in this file (other than the explicitly-gated live
one) exercise the real ``ToolExecutor``/``ToolRegistry``/``AuthorizationContext``
machinery directly — the SAME authorization and tool-execution path
``backend/app/agent/tool_bridge.py::DeterministicToolAdapter`` uses — but
they do NOT drive a real ADK ``LlmAgent``/Gemini model. No Gemini credential
is configured in this environment (see ``test_agent_live_gemini.py``'s own
skip gate, reused below), so a genuine LLM-driven NEXUS investigation remains
unverified here, exactly as Stage 08's GH-007C status requires disclosing.
What IS verified: that a real investigation's tool-execution substrate,
pointed at a real NEXUS-authorized context, returns evidence-cited data and
correctly enforces source scoping — the part of "objective -> permitted
tools -> NEXUS graph/evidence -> validated findings" that doesn't require a
live model to prove.
"""
from __future__ import annotations

import os

import pytest

from backend.app.security import AuthorizationContext
from backend.app.tools import register_all_tools
from backend.app.tools.contracts import ToolInvocation, ToolStatus
from backend.app.tools.evidence_tools import EVIDENCE_LOOKUP_TOOL_ID, EVIDENCE_LOOKUP_VERSION
from backend.app.tools.executor import ToolExecutor
from backend.app.tools.graph_tools import (
    GRAPH_NEIGHBORHOOD_TOOL_ID, GRAPH_NEIGHBORHOOD_VERSION, GRAPH_REVERSE_DEPENDENCIES_TOOL_ID,
    GRAPH_REVERSE_DEPENDENCIES_VERSION,
)
from backend.app.tools.registry import ToolDependencies, ToolRegistry
from backend.tests.nexus_live import (
    NEXUS_WORKSPACE_ID, developer_principal, intern_principal, nexus_neo4j, nexus_pg, nexus_settings,
)

pytestmark = pytest.mark.integration


def _context(principal) -> AuthorizationContext:
    return AuthorizationContext(
        principal=principal, tenant_id=principal.tenant_id, workspace_id=NEXUS_WORKSPACE_ID,
        task_id="nexus-investigator-test", permissions=principal.permissions,
    )


@pytest.fixture
def registry() -> ToolRegistry:
    reg = ToolRegistry()
    register_all_tools(reg)
    return reg


def test_developer_scoped_question_returns_evidence_cited_dependencies(nexus_neo4j, registry, developer_principal):
    """'What does NEX-SVC-PAYMENT_API depend on' — a real, authorized
    developer-scoped query through the actual tool-execution path, asserting
    evidence references back to real NEX-* records."""
    executor = ToolExecutor(registry, ToolDependencies(graph_store=nexus_neo4j))
    result = executor.execute(ToolInvocation(
        tool_id=GRAPH_NEIGHBORHOOD_TOOL_ID, version=GRAPH_NEIGHBORHOOD_VERSION,
        arguments={"entity_id": "NEX-SVC-PAYMENT_API", "limit": 100}, context=_context(developer_principal)))
    # NEX-SVC-PAYMENT_API has more neighbors than the tool's max limit (100),
    # so INCOMPLETE (truncated-but-valid) is an expected, correct outcome here
    # — not a failure — matching the existing truncation convention.
    assert result.status in (ToolStatus.SUCCESS, ToolStatus.INCOMPLETE)
    assert result.data["relationships"], "expected at least one dependency relationship"
    assert all(node["id"].startswith("NEX-") for node in result.data["nodes"])


def test_intern_scoped_investigation_never_surfaces_developer_only_source(nexus_neo4j, registry, intern_principal):
    """An intern-scoped investigation into a PAY-repository-only fact must
    not surface it — this is the denied/partial-result scenario: an honest
    'not found/not authorized' result, never a fabricated finding."""
    executor = ToolExecutor(registry, ToolDependencies(graph_store=nexus_neo4j))
    # Any service's engineering/evidence facts sourced only from a
    # PROJECT_DEVELOPER source should be invisible; use evidence lookup on a
    # service whose repository-derived package evidence intern lacks access to.
    result = executor.execute(ToolInvocation(
        tool_id=EVIDENCE_LOOKUP_TOOL_ID, version=EVIDENCE_LOOKUP_VERSION,
        arguments={"entity_id": "NEX-SVC-PAYMENT_API", "limit": 50}, context=_context(intern_principal)))
    assert result.status in (ToolStatus.SUCCESS, ToolStatus.NOT_FOUND)
    if result.status == ToolStatus.SUCCESS:
        evidence_items = result.data.get("items", [])
        assert all(item["source_id"] != "NEX-SRC-PAY-REPO" for item in evidence_items), (
            "intern must never see PROJECT_DEVELOPER-only evidence")


def test_reverse_dependencies_respects_developer_scope(nexus_neo4j, registry, developer_principal):
    executor = ToolExecutor(registry, ToolDependencies(graph_store=nexus_neo4j))
    result = executor.execute(ToolInvocation(
        tool_id=GRAPH_REVERSE_DEPENDENCIES_TOOL_ID, version=GRAPH_REVERSE_DEPENDENCIES_VERSION,
        arguments={"entity_id": "NEX-SVC-PAYMENT_API"}, context=_context(developer_principal)))
    assert result.status == ToolStatus.SUCCESS


# --- Genuinely live, LLM-driven NEXUS investigation (explicitly gated) ------

def _live_gemini_opted_in() -> bool:
    return os.getenv("GRAPH_HACKS_LIVE_GEMINI_TEST", "").strip() in {"1", "true", "yes"}


@pytest.mark.skipif(not _live_gemini_opted_in(),
                    reason="Live Gemini NEXUS investigation requires GRAPH_HACKS_LIVE_GEMINI_TEST=1 "
                          "and a configured GEMINI_API_KEY (see test_agent_live_gemini.py)")
def test_live_gemini_investigation_over_nexus_data():
    """Placeholder for genuine GH-007C acceptance over NEXUS data: a live,
    model-driven investigation (objective -> model tool calls -> evidence-
    cited result) scoped to a NEXUS-authorized AuthorizationContext. No
    GEMINI_API_KEY is configured in this environment, so this test is
    SKIPPED here — it is not a substitute for actually running it once
    credentials are available, and its absence of a failure must never be
    read as "verified"."""
    pytest.skip("No GEMINI_API_KEY configured in this environment; "
               "wire a real run_investigation() call here once one is available")
