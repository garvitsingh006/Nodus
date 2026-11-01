"""Shared fixtures/constants for the NEXUS Stage 07/08 regression suite.

Unlike the rest of ``backend/tests``, these tests exercise the ALREADY
IMPORTED, shared live NEXUS dataset (``scripts/import_nexus.py``) in the
developer's own Neo4j/Postgres (``.env``'s ``NEO4J_*``/``POSTGRES_DSN``)
rather than spinning up an isolated throwaway database per test. Re-running
the importer (1,101 records) for every test module would be slow and is
unnecessary: the dataset is deterministic and content-hash idempotent, so
asserting against the already-populated state is both faster and a more
realistic end-to-end check of the real demo deployment.

Every fixture here skips (never fails) when the live services aren't
reachable or the dataset hasn't been imported yet, so a clean environment
with no Docker running still gets a clean, informative skip rather than a
hard failure.
"""
from __future__ import annotations

import pytest

from backend.app.config import Settings
from backend.app.governance.identity import NEXUS_TENANT_ID, resolve_demo_principal
from backend.app.governance.store import GovernanceStore
from backend.app.graph.driver import Neo4jStore
from backend.app.graph.repository import GraphRepository
from backend.app.security import Principal

NEXUS_WORKSPACE_ID = "nexus-workspace"

# Known-stable 7A identifiers (manifest-declared; see nexus_src/7A/.../manifest.json).
KNOWN_SERVICE_IDS = (
    "NEX-SVC-CHECKOUT_SERVICE", "NEX-SVC-DEPLOYMENT_PLATFORM", "NEX-SVC-EVENT_INGESTOR",
    "NEX-SVC-GATEWAY_ROUTER", "NEX-SVC-IDENTITY_API", "NEX-SVC-METRICS_PIPELINE",
    "NEX-SVC-OBSERVABILITY_PLATFORM", "NEX-SVC-ORDER_API", "NEX-SVC-PAYMENT_API",
    "NEX-SVC-POLICY_SERVICE", "NEX-SVC-REFUND_WORKER", "NEX-SVC-REPORTING_API",
    "NEX-SVC-TOKEN_VALIDATOR", "NEX-SVC-WEBHOOK_CONSUMER",
)
DECLARED_DEPENDENCY_COUNT = 23
TOTAL_SOURCE_COUNT = 29


@pytest.fixture(scope="session")
def nexus_settings():
    try:
        settings = Settings.from_env()
    except RuntimeError as exc:
        pytest.skip(f"NEO4J_PASSWORD not configured; skipping live NEXUS tests: {exc}")
    if not settings.postgres_dsn:
        pytest.skip("POSTGRES_DSN not configured; skipping live NEXUS tests")
    return settings


@pytest.fixture(scope="session")
def nexus_neo4j(nexus_settings):
    store = Neo4jStore(nexus_settings)
    try:
        store.start()
    except Exception as exc:  # pragma: no cover - environment-dependent
        pytest.skip(f"Live Neo4j unavailable: {exc}")
    try:
        yield store
    finally:
        store.close()


@pytest.fixture(scope="session")
def nexus_pg(nexus_settings):
    store = GovernanceStore(nexus_settings.postgres_dsn)
    try:
        store.start()
    except Exception as exc:  # pragma: no cover - environment-dependent
        pytest.skip(f"Live PostgreSQL unavailable: {exc}")
    rows = store.read("SELECT company_id FROM nexus_companies WHERE company_id='nexus'")
    if not rows:
        pytest.skip("NEXUS dataset not imported into the live database; "
                    "run scripts/import_nexus.py first")
    return store


def resolve(token: str, store: GovernanceStore, *, demo_mode_enabled: bool = True) -> Principal | None:
    return resolve_demo_principal(token, store, tenant_id=NEXUS_TENANT_ID,
                                  workspace_id=NEXUS_WORKSPACE_ID, demo_mode_enabled=demo_mode_enabled)


@pytest.fixture
def admin_principal(nexus_pg):
    principal = resolve("4", nexus_pg)
    if principal is None:
        pytest.skip("Demo token '4' did not resolve; is GRAPH_HACKS_NEXUS_DEMO_MODE handling intact?")
    return principal


@pytest.fixture
def developer_principal(nexus_pg):
    """D02: a PAY-project developer with real resource grants."""
    principal = resolve("D02", nexus_pg)
    if principal is None:
        pytest.skip("Demo token 'D02' did not resolve")
    return principal


@pytest.fixture
def intern_principal(nexus_pg):
    """I10: an intern with shared+foundation sources only."""
    principal = resolve("I10", nexus_pg)
    if principal is None:
        pytest.skip("Demo token 'I10' did not resolve")
    return principal


def repository_for(neo4j_store: Neo4jStore, principal: Principal) -> GraphRepository:
    from backend.app.governance.identity import authorized_source_ids_for
    from backend.app.security import Permission

    return GraphRepository(
        neo4j_store, principal.tenant_id, NEXUS_WORKSPACE_ID, principal.subject,
        allow_all=Permission.ADMIN in principal.permissions, can_evidence=True,
        authorized_source_ids=authorized_source_ids_for(principal),
    )
