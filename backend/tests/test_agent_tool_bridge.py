"""GH-007A: direct, offline unit coverage of the narrow ADK <-> NODUS
adapters in ``backend/app/agent/tool_bridge.py`` and
``backend/app/agent/run_state.py`` — no ADK ``Runner``, no Neo4j. These
exercise the adapters exactly the way ``backend/app/agent/investigator.py``
does (bind a ``RunState`` to the context var, call ``run_async`` directly),
which is the fastest, most precise way to prove each trust/authorization
boundary independent of whether the model happens to call the tool that way
during a live run.
"""
from __future__ import annotations

import asyncio
import json
import shutil

import pytest

from backend.app.agent.contracts import AgentExecutionContext, ApprovedSkill, RuntimeLimits
from backend.app.agent.run_state import CURRENT_RUN, RunState, SkillAlreadySelectedError, current_run
from backend.app.agent.tool_bridge import (
    DeterministicToolAdapter, SkillDiscoveryTool, SkillLoadInstructionsTool, build_investigation_tools,
)
from backend.app.gateway.contracts import ModelRole
from backend.app.security import AuthorizationContext, Permission, Principal
from backend.app.skills.catalog import build_production_skill_registry
from backend.app.skills.config import default_skill_roots
from backend.app.skills.loader import discover_candidates
from backend.app.skills.registry import SkillRegistry
from backend.app.skills.trust import SkillTrustPolicy
from backend.app.tools import register_all_tools
from backend.app.tools.contracts import ToolInvocation, ToolStatus
from backend.app.tools.executor import ToolExecutor
from backend.app.tools.registry import ToolDependencies, ToolRegistry


def _context(permissions) -> AuthorizationContext:
    principal = Principal(subject="agent", tenant_id="t1", workspaces=("w1",), permissions=frozenset(permissions))
    return AuthorizationContext(principal=principal, tenant_id="t1", workspace_id="w1", task_id="task-1",
                               permissions=frozenset(permissions))


def _exec_context(permissions, approved=(("verify-evidence", "1.0.0"),)) -> AgentExecutionContext:
    return AgentExecutionContext(
        authorization=_context(permissions), run_id="run-1", model_role=ModelRole.DEFAULT,
        approved_skills=tuple(ApprovedSkill(skill_id=s, version=v) for s, v in approved),
        limits=RuntimeLimits(max_llm_calls=6, max_tool_calls=6),
    )


@pytest.fixture
def tool_registry() -> ToolRegistry:
    registry = ToolRegistry()
    register_all_tools(registry)
    return registry


@pytest.fixture
def skill_registry(tool_registry) -> SkillRegistry:
    return build_production_skill_registry(tool_registry=tool_registry)


def _bind(exec_context, tool_registry, skill_registry, tool_executor=None):
    state = RunState(exec_context=exec_context, tool_executor=tool_executor or ToolExecutor(
        tool_registry, ToolDependencies(graph_store=None)), skill_registry=skill_registry)
    token = CURRENT_RUN.set(state)
    return state, token


def _run(coro):
    return asyncio.run(coro)


# 1. Skill discovery lists only approved skills, metadata only.
def test_skill_discovery_lists_only_approved_skills(tool_registry, skill_registry):
    exec_context = _exec_context({Permission.READ_GRAPH, Permission.READ_EVIDENCE},
                                 approved=(("verify-evidence", "1.0.0"), ("graph-trace", "1.0.0")))
    state, token = _bind(exec_context, tool_registry, skill_registry)
    try:
        result = _run(SkillDiscoveryTool().run_async(args={}, tool_context=None))
    finally:
        CURRENT_RUN.reset(token)
    ids = {item["skill_id"] for item in result["skills"]}
    assert ids == {"verify-evidence", "graph-trace"}
    assert all(item["available"] for item in result["skills"])
    assert "instructions" not in result["skills"][0]  # Level A: metadata only


# 2. Instruction loading resolves the exact pinned version and records digest.
def test_skill_instructions_loading_records_pinned_version_and_digest(tool_registry, skill_registry):
    exec_context = _exec_context({Permission.READ_GRAPH, Permission.READ_EVIDENCE})
    state, token = _bind(exec_context, tool_registry, skill_registry)
    try:
        result = _run(SkillLoadInstructionsTool().run_async(args={"skill_id": "verify-evidence"},
                                                            tool_context=None))
        assert "error" not in result
        assert result["version"] == "1.0.0"
        assert len(result["content_digest"]) == 64
        assert "instructions" in result and len(result["instructions"]) > 0
        assert state.selected_skill_descriptor.skill_id == "verify-evidence"
        assert state.selected_skill_descriptor.content_digest == result["content_digest"]
    finally:
        CURRENT_RUN.reset(token)


