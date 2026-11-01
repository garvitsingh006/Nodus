"""Durable ingestion-state persistence: connector configs, sync runs,
checkpoints, per-record fingerprints and the transactional outbox. This store
owns connector/sync metadata only — canonical graph facts stay in Neo4j
(``backend.app.graph.repository``); nothing here duplicates a graph
relationship.
"""
from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

import psycopg
from psycopg.rows import dict_row

SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS connector_configs (
        tenant_id TEXT NOT NULL,
        workspace_id TEXT NOT NULL,
        connector_id TEXT NOT NULL,
        source_id TEXT NOT NULL,
        kind TEXT NOT NULL,
        settings JSONB NOT NULL DEFAULT '{}'::jsonb,
        enabled BOOLEAN NOT NULL DEFAULT true,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        PRIMARY KEY (tenant_id, workspace_id, connector_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS sync_checkpoints (
        tenant_id TEXT NOT NULL,
        workspace_id TEXT NOT NULL,
        connector_id TEXT NOT NULL,
        cursor TEXT,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        PRIMARY KEY (tenant_id, workspace_id, connector_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS sync_runs (
        run_id TEXT PRIMARY KEY,
        tenant_id TEXT NOT NULL,
        workspace_id TEXT NOT NULL,
        connector_id TEXT NOT NULL,
        started_at TIMESTAMPTZ NOT NULL,
        finished_at TIMESTAMPTZ,
        status TEXT NOT NULL,
        records_seen INT NOT NULL DEFAULT 0,
        records_changed INT NOT NULL DEFAULT 0,
        retryable BOOLEAN,
        error TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS source_record_state (
        tenant_id TEXT NOT NULL,
        workspace_id TEXT NOT NULL,
        source_id TEXT NOT NULL,
        record_id TEXT NOT NULL,
        content_hash TEXT NOT NULL,
        revision INT NOT NULL,
        active BOOLEAN NOT NULL DEFAULT true,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        PRIMARY KEY (tenant_id, workspace_id, source_id, record_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS outbox_events (
        event_id TEXT PRIMARY KEY,
        tenant_id TEXT NOT NULL,
        workspace_id TEXT NOT NULL,
        connector_id TEXT NOT NULL,
        aggregate_id TEXT NOT NULL,
        aggregate_revision INT NOT NULL,
        event_type TEXT NOT NULL,
        idempotency_key TEXT NOT NULL,
        occurred_at TIMESTAMPTZ NOT NULL,
        payload JSONB NOT NULL,
        status TEXT NOT NULL DEFAULT 'PENDING',
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        delivered_at TIMESTAMPTZ,
        UNIQUE (tenant_id, idempotency_key)
    )
    """,
    # Lease-based claiming (G3/G4: bounded concurrent processing, safe
    # recovery after worker termination) and retry/quarantine tracking,
    # added without disturbing any existing column or row.
    "ALTER TABLE outbox_events ADD COLUMN IF NOT EXISTS claimed_by TEXT",
    "ALTER TABLE outbox_events ADD COLUMN IF NOT EXISTS claimed_at TIMESTAMPTZ",
    "ALTER TABLE outbox_events ADD COLUMN IF NOT EXISTS lease_expires_at TIMESTAMPTZ",
    "ALTER TABLE outbox_events ADD COLUMN IF NOT EXISTS attempts INT NOT NULL DEFAULT 0",
    "ALTER TABLE outbox_events ADD COLUMN IF NOT EXISTS last_error TEXT",
    "CREATE INDEX IF NOT EXISTS outbox_pending_by_aggregate ON outbox_events "
    "(tenant_id, workspace_id, connector_id, aggregate_id, aggregate_revision) "
    "WHERE status IN ('PENDING', 'CLAIMED')",
    """
    CREATE TABLE IF NOT EXISTS projection_watermarks (
        projector TEXT NOT NULL,
        tenant_id TEXT NOT NULL,
        aggregate_id TEXT NOT NULL,
        revision INT NOT NULL,
        last_event_id TEXT,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        PRIMARY KEY (projector, tenant_id, aggregate_id)
    )
    """,
)


class PostgresStore:
    """One connection per unit of work; no connection pool yet (out of scope
    for GH-004A — the API process is single-worker today, same as Neo4jStore)."""

    def __init__(self, dsn: str):
        if not dsn:
            raise ValueError("A PostgreSQL DSN is required")
        self.dsn = dsn

    # Bounds how long a connection attempt can hang when PostgreSQL is
    # unavailable (GH-005C): without this, a stalled TCP handshake can run far
    # longer than any ToolExecutor-level wait, leaving an orphaned thread
    # behind it. This is the actual enforceable deadline for Postgres-backed
    # tools (e.g. ``source.coverage``) — the executor's own timeout is only a
    # safety margin on top of it, not a substitute.
    CONNECT_TIMEOUT_SECONDS = 10

    def start(self) -> None:
        with psycopg.connect(self.dsn, autocommit=True, connect_timeout=self.CONNECT_TIMEOUT_SECONDS) as conn:
            with conn.cursor() as cur:
                for statement in SCHEMA:
                    cur.execute(statement)

    @contextmanager
    def transaction(self) -> Iterator[psycopg.Cursor]:
        """Yields a dict-row cursor; commits on success, rolls back on any
        exception (including a ``ConnectorError``) via ``psycopg``'s own
        transaction context manager."""
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
