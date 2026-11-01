"""NEXUS company/identity/governance persistence: company, project, team,
employee, role, source-registry and resource-grant state. This is NODUS Stage
09/Stage 07 company-domain data — distinct from ``backend.app.ingestion``
(connector/outbox/sync metadata) and from Neo4j (the permission-aware
relationship graph). Nothing here duplicates a graph relationship; this store
is the authoritative source for *who can see what*, which the graph/tool
layer consults before any traversal or evidence read.

Most record detail is kept as verbatim JSONB in ``nexus_records`` (so original
source content and provenance are always retrievable unmodified); a handful
of tables get real columns because the authorization hot path
(``backend.app.governance.identity``) needs to filter/join on them.
"""
from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

import psycopg
from psycopg.rows import dict_row

SCHEMA = (
    # Verbatim record store: every 7A-7E record, keyed by its own stable ID,
    # for provenance/original-evidence retrieval. `collection` is the source
    # file's top-level collection name (e.g. "employees", "resource_grants").
    """
    CREATE TABLE IF NOT EXISTS nexus_records (
        tenant_id TEXT NOT NULL,
        company_id TEXT NOT NULL,
        collection TEXT NOT NULL,
        record_id TEXT NOT NULL,
        source_id TEXT,
        stage TEXT NOT NULL,
        data JSONB NOT NULL,
        imported_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        PRIMARY KEY (tenant_id, company_id, collection, record_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS nexus_records_by_source "
    "ON nexus_records (tenant_id, company_id, source_id)",
    # File-level idempotency/change-detection for the bootstrap importer,
    # complementing the ingestion pipeline's per-record content hash.
    """
    CREATE TABLE IF NOT EXISTS nexus_import_state (
        tenant_id TEXT NOT NULL,
        company_id TEXT NOT NULL,
        stage TEXT NOT NULL,
        filename TEXT NOT NULL,
        sha256 TEXT NOT NULL,
        record_count INT NOT NULL,
        imported_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        PRIMARY KEY (tenant_id, company_id, stage, filename)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS nexus_companies (
        tenant_id TEXT NOT NULL,
        company_id TEXT NOT NULL,
        company_key TEXT NOT NULL,
        official_name TEXT NOT NULL,
        data JSONB NOT NULL,
        PRIMARY KEY (tenant_id, company_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS nexus_employees (
        tenant_id TEXT NOT NULL,
        company_id TEXT NOT NULL,
        employee_id TEXT NOT NULL,
        display_name TEXT NOT NULL,
        data JSONB NOT NULL,
        PRIMARY KEY (tenant_id, company_id, employee_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS nexus_project_assignments (
        tenant_id TEXT NOT NULL,
        company_id TEXT NOT NULL,
        employee_id TEXT NOT NULL,
        project_id TEXT NOT NULL,
        data JSONB NOT NULL DEFAULT '{}'::jsonb,
        PRIMARY KEY (tenant_id, company_id, employee_id, project_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS nexus_project_assignments_by_project "
    "ON nexus_project_assignments (tenant_id, company_id, project_id)",
    """
    CREATE TABLE IF NOT EXISTS nexus_team_memberships (
        tenant_id TEXT NOT NULL,
        company_id TEXT NOT NULL,
        employee_id TEXT NOT NULL,
        team_id TEXT NOT NULL,
        data JSONB NOT NULL DEFAULT '{}'::jsonb,
        PRIMARY KEY (tenant_id, company_id, employee_id, team_id)
    )
    """,
    # Source registry: the 28 selectable sources plus the non-selectable
    # company-foundation scaffold. `classification` is one of
    # COMPANY_SHARED, PROJECT_DEVELOPER, CROSS_PROJECT_CONFIDENTIAL,
    # ADMINISTRATIVE_RECORDS, FOUNDATIONAL. `eligible_role_codes` mirrors the
    # 7B/7C/7D manifests' declared role eligibility for this source's
    # baseline entitlement (narrower checks still require a resource grant).
    """
    CREATE TABLE IF NOT EXISTS nexus_sources (
        tenant_id TEXT NOT NULL,
        company_id TEXT NOT NULL,
        source_id TEXT NOT NULL,
        stage TEXT NOT NULL,
        classification TEXT NOT NULL,
        owner_project_id TEXT,
        selectable BOOLEAN NOT NULL DEFAULT true,
        eligible_role_codes TEXT[] NOT NULL DEFAULT '{}',
        data JSONB NOT NULL,
        PRIMARY KEY (tenant_id, company_id, source_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS nexus_roles (
        tenant_id TEXT NOT NULL,
        company_id TEXT NOT NULL,
        role_id TEXT NOT NULL,
        role_code TEXT NOT NULL,
        data JSONB NOT NULL,
        PRIMARY KEY (tenant_id, company_id, role_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS nexus_baseline_entitlements (
        tenant_id TEXT NOT NULL,
        company_id TEXT NOT NULL,
        entitlement_id TEXT NOT NULL,
        role_id TEXT NOT NULL,
        resource_class TEXT NOT NULL,
        baseline_decision TEXT NOT NULL,
        scope TEXT NOT NULL,
        assigned_project_required BOOLEAN NOT NULL DEFAULT false,
        explicit_resource_grant_required BOOLEAN NOT NULL DEFAULT false,
        data JSONB NOT NULL,
        PRIMARY KEY (tenant_id, company_id, entitlement_id)
    )
    """,
    # Demo identities: non-secret demo-token selectors (token "4" = admin),
    # gated entirely by server-side isolated-demo-mode configuration — see
    # backend.app.governance.identity. Never treated as a production secret.
    """
    CREATE TABLE IF NOT EXISTS nexus_demo_identities (
        tenant_id TEXT NOT NULL,
        company_id TEXT NOT NULL,
        demo_identity_id TEXT NOT NULL,
        demo_token TEXT NOT NULL,
        employee_id TEXT NOT NULL,
        role_id TEXT NOT NULL,
        data JSONB NOT NULL,
        PRIMARY KEY (tenant_id, company_id, demo_identity_id),
        UNIQUE (tenant_id, company_id, demo_token)
    )
    """,
    # Resource access catalog: the 28-source-plus entries describing each
    # protected resource's classification and owning scope.
    """
    CREATE TABLE IF NOT EXISTS nexus_resource_access_catalog (
        tenant_id TEXT NOT NULL,
        company_id TEXT NOT NULL,
        resource_id TEXT NOT NULL,
        access_classification TEXT NOT NULL,
        owner_project_id TEXT,
        data JSONB NOT NULL,
        PRIMARY KEY (tenant_id, company_id, resource_id)
    )
    """,
    # Resource grants: the authoritative, time-bounded, revocable permission
    # ledger. Status/validity columns are real (not JSONB-only) because
    # effective-access checks run on every authorized read.
    """
    CREATE TABLE IF NOT EXISTS nexus_resource_grants (
        tenant_id TEXT NOT NULL,
        company_id TEXT NOT NULL,
        grant_id TEXT NOT NULL,
        employee_id TEXT NOT NULL,
        resource_id TEXT NOT NULL,
        scope_project_id TEXT,
        permission_level TEXT,
        status TEXT NOT NULL,
        valid_from TIMESTAMPTZ,
        expires_at TIMESTAMPTZ,
        revoked_at TIMESTAMPTZ,
        administrative_decision_id TEXT,
        data JSONB NOT NULL,
        PRIMARY KEY (tenant_id, company_id, grant_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS nexus_resource_grants_by_employee "
    "ON nexus_resource_grants (tenant_id, company_id, employee_id, resource_id)",
    """
    CREATE TABLE IF NOT EXISTS nexus_access_requests (
        tenant_id TEXT NOT NULL,
        company_id TEXT NOT NULL,
        request_id TEXT NOT NULL,
        employee_id TEXT,
        resource_id TEXT,
        status TEXT,
        data JSONB NOT NULL,
        PRIMARY KEY (tenant_id, company_id, request_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS nexus_access_decisions (
        tenant_id TEXT NOT NULL,
        company_id TEXT NOT NULL,
        decision_id TEXT NOT NULL,
        request_id TEXT,
        grant_id TEXT,
        decision_actor_employee_id TEXT,
        data JSONB NOT NULL,
        PRIMARY KEY (tenant_id, company_id, decision_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS nexus_restricted_disclosures (
        tenant_id TEXT NOT NULL,
        company_id TEXT NOT NULL,
        disclosure_id TEXT NOT NULL,
        recipient_employee_id TEXT,
        data JSONB NOT NULL,
        PRIMARY KEY (tenant_id, company_id, disclosure_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS nexus_administrative_audit_events (
        tenant_id TEXT NOT NULL,
        company_id TEXT NOT NULL,
        event_id TEXT NOT NULL,
        actor_employee_id TEXT,
        occurred_at TIMESTAMPTZ,
        data JSONB NOT NULL,
        PRIMARY KEY (tenant_id, company_id, event_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS nexus_governance_policies (
        tenant_id TEXT NOT NULL,
        company_id TEXT NOT NULL,
        policy_id TEXT NOT NULL,
        data JSONB NOT NULL,
        PRIMARY KEY (tenant_id, company_id, policy_id)
    )
    """,
)


class GovernanceStore:
    """Mirrors ``backend.app.ingestion.postgres_store.PostgresStore``'s
    connection style exactly (one connection per unit of work, no pool)."""

    CONNECT_TIMEOUT_SECONDS = 10

    def __init__(self, dsn: str):
        if not dsn:
            raise ValueError("A PostgreSQL DSN is required")
        self.dsn = dsn

    def start(self) -> None:
        with psycopg.connect(self.dsn, autocommit=True, connect_timeout=self.CONNECT_TIMEOUT_SECONDS) as conn:
            with conn.cursor() as cur:
                for statement in SCHEMA:
                    cur.execute(statement)

    @contextmanager
    def transaction(self) -> Iterator[psycopg.Cursor]:
        with psycopg.connect(self.dsn, row_factory=dict_row,
                             connect_timeout=self.CONNECT_TIMEOUT_SECONDS) as conn:
            with conn.transaction():
                with conn.cursor() as cur:
                    yield cur

    def read(self, sql: str, params: tuple = ()) -> list[dict]:
        with psycopg.connect(self.dsn, row_factory=dict_row, autocommit=True,
                             connect_timeout=self.CONNECT_TIMEOUT_SECONDS) as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                return cur.fetchall()