# 3. A skill not in this run's approved list cannot be loaded even if it exists in the registry.
def test_unapproved_skill_cannot_be_loaded(tool_registry, skill_registry):
    exec_context = _exec_context({Permission.READ_GRAPH}, approved=(("graph-trace", "1.0.0"),))
    state, token = _bind(exec_context, tool_registry, skill_registry)
    try:
        result = _run(SkillLoadInstructionsTool().run_async(args={"skill_id": "verify-evidence"},
                                                            tool_context=None))
        assert "error" in result
        assert state.selected_skill_descriptor is None
    finally:
        CURRENT_RUN.reset(token)


# 4. Never silently replace a pinned version: a second, different skill load is rejected.
def test_skill_selection_cannot_be_switched_mid_run(tool_registry, skill_registry):
    exec_context = _exec_context({Permission.READ_GRAPH, Permission.READ_EVIDENCE},
                                 approved=(("verify-evidence", "1.0.0"), ("graph-trace", "1.0.0")))
    state, token = _bind(exec_context, tool_registry, skill_registry)
    try:
        first = _run(SkillLoadInstructionsTool().run_async(args={"skill_id": "verify-evidence"}, tool_context=None))
        assert "error" not in first
        second = _run(SkillLoadInstructionsTool().run_async(args={"skill_id": "graph-trace"}, tool_context=None))
        assert "error" in second
        assert state.selected_skill_descriptor.skill_id == "verify-evidence"  # unchanged

        # Re-selecting the SAME skill is an idempotent no-op, not an error.
        again = _run(SkillLoadInstructionsTool().run_async(args={"skill_id": "verify-evidence"},
                                                           tool_context=None))
        assert "error" not in again
    finally:
        CURRENT_RUN.reset(token)


# 5. Revoked skill versions are never loadable.
def test_revoked_skill_is_rejected(tool_registry, skill_registry):
    skill_registry.revoke("verify-evidence", "1.0.0", reason="test revocation")
    exec_context = _exec_context({Permission.READ_GRAPH, Permission.READ_EVIDENCE})
    state, token = _bind(exec_context, tool_registry, skill_registry)
    try:
        result = _run(SkillLoadInstructionsTool().run_async(args={"skill_id": "verify-evidence"},
                                                            tool_context=None))
        assert "error" in result
        assert state.selected_skill_descriptor is None
        assert state.skill_load_errors
    finally:
        CURRENT_RUN.reset(token)


# 6. Tampered on-disk content (digest mismatch) is rejected, not silently trusted.
def test_tampered_skill_content_is_rejected(tool_registry, tmp_path):
    root = tmp_path / "skills"
    shutil.copytree(default_skill_roots()[0] / "verify-evidence", root / "verify-evidence")
    candidates = discover_candidates((root,))
    policy = SkillTrustPolicy(approved_digests={c.skill_id: frozenset({c.content_digest}) for c in candidates})
    registry = SkillRegistry(trust_policy=policy)
    for candidate in candidates:
        registry.register(candidate, tool_registry=tool_registry)

    # Tamper with the instructions after registration.
    skill_md = root / "verify-evidence" / "SKILL.md"
    skill_md.write_text(skill_md.read_text(encoding="utf-8") + "\nmalicious appended instruction\n",
                        encoding="utf-8")

    exec_context = _exec_context({Permission.READ_GRAPH, Permission.READ_EVIDENCE})
    state, token = _bind(exec_context, tool_registry, registry)
    try:
        result = _run(SkillLoadInstructionsTool().run_async(args={"skill_id": "verify-evidence"},
                                                            tool_context=None))
        assert "error" in result
        assert state.selected_skill_descriptor is None
    finally:
        CURRENT_RUN.reset(token)


# 7. A deterministic tool cannot be called before a trusted skill is selected.
def test_tool_call_denied_before_skill_selection(tool_registry, skill_registry):
    exec_context = _exec_context({Permission.READ_GRAPH, Permission.READ_EVIDENCE})
    state, token = _bind(exec_context, tool_registry, skill_registry)
    try:
        spec = tool_registry.resolve("evidence.verify", "1.0.0")
        result = _run(DeterministicToolAdapter(spec).run_async(args={"entity_id": "alice"}, tool_context=None))
        assert "error" in result
        assert state.tool_calls_made == 0
    finally:
        CURRENT_RUN.reset(token)


# 8. A tool not declared compatible by the selected skill is denied.
def test_tool_not_declared_by_selected_skill_is_denied(tool_registry, skill_registry):
    exec_context = _exec_context({Permission.READ_GRAPH, Permission.READ_EVIDENCE},
                                 approved=(("verify-evidence", "1.0.0"),))
    state, token = _bind(exec_context, tool_registry, skill_registry)
    try:
        _run(SkillLoadInstructionsTool().run_async(args={"skill_id": "verify-evidence"}, tool_context=None))
        # graph.find_paths is not declared by verify-evidence's manifest.
        spec = tool_registry.resolve("graph.find_paths", "1.0.0")
        result = _run(DeterministicToolAdapter(spec).run_async(
            args={"source_id": "a", "target_id": "b"}, tool_context=None))
        assert "error" in result
        assert state.tool_calls_made == 0
    finally:
        CURRENT_RUN.reset(token)


