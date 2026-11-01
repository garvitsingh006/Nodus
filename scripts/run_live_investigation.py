"""ND-13C: one-off script for the first real, live-Gemini-powered coordinated
investigation against the freshly-imported NEXUS data. Not a test, not part
of the suite -- run manually, prints a structured report, never commits or
prints the API key itself.
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv

load_dotenv(dotenv_path=Path(__file__).resolve().parents[1] / ".env", override=True)

from backend.app.agent.contracts import CoordinatorBudget, InvestigationRequest
from backend.app.agent.coordinator import run_coordinated_investigation
from backend.app.config import Settings
from backend.app.gateway.budget import SharedTokenBudgetTracker
from backend.app.gateway.config import GatewayConfig
from backend.app.gateway.gateway import ModelGateway
from backend.app.gateway.gemini_client import GeminiProviderClient
from backend.app.graph.driver import Neo4jStore
from backend.app.security import AuthorizationContext, Permission, Principal
from backend.app.skills.catalog import build_production_skill_registry
from backend.app.tools import register_all_tools
from backend.app.tools.executor import ToolExecutor
from backend.app.tools.registry import ToolDependencies, ToolRegistry


def _report(label: str, result) -> None:
    print(f"\n=== {label} ===")
    print("run_id:", result.run_id)
    print("status:", result.status.value)
    print("verification_status:", result.verification_status)
    print("models_used:", result.models_used)
    print("model_usage:", result.model_usage.model_dump())
    print("entity_ids:", result.entity_ids)
    print("relationship_ids:", result.relationship_ids)
    print("evidence_refs:", result.evidence_refs)
    print("graph_revision:", result.graph_revision)
    print("supported_findings:", result.supported_findings)
    print("potential_effects:", result.potential_effects)
    print("unverified_findings:", result.unverified_findings)
    print("contradictions:", result.contradictions)
    print("stale_findings:", result.stale_findings)
    print("incomplete_work:", result.incomplete_work)
    print("\n--- specialist passes ---")
    for s in result.specialist_results:
        print(f"[{s.role.value}] status={s.status.value} skill={s.skill_used.skill_id if s.skill_used else None} "
             f"model={s.model_used} tool_calls={s.tool_calls_made} tools={s.tool_ids_called} "
             f"llm_calls={s.llm_calls_made}")
        if s.claim_verifications:
            for c in s.claim_verifications:
                print(f"    claim[{c.claim_type.value}] {c.claim_id} -> {c.status.value}: {c.explanation}")
        if s.summary:
            print(f"    summary: {s.summary[:300]}")
    print("\n--- final summary ---")
    print(result.summary)


async def main(principal_label: str, source_ids: tuple[str, ...] | None) -> None:
    settings = Settings.from_env()
    store = Neo4jStore(settings)
    store.start()

    tool_registry = ToolRegistry()
    register_all_tools(tool_registry)
    skill_registry = build_production_skill_registry(tool_registry=tool_registry)
    tool_executor = ToolExecutor(tool_registry, ToolDependencies(graph_store=store))

    gateway_config = GatewayConfig.from_env()
    if not gateway_config.api_key:
        raise SystemExit("GEMINI_API_KEY is not configured")
    client = GeminiProviderClient(api_key=gateway_config.api_key)

    tracker = SharedTokenBudgetTracker()
    budget_key = f"live-investigation:{principal_label}"
    tracker.configure(budget_key, limit=60_000)  # conservative cap on total tokens for this whole run

    gateway = ModelGateway(client, gateway_config.routing, pricing=gateway_config.pricing,
                           retry_policy=gateway_config.retry_policy, budget_tracker=tracker)

    base_permissions = {Permission.READ_GRAPH, Permission.READ_EVIDENCE, Permission.EXECUTE_TOOL}
    if source_ids is None:
        base_permissions.add(Permission.ADMIN)  # full-access run: ADMIN bypasses the NEXUS-tenant source-scope gate
    permissions = frozenset(base_permissions)
    principal = Principal(subject=principal_label, tenant_id="nexus-tenant", workspaces=("nexus-workspace",),
                          permissions=permissions, source_ids=source_ids or ())
    authorization = AuthorizationContext(principal=principal, tenant_id="nexus-tenant",
                                        workspace_id="nexus-workspace", task_id="live-nd13c",
                                        permissions=permissions)

    request = InvestigationRequest(
        question=("Analyze NEX-PR-PAY-003, identify potentially affected services and relevant experts, "
                 "and verify the evidence supporting those findings."),
        entity_id="NEX-PR-PAY-003")

    # ND-13E: IMPACT is now deterministic-first (one Stage 12 tool call + at most one bounded narrative
    # call), so it no longer needs a large time/LLM-call share -- only DISCOVERY's ADK loop does.
    budget = CoordinatorBudget(max_total_tool_calls=16, max_total_llm_calls=14, overall_timeout_seconds=200.0)

    result = await run_coordinated_investigation(
        request, authorization, f"live-{principal_label}", gateway, tool_registry, tool_executor, skill_registry,
        budget=budget, request_budget_key=budget_key)

    _report(f"LIVE INVESTIGATION ({principal_label})", result)
    print("\nremaining shared token budget:", tracker.remaining(budget_key))

    store.close()


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "full"
    if mode == "restricted":
        asyncio.run(main("restricted-tester", source_ids=("github",)))
    else:
        asyncio.run(main("full-access-tester", source_ids=None))
