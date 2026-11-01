"""Per-run isolation: a ``contextvars.ContextVar`` holding the trusted state
for exactly one investigation run, never a module-level mutable dict/global.

``contextvars`` are task-local under asyncio: a value set inside one
``run_investigation()`` coroutine is invisible to any sibling coroutine
started independently (e.g. via ``asyncio.gather``) and is never mutated by
another run — this is what satisfies GH-007A's "no global mutable
authorization context" / "prevent context leakage between concurrent users
and runs" requirements, without a lock-guarded registry of runs.

``backend.app.agent.gateway_llm.GatewayLlm`` and
``backend.app.agent.tool_bridge`` tools read the current run's state from
here; nothing else should.
"""
from __future__ import annotations

import contextvars
import dataclasses

from backend.app.agent.contracts import AgentExecutionContext, ModelUsageSummary, ToolCallRecord
from backend.app.skills.contracts import SkillDescriptor, SkillManifest
from backend.app.skills.registry import SkillRegistry
from backend.app.tools.contracts import ToolResult
from backend.app.tools.executor import ToolExecutor


class SkillAlreadySelectedError(ValueError):
    """Raised when the model tries to switch the active skill mid-run — a
    pinned skill selection is never silently replaced during an invocation."""


@dataclasses.dataclass
class RunState:
    exec_context: AgentExecutionContext
    tool_executor: ToolExecutor
    skill_registry: SkillRegistry

    selected_skill_descriptor: SkillDescriptor | None = dataclasses.field(default=None)
    selected_skill_manifest: SkillManifest | None = dataclasses.field(default=None)
    skill_load_errors: list[str] = dataclasses.field(default_factory=list)

    llm_calls: int = 0
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_tokens: int = 0
    estimated_cost_usd: float | None = None
    model_used: str | None = None
    hard_stopped_reason: str | None = None

    tool_calls_made: int = 0
    tool_call_records: list[ToolCallRecord] = dataclasses.field(default_factory=list)
    observed_entity_ids: set[str] = dataclasses.field(default_factory=set)
    observed_relationship_ids: set[str] = dataclasses.field(default_factory=set)
    observed_evidence_refs: set[str] = dataclasses.field(default_factory=set)
    observed_graph_revision: int | None = None
    observed_conflicts: list[str] = dataclasses.field(default_factory=list)
    observed_source_limitations: list[str] = dataclasses.field(default_factory=list)
    not_found_entity_ids: set[str] = dataclasses.field(default_factory=set)

    def approved_version(self, skill_id: str) -> str | None:
        for approved in self.exec_context.approved_skills:
            if approved.skill_id == skill_id:
                return approved.version
        return None

    def select_skill(self, descriptor: SkillDescriptor, manifest: SkillManifest) -> None:
        if self.selected_skill_descriptor is not None:
            if (self.selected_skill_descriptor.skill_id, self.selected_skill_descriptor.version) != (
                    descriptor.skill_id, descriptor.version):
                raise SkillAlreadySelectedError(
                    f"Skill {self.selected_skill_descriptor.skill_id}@"
                    f"{self.selected_skill_descriptor.version} is already pinned for this run; "
                    f"cannot switch to {descriptor.skill_id}@{descriptor.version}")
            return  # idempotent re-selection of the same pinned version
        self.selected_skill_descriptor = descriptor
        self.selected_skill_manifest = manifest

    def tool_allowed(self, tool_id: str, version: str) -> bool:
        if self.selected_skill_manifest is None:
            return False
        return any(dep.tool_id == tool_id and dep.version == version
                   for dep in self.selected_skill_manifest.compatible_tools)

    def record_llm_usage(self, *, input_tokens: int | None, output_tokens: int | None,
                         total_tokens: int | None, estimated_cost_usd: float | None,
                         actual_model: str | None = None) -> None:
        self.llm_calls += 1
        self.total_input_tokens += input_tokens or 0
        self.total_output_tokens += output_tokens or 0
        self.total_tokens += total_tokens or 0
        if estimated_cost_usd is not None:
            self.estimated_cost_usd = (self.estimated_cost_usd or 0.0) + estimated_cost_usd
        if actual_model:
            self.model_used = actual_model

    def record_tool_result(self, tool_id: str, version: str, result: ToolResult,
                           arguments: dict | None = None) -> None:
        self.tool_calls_made += 1
        self.tool_call_records.append(ToolCallRecord(
            tool_id=tool_id, version=version, status=result.status.value,
            evidence_refs=result.evidence_refs, graph_revision=result.graph_revision,
        ))
        self.observed_evidence_refs.update(result.evidence_refs)
        if result.graph_revision is not None:
            self.observed_graph_revision = result.graph_revision

        data = result.data or {}
        _collect_identifiers(data, self.observed_entity_ids, self.observed_relationship_ids)

        if "conflicts" in data:
            conflicts = data.get("conflicts") or {}
            for field_name, values in conflicts.items():
                self.observed_conflicts.append(f"{data.get('entity_id', '?')}.{field_name}: {values!r}")

        if tool_id.startswith("source.coverage"):
            status = data.get("status")
            if status and status != "AVAILABLE":
                self.observed_source_limitations.append(f"{data.get('source_id', '?')}: {status}")

        if result.status.value == "NOT_FOUND" and arguments:
            entity_id = arguments.get("entity_id")
            if entity_id:
                self.not_found_entity_ids.add(entity_id)

    def usage_summary(self) -> ModelUsageSummary:
        return ModelUsageSummary(
            llm_calls=self.llm_calls, total_input_tokens=self.total_input_tokens,
            total_output_tokens=self.total_output_tokens, total_tokens=self.total_tokens,
            estimated_cost_usd=self.estimated_cost_usd, tool_calls=self.tool_calls_made,
            model_used=self.model_used,
        )


def _collect_identifiers(data: dict, entity_ids: set[str], relationship_ids: set[str]) -> None:
    """Best-effort, schema-light extraction of entity/relationship ids from a
    tool's own validated output payload (never from model text). Looks only
    at shapes the Stage 04 tool library actually produces: an ``entity``/
    ``relationship`` object with an ``id`` field, or ``nodes``/
    ``relationships`` list fields of the same shape."""
    entity = data.get("entity")
    if isinstance(entity, dict) and entity.get("id"):
        entity_ids.add(entity["id"])
    relationship = data.get("relationship")
    if isinstance(relationship, dict) and relationship.get("id"):
        relationship_ids.add(relationship["id"])
    for node in data.get("nodes") or []:
        if isinstance(node, dict) and node.get("id"):
            entity_ids.add(node["id"])
    for rel in data.get("relationships") or []:
        if isinstance(rel, dict) and rel.get("id"):
            relationship_ids.add(rel["id"])
    for path in data.get("paths") or []:
        if isinstance(path, dict):
            _collect_identifiers(path, entity_ids, relationship_ids)


CURRENT_RUN: contextvars.ContextVar[RunState] = contextvars.ContextVar("nodus_agent_current_run")


def current_run() -> RunState:
    try:
        return CURRENT_RUN.get()
    except LookupError as exc:
        raise RuntimeError(
            "No AgentExecutionContext is bound to the current run — GatewayLlm/tool adapters "
            "must only be invoked from within backend.app.agent.investigator.run_investigation()"
        ) from exc