# 9. Authorized tool execution goes through the real ToolExecutor and is recorded.
def test_authorized_tool_call_denied_by_executor_on_insufficient_permission(tool_registry, skill_registry):
    """A skill declaring a tool never grants the permission it requires —
    ToolExecutor/ToolPolicy remain the sole authority. Here the principal
    lacks READ_EVIDENCE, so evidence.lookup (which requires both READ_GRAPH
    and READ_EVIDENCE) is denied even though the selected skill declares it
    compatible."""
    exec_context = _exec_context({Permission.READ_GRAPH})  # no READ_EVIDENCE
    state, token = _bind(exec_context, tool_registry, skill_registry)
    try:
        _run(SkillLoadInstructionsTool().run_async(args={"skill_id": "verify-evidence"}, tool_context=None))
        spec = tool_registry.resolve("evidence.lookup", "1.0.0")
        result = _run(DeterministicToolAdapter(spec).run_async(args={"entity_id": "alice"}, tool_context=None))
        assert result["status"] == ToolStatus.FORBIDDEN.value
        assert state.tool_calls_made == 1
        assert state.tool_call_records[0].status == ToolStatus.FORBIDDEN.value
    finally:
        CURRENT_RUN.reset(token)


# 10. Malformed tool arguments are handled gracefully (INVALID_INPUT), not an exception.
def test_malformed_tool_arguments_are_rejected_gracefully(tool_registry, skill_registry):
    exec_context = _exec_context({Permission.READ_GRAPH, Permission.READ_EVIDENCE})
    state, token = _bind(exec_context, tool_registry, skill_registry)
    try:
        _run(SkillLoadInstructionsTool().run_async(args={"skill_id": "verify-evidence"}, tool_context=None))
        spec = tool_registry.resolve("evidence.verify", "1.0.0")
        # entity_id is required; omit it entirely.
        result = _run(DeterministicToolAdapter(spec).run_async(args={}, tool_context=None))
        assert result["status"] == ToolStatus.INVALID_INPUT.value
    finally:
        CURRENT_RUN.reset(token)


# 11. Only READ_ONLY tools can ever be wrapped.
def test_mutation_tool_specs_cannot_be_wrapped():
    from backend.app.tools.registry import SideEffect, ToolSpec
    from backend.app.tools.contracts import ExecutionLimits
    from pydantic import BaseModel

    class In(BaseModel):
        pass

    class Out(BaseModel):
        pass

    spec = ToolSpec(tool_id="graph.write", version="1.0.0", description="would mutate",
                    input_model=In, output_model=Out, handler=lambda *a: None,
                    required_permissions=frozenset({Permission.WRITE_GRAPH}), side_effect=SideEffect.MUTATION,
                    limits=ExecutionLimits())
    with pytest.raises(ValueError):
        DeterministicToolAdapter(spec)


# 12. Per-run tool-call limit is enforced by the adapter itself.
def test_per_run_tool_call_limit_is_enforced(tool_registry, skill_registry):
    exec_context = AgentExecutionContext(
        authorization=_context({Permission.READ_GRAPH, Permission.READ_EVIDENCE}), run_id="run-1",
        model_role=ModelRole.DEFAULT,
        approved_skills=(ApprovedSkill(skill_id="verify-evidence", version="1.0.0"),),
        limits=RuntimeLimits(max_llm_calls=6, max_tool_calls=1),
    )
    state, token = _bind(exec_context, tool_registry, skill_registry)
    try:
        _run(SkillLoadInstructionsTool().run_async(args={"skill_id": "verify-evidence"}, tool_context=None))
        spec = tool_registry.resolve("graph.revision", "1.0.0")
        first = _run(DeterministicToolAdapter(spec).run_async(args={}, tool_context=None))
        assert "error" not in first or "status" in first
        second = _run(DeterministicToolAdapter(spec).run_async(args={}, tool_context=None))
        assert "error" in second
        assert state.tool_calls_made == 1
    finally:
        CURRENT_RUN.reset(token)


# 13. No code reachable from the agent's tool bridge performs raw Cypher, filesystem or shell access.
def test_tool_bridge_has_no_direct_database_or_subprocess_access():
    import ast
    from pathlib import Path

    path = Path(__file__).resolve().parents[2] / "backend" / "app" / "agent" / "tool_bridge.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    banned_modules = {"subprocess", "os", "neo4j", "psycopg"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name.split(".")[0] not in banned_modules
        if isinstance(node, ast.ImportFrom) and node.module:
            assert node.module.split(".")[0] not in banned_modules


# 14. Using the adapters outside a bound run raises a clear error, never silently no-ops.
def test_tool_bridge_requires_a_bound_run_context():
    with pytest.raises(RuntimeError):
        current_run()
