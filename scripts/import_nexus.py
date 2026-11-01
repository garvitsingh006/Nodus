"""Bootstrap/import the NEXUS 7A-7E dataset into PostgreSQL (governance +
provenance) and Neo4j (permission-aware relationship graph), through the
existing outbox/projector pipeline. Deterministic, idempotent, safe to rerun.

Usage (from repo root):
    .venv/Scripts/python.exe scripts/import_nexus.py
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.app.config import Settings
from backend.app.governance.loader import FOUNDATION_SOURCE_ID, build_import_plan
from backend.app.governance.nexus_connector import NexusFileConnector
from backend.app.governance.store import GovernanceStore
from backend.app.graph.driver import Neo4jStore
from backend.app.graph.repository import GraphRepository
from backend.app.ingestion.connector import ConnectorConfig, ConnectorIdentity
from backend.app.ingestion.postgres_store import PostgresStore
from backend.app.ingestion.sync import SyncCoordinator

DEFAULT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_7A = DEFAULT_ROOT / "nexus_src" / "7A" / "NEXUS_Stage_7A"
DEFAULT_7B = DEFAULT_ROOT / "nexus_src" / "7B" / "NEXUS_Stage_7B"
DEFAULT_7C = DEFAULT_ROOT / "nexus_src" / "7C" / "NEXUS_Stage_7C"
DEFAULT_7D = DEFAULT_ROOT / "nexus_src" / "7D" / "NEXUS_Stage_7D"
DEFAULT_7E = DEFAULT_ROOT / "nexus"

TENANT_ID = "nexus-tenant"
WORKSPACE_ID = "nexus-workspace"
COMPANY_ID = "nexus"


def _write_governance(store: GovernanceStore, plan) -> None:
    with store.transaction() as cur:
        for c in plan.companies:
            cur.execute("""
                INSERT INTO nexus_companies (tenant_id, company_id, company_key, official_name, data)
                VALUES (%s,%s,%s,%s,%s)
                ON CONFLICT (tenant_id, company_id) DO UPDATE SET
                    company_key=EXCLUDED.company_key, official_name=EXCLUDED.official_name, data=EXCLUDED.data
                """, (TENANT_ID, c["company_id"], c["company_key"], c["official_name"], _json(c["data"])))
        for e in plan.employees:
            cur.execute("""
                INSERT INTO nexus_employees (tenant_id, company_id, employee_id, display_name, data)
                VALUES (%s,%s,%s,%s,%s)
                ON CONFLICT (tenant_id, company_id, employee_id) DO UPDATE SET
                    display_name=EXCLUDED.display_name, data=EXCLUDED.data
                """, (TENANT_ID, COMPANY_ID, e["employee_id"], e["display_name"], _json(e["data"])))
        for pa in plan.project_assignments:
            cur.execute("""
                INSERT INTO nexus_project_assignments (tenant_id, company_id, employee_id, project_id, data)
                VALUES (%s,%s,%s,%s,%s)
                ON CONFLICT (tenant_id, company_id, employee_id, project_id) DO UPDATE SET data=EXCLUDED.data
                """, (TENANT_ID, COMPANY_ID, pa["employee_id"], pa["project_id"], _json(pa["data"])))
        for tm in plan.team_memberships:
            cur.execute("""
                INSERT INTO nexus_team_memberships (tenant_id, company_id, employee_id, team_id, data)
                VALUES (%s,%s,%s,%s,%s)
                ON CONFLICT (tenant_id, company_id, employee_id, team_id) DO UPDATE SET data=EXCLUDED.data
                """, (TENANT_ID, COMPANY_ID, tm["employee_id"], tm["team_id"], _json(tm["data"])))
        for role in plan.roles:
            cur.execute("""
                INSERT INTO nexus_roles (tenant_id, company_id, role_id, role_code, data)
                VALUES (%s,%s,%s,%s,%s)
                ON CONFLICT (tenant_id, company_id, role_id) DO UPDATE SET
                    role_code=EXCLUDED.role_code, data=EXCLUDED.data
                """, (TENANT_ID, COMPANY_ID, role["role_id"], role["role_code"], _json(role["data"])))
        for ent in plan.baseline_entitlements:
            cur.execute("""
                INSERT INTO nexus_baseline_entitlements
                    (tenant_id, company_id, entitlement_id, role_id, resource_class, baseline_decision, scope,
                     assigned_project_required, explicit_resource_grant_required, data)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (tenant_id, company_id, entitlement_id) DO UPDATE SET
                    role_id=EXCLUDED.role_id, resource_class=EXCLUDED.resource_class,
                    baseline_decision=EXCLUDED.baseline_decision, scope=EXCLUDED.scope,
                    assigned_project_required=EXCLUDED.assigned_project_required,
                    explicit_resource_grant_required=EXCLUDED.explicit_resource_grant_required, data=EXCLUDED.data
                """, (TENANT_ID, COMPANY_ID, ent["entitlement_id"], ent["role_id"], ent["resource_class"],
                      ent["baseline_decision"], ent["scope"], ent["assigned_project_required"],
                      ent["explicit_resource_grant_required"], _json(ent["data"])))
        for demo in plan.demo_identities:
            cur.execute("""
                INSERT INTO nexus_demo_identities
                    (tenant_id, company_id, demo_identity_id, demo_token, employee_id, role_id, data)
                VALUES (%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (tenant_id, company_id, demo_identity_id) DO UPDATE SET
                    demo_token=EXCLUDED.demo_token, employee_id=EXCLUDED.employee_id,
                    role_id=EXCLUDED.role_id, data=EXCLUDED.data
                """, (TENANT_ID, COMPANY_ID, demo["demo_identity_id"], demo["demo_token"],
                      demo["employee_id"], demo["role_id"], _json(demo["data"])))
        for src in plan.sources:
            cur.execute("""
                INSERT INTO nexus_sources
                    (tenant_id, company_id, source_id, stage, classification, owner_project_id, selectable,
                     eligible_role_codes, data)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (tenant_id, company_id, source_id) DO UPDATE SET
                    stage=EXCLUDED.stage, classification=EXCLUDED.classification,
                    owner_project_id=EXCLUDED.owner_project_id, selectable=EXCLUDED.selectable,
                    eligible_role_codes=EXCLUDED.eligible_role_codes, data=EXCLUDED.data
                """, (TENANT_ID, COMPANY_ID, src["source_id"], src["stage"], src["classification"],
                      src["owner_project_id"], src["selectable"], src["eligible_role_codes"], _json(src["data"])))
        # Foundation source: not one of the 28, always selectable/shared.
        cur.execute("""
            INSERT INTO nexus_sources
                (tenant_id, company_id, source_id, stage, classification, owner_project_id, selectable,
                 eligible_role_codes, data)
            VALUES (%s,%s,%s,'7A','FOUNDATIONAL',NULL,false,%s,%s)
            ON CONFLICT (tenant_id, company_id, source_id) DO NOTHING
            """, (TENANT_ID, COMPANY_ID, FOUNDATION_SOURCE_ID,
                  ["ADMIN", "DEVELOPER", "INTERN"], _json({"note": "Always-visible company foundation scaffold"})))
        for res in plan.resource_access_catalog:
            cur.execute("""
                INSERT INTO nexus_resource_access_catalog
                    (tenant_id, company_id, resource_id, access_classification, owner_project_id, data)
                VALUES (%s,%s,%s,%s,%s,%s)
                ON CONFLICT (tenant_id, company_id, resource_id) DO UPDATE SET
                    access_classification=EXCLUDED.access_classification,
                    owner_project_id=EXCLUDED.owner_project_id, data=EXCLUDED.data
                """, (TENANT_ID, COMPANY_ID, res["resource_id"], res["access_classification"],
                      res["owner_project_id"], _json(res["data"])))
        for grant in plan.resource_grants:
            cur.execute("""
                INSERT INTO nexus_resource_grants
                    (tenant_id, company_id, grant_id, employee_id, resource_id, scope_project_id,
                     permission_level, status, valid_from, expires_at, revoked_at,
                     administrative_decision_id, data)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (tenant_id, company_id, grant_id) DO UPDATE SET
                    employee_id=EXCLUDED.employee_id, resource_id=EXCLUDED.resource_id,
                    scope_project_id=EXCLUDED.scope_project_id, permission_level=EXCLUDED.permission_level,
                    status=EXCLUDED.status, valid_from=EXCLUDED.valid_from, expires_at=EXCLUDED.expires_at,
                    revoked_at=EXCLUDED.revoked_at,
                    administrative_decision_id=EXCLUDED.administrative_decision_id, data=EXCLUDED.data
                """, (TENANT_ID, COMPANY_ID, grant["grant_id"], grant["employee_id"], grant["resource_id"],
                      grant["scope_project_id"], grant["permission_level"], grant["status"], grant["valid_from"],
                      grant["expires_at"], grant["revoked_at"], grant["administrative_decision_id"],
                      _json(grant["data"])))
        for req in plan.access_requests:
            cur.execute("""
                INSERT INTO nexus_access_requests
                    (tenant_id, company_id, request_id, employee_id, resource_id, status, data)
                VALUES (%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (tenant_id, company_id, request_id) DO UPDATE SET
                    employee_id=EXCLUDED.employee_id, resource_id=EXCLUDED.resource_id,
                    status=EXCLUDED.status, data=EXCLUDED.data
                """, (TENANT_ID, COMPANY_ID, req["request_id"], req["employee_id"], req["resource_id"],
                      req["status"], _json(req["data"])))
        for dec in plan.access_decisions:
            cur.execute("""
                INSERT INTO nexus_access_decisions
                    (tenant_id, company_id, decision_id, request_id, grant_id,
                     decision_actor_employee_id, data)
                VALUES (%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (tenant_id, company_id, decision_id) DO UPDATE SET
                    request_id=EXCLUDED.request_id, grant_id=EXCLUDED.grant_id,
                    decision_actor_employee_id=EXCLUDED.decision_actor_employee_id, data=EXCLUDED.data
                """, (TENANT_ID, COMPANY_ID, dec["decision_id"], dec["request_id"], dec["grant_id"],
                      dec["decision_actor_employee_id"], _json(dec["data"])))
        for disc in plan.restricted_disclosures:
            cur.execute("""
                INSERT INTO nexus_restricted_disclosures
                    (tenant_id, company_id, disclosure_id, recipient_employee_id, data)
                VALUES (%s,%s,%s,%s,%s)
                ON CONFLICT (tenant_id, company_id, disclosure_id) DO UPDATE SET
                    recipient_employee_id=EXCLUDED.recipient_employee_id, data=EXCLUDED.data
                """, (TENANT_ID, COMPANY_ID, disc["disclosure_id"], disc["recipient_employee_id"],
                      _json(disc["data"])))
        for event in plan.administrative_audit_events:
            cur.execute("""
                INSERT INTO nexus_administrative_audit_events
                    (tenant_id, company_id, event_id, actor_employee_id, occurred_at, data)
                VALUES (%s,%s,%s,%s,%s,%s)
                ON CONFLICT (tenant_id, company_id, event_id) DO UPDATE SET
                    actor_employee_id=EXCLUDED.actor_employee_id, occurred_at=EXCLUDED.occurred_at,
                    data=EXCLUDED.data
                """, (TENANT_ID, COMPANY_ID, event["event_id"], event["actor_employee_id"],
                      event["occurred_at"], _json(event["data"])))
        for policy in plan.governance_policies:
            cur.execute("""
                INSERT INTO nexus_governance_policies (tenant_id, company_id, policy_id, data)
                VALUES (%s,%s,%s,%s)
                ON CONFLICT (tenant_id, company_id, policy_id) DO UPDATE SET data=EXCLUDED.data
                """, (TENANT_ID, COMPANY_ID, policy["policy_id"], _json(policy["data"])))
        for row in plan.records:
            cur.execute("""
                INSERT INTO nexus_records
                    (tenant_id, company_id, collection, record_id, source_id, stage, data)
                VALUES (%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (tenant_id, company_id, collection, record_id) DO UPDATE SET
                    source_id=EXCLUDED.source_id, stage=EXCLUDED.stage, data=EXCLUDED.data
                """, (TENANT_ID, COMPANY_ID, row.collection, row.record_id, row.source_id, row.stage,
                      _json(row.data)))


def _json(value) -> str:
    import json
    return json.dumps(value, default=str)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--stage-7a", type=Path, default=DEFAULT_7A)
    parser.add_argument("--stage-7b", type=Path, default=DEFAULT_7B)
    parser.add_argument("--stage-7c", type=Path, default=DEFAULT_7C)
    parser.add_argument("--stage-7d", type=Path, default=DEFAULT_7D)
    parser.add_argument("--stage-7e", type=Path, default=DEFAULT_7E)
    parser.add_argument("--tenant-id", default=TENANT_ID)
    parser.add_argument("--workspace-id", default=WORKSPACE_ID)
    args = parser.parse_args()

    settings = Settings.from_env()
    if not settings.postgres_dsn:
        raise SystemExit("POSTGRES_DSN must be set to import NEXUS")

    governance = GovernanceStore(settings.postgres_dsn)
    governance.start()
    ingestion_store = PostgresStore(settings.postgres_dsn)
    ingestion_store.start()

    plan = build_import_plan(args.stage_7a, args.stage_7b, args.stage_7c, args.stage_7d, args.stage_7e)
    print(f"Parsed {len(plan.records)} records across {len(plan.source_records)} sources "
          f"({len(plan.sources)} selectable + 1 foundation).")

    _write_governance(governance, plan)
    print(f"Governance rows written: companies={len(plan.companies)} employees={len(plan.employees)} "
          f"sources={len(plan.sources) + 1} grants={len(plan.resource_grants)} "
          f"records={len(plan.records)}")

    neo4j_store = Neo4jStore(settings)
    neo4j_store.start()
    try:
        graph = GraphRepository(neo4j_store, args.tenant_id, args.workspace_id, "nexus-importer", allow_all=True,
                                can_evidence=True)
        coordinator = SyncCoordinator(ingestion_store, graph)
        connector = NexusFileConnector(args.stage_7a, args.stage_7b, args.stage_7c, args.stage_7d, args.stage_7e)

        source_ids = [FOUNDATION_SOURCE_ID] + [s["source_id"] for s in plan.sources]
        total_seen = total_changed = 0
        for source_id in source_ids:
            config = ConnectorConfig(
                tenant_id=args.tenant_id, workspace_id=args.workspace_id,
                identity=ConnectorIdentity(connector_id=f"nexus-{source_id}", source_id=source_id,
                                           kind="nexus_file"))
            result = coordinator.run(connector, config)
            total_seen += result["records_seen"]
            total_changed += result["records_changed"]
        print(f"Neo4j projection: {len(source_ids)} sources, {total_seen} records seen, "
              f"{total_changed} changed this run.")
    finally:
        neo4j_store.close()


if __name__ == "__main__":
    main()
