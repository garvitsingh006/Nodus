"""NODUS Stage 14: durable PostgreSQL-backed investigation run/checkpoint
persistence (ND-14A), plus access-aware pauses, human handoff ownership
transfer, and lifecycle provenance (ND-14B).

Mirrors ``backend.app.governance.store.GovernanceStore``'s connection/
transaction conventions exactly (one connection per unit of work, no pool,
``CREATE TABLE IF NOT EXISTS`` schema, a transactional cursor helper) — this
is not a new persistence abstraction, just the same one applied to a new
table. Claim/resume and handoff-decision locking mirror
``backend.app.ingestion.projector``'s existing outbox-claim pattern
(``SELECT ... FOR UPDATE`` plus a lease expiry), not a new concurrency
primitive. Stage 09's own access-request/approval lifecycle
(``backend.app.governance.access_requests``) is reused unmodified — a run
pausing for access does not duplicate it; approving a request already makes
``refresh_principal_source_ids``/``refresh_authorization`` see the new grant
on the very next resume attempt, with zero changes needed here.

Only structured references and compact, already-validated ``SpecialistResult``
dumps are ever persisted here — never API keys/bearer tokens, never a raw
model conversation, never unrestricted source records. A ``SpecialistResult``
already carries only evidence *references* (ids), not evidence content.
``principal_subject`` is the run's immutable original creator (ND-14B section
4); ``assignee_subject`` is the current owner, transferred only through
``request_handoff``/``decide_handoff``, never by resuming or checkpointing.
"""
from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import datetime, timezone
from uuid import uuid4
from typing import Any, Iterator

import psycopg
from psycopg.rows import dict_row

from backend.app.agent.contracts import CoordinatorBudget, SpecialistResult, SpecialistRole

SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS nodus_investigation_runs (
        run_id TEXT PRIMARY KEY,
        tenant_id TEXT NOT NULL,
        workspace_id TEXT NOT NULL,
        principal_subject TEXT NOT NULL,
        objective TEXT NOT NULL,
        entity_id TEXT,
        status TEXT NOT NULL DEFAULT 'RUNNING',
        result_status TEXT,
        claimed_by TEXT,
        claimed_at TIMESTAMPTZ,
        lease_expires_at TIMESTAMPTZ,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        completed_at TIMESTAMPTZ,
        failure_reason TEXT,
        budget_json JSONB NOT NULL,
        usage_json JSONB NOT NULL DEFAULT '{}'::jsonb,
        graph_revision INT
    )
    """,
    # ND-14B: the current owner, distinct from the immutable creator above. Added via ALTER so an
    # already-deployed ND-14A table picks it up too (same convention as ingestion/postgres_store.py).
    "ALTER TABLE nodus_investigation_runs ADD COLUMN IF NOT EXISTS assignee_subject TEXT",
    "UPDATE nodus_investigation_runs SET assignee_subject = principal_subject WHERE assignee_subject IS NULL",
    # (run_id, phase) as the primary key is what makes ``save_checkpoint`` idempotent: repeating the same
    # phase's write is an UPDATE of the same row, never a second row (ND-14A section 3).
    """
    CREATE TABLE IF NOT EXISTS nodus_investigation_checkpoints (
        run_id TEXT NOT NULL REFERENCES nodus_investigation_runs(run_id),
        phase TEXT NOT NULL,
        specialist_result_json JSONB NOT NULL,
        graph_revision INT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        PRIMARY KEY (run_id, phase)
    )
    """,
    # ND-14B: one human-to-human ownership-transfer request. A fresh PENDING row per request -- a prior
    # ACCEPTED/DECLINED handoff is historical provenance, never overwritten.
    """
    CREATE TABLE IF NOT EXISTS nodus_investigation_handoffs (
        handoff_id TEXT PRIMARY KEY,
        run_id TEXT NOT NULL REFERENCES nodus_investigation_runs(run_id),
        sender_subject TEXT NOT NULL,
        recipient_subject TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'PENDING',
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        decided_at TIMESTAMPTZ
    )
    """,
    # ND-14B section 8: append-only lifecycle provenance. Never updated or deleted once written.
    """
    CREATE TABLE IF NOT EXISTS nodus_investigation_events (
        event_id TEXT PRIMARY KEY,
        run_id TEXT NOT NULL REFERENCES nodus_investigation_runs(run_id),
        event_type TEXT NOT NULL,
        actor_subject TEXT NOT NULL,
        occurred_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        reference_id TEXT,
        resulting_state TEXT
    )
    """,
    # ND-14C: a durable history of VERIFICATION results, one row per distinct graph revision a
    # re-verification was actually performed against. The UNIQUE(run_id, graph_revision) constraint is
    # the idempotency guarantee (section 7): processing the same graph revision twice never creates a
    # second row for it. Prior rows are marked SUPERSEDED, never deleted or overwritten -- historical
    # verification evidence stays queryable (section 2).
    """
    CREATE TABLE IF NOT EXISTS nodus_verification_revisions (
        revision_id TEXT PRIMARY KEY,
        run_id TEXT NOT NULL REFERENCES nodus_investigation_runs(run_id),
        revision_number INT NOT NULL,
        specialist_result_json JSONB NOT NULL,
        graph_revision INT,
        freshness_status TEXT NOT NULL DEFAULT 'CURRENT',
        trigger_reason TEXT,
        actor_subject TEXT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        superseded_at TIMESTAMPTZ,
        UNIQUE (run_id, graph_revision)
    )
    """,
)


class HandoffConflict(ValueError):
    """A handoff could not be decided as requested — already decided by a concurrent/duplicate call,
    the caller is not its recipient, or it no longer matches the run's current owner. One generic
    exception for every reason, same spirit as ``RunClaimDenied``."""

# A worker crashing mid-phase must not permanently lock a run; a stale (expired) lease is reclaimable by
# another resume attempt, same spirit as the outbox's own lease_expires_at column.
_LEASE_SECONDS = 600


class RunClaimDenied(ValueError):
    """Resume could not claim this run. Deliberately the ONE exception for every denial reason — wrong
    tenant, wrong principal, already actively claimed, already completed, or the run simply does not
    exist — so a caller can never distinguish "not yours" from "doesn't exist" by probing run ids
    (ND-14A section 6: preserve generic denial messages)."""


class InvestigationStore:
    """Server-owned; never constructed from caller-supplied data. One connection per operation, exactly
    like ``GovernanceStore``."""

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
    def _transaction(self) -> Iterator[psycopg.Cursor]:
        with psycopg.connect(self.dsn, row_factory=dict_row,
                             connect_timeout=self.CONNECT_TIMEOUT_SECONDS) as conn:
            with conn.transaction():
                with conn.cursor() as cur:
                    yield cur

    def create_run(self, *, run_id: str, tenant_id: str, workspace_id: str, principal_subject: str,
                   objective: str, entity_id: str | None, budget: CoordinatorBudget) -> None:
        """Idempotent: a resumed/retried caller re-creating the same ``run_id`` never overwrites an
        already-in-progress run's own budget/identity. The creator starts as its own assignee (current
        owner) — handoff is the only way that ever changes."""
        with self._transaction() as cur:
            cur.execute("""
                INSERT INTO nodus_investigation_runs
                    (run_id, tenant_id, workspace_id, principal_subject, assignee_subject, objective,
                     entity_id, status, budget_json)
                VALUES (%s,%s,%s,%s,%s,%s,%s,'RUNNING',%s)
                ON CONFLICT (run_id) DO NOTHING
                """, (run_id, tenant_id, workspace_id, principal_subject, principal_subject, objective,
                      entity_id, json.dumps(budget.model_dump(mode="json"))))

    def save_checkpoint(self, run_id: str, phase: SpecialistRole, result: SpecialistResult,
                        graph_revision: int | None) -> None:
        """Commits one completed specialist phase's structured result. Idempotent via the
        ``(run_id, phase)`` primary key — repeating this call for the same phase replaces, never
        duplicates, that phase's row.

        Also advances the RUN's own ``graph_revision`` column (never only set at ``finish_run``, which an
        interrupted — never-finished — run never reaches): resume's staleness check (ND-14A section 7)
        must be able to compare against the most recently observed revision even when the run was
        interrupted before completing."""
        with self._transaction() as cur:
            cur.execute("""
                INSERT INTO nodus_investigation_checkpoints (run_id, phase, specialist_result_json, graph_revision)
                VALUES (%s,%s,%s,%s)
                ON CONFLICT (run_id, phase) DO UPDATE SET
                    specialist_result_json = EXCLUDED.specialist_result_json,
                    graph_revision = EXCLUDED.graph_revision
                """, (run_id, phase.value, json.dumps(result.model_dump(mode="json")), graph_revision))
            cur.execute("""
                UPDATE nodus_investigation_runs
                SET graph_revision = COALESCE(%s, graph_revision), updated_at = now() WHERE run_id = %s
                """, (graph_revision, run_id))

    def load_checkpoints(self, run_id: str) -> dict[SpecialistRole, SpecialistResult]:
        with psycopg.connect(self.dsn, row_factory=dict_row, autocommit=True,
                             connect_timeout=self.CONNECT_TIMEOUT_SECONDS) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT phase, specialist_result_json FROM nodus_investigation_checkpoints "
                           "WHERE run_id = %s", (run_id,))
                rows = cur.fetchall()
        return {SpecialistRole(row["phase"]): SpecialistResult.model_validate(row["specialist_result_json"])
               for row in rows}

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        """The minimal read surface for inspecting a run's own status (ND-14A section 8) — never
        returns persisted checkpoint content; call ``load_checkpoints`` separately under the same
        authorization check a caller would need to resume."""
        with psycopg.connect(self.dsn, row_factory=dict_row, autocommit=True,
                             connect_timeout=self.CONNECT_TIMEOUT_SECONDS) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM nodus_investigation_runs WHERE run_id = %s", (run_id,))
                return cur.fetchone()

    def claim_for_resume(self, run_id: str, *, tenant_id: str, principal_subject: str,
                         worker_id: str) -> dict[str, Any]:
        """Row-locks and claims ``run_id`` for exclusive continuation by this worker. Raises
        ``RunClaimDenied`` for every failure mode (see its docstring) — the single generic signal ND-14A
        section 6 requires. The ``tenant_id``/current-``assignee_subject`` equality check here is the hard
        cross-tenant/cross-identity guard: it runs BEFORE any checkpoint content is ever loaded, so a
        denied caller never even reaches a query that could disclose whether the run exists.

        Checked against ``assignee_subject`` (the CURRENT owner), never the immutable
        ``principal_subject`` (the original creator) — ND-14B: once a handoff transfers ownership, the
        previous owner's own resume attempts are denied exactly like any other non-owner's, with no
        separate code path needed."""
        with self._transaction() as cur:
            cur.execute("""
                SELECT tenant_id, assignee_subject, status, lease_expires_at
                FROM nodus_investigation_runs WHERE run_id = %s FOR UPDATE
                """, (run_id,))
            row = cur.fetchone()
            if row is None:
                raise RunClaimDenied("run cannot be resumed")
            if row["tenant_id"] != tenant_id or row["assignee_subject"] != principal_subject:
                raise RunClaimDenied("run cannot be resumed")
            lease_expires_at = row["lease_expires_at"]
            actively_claimed = row["status"] == "RUNNING" and lease_expires_at is not None and \
                lease_expires_at > datetime.now(timezone.utc)
            if row["status"] == "COMPLETED" or actively_claimed:
                raise RunClaimDenied("run cannot be resumed")
            cur.execute("""
                UPDATE nodus_investigation_runs
                SET status='RUNNING', claimed_by=%s, claimed_at=now(),
                    lease_expires_at = now() + make_interval(secs => %s), updated_at = now()
                WHERE run_id = %s
                """, (worker_id, _LEASE_SECONDS, run_id))
            cur.execute("SELECT * FROM nodus_investigation_runs WHERE run_id = %s", (run_id,))
            return cur.fetchone()

    def finish_run(self, run_id: str, *, status: str, result_status: str | None, graph_revision: int | None,
                   usage: dict, failure_reason: str | None = None) -> None:
        with self._transaction() as cur:
            cur.execute("""
                UPDATE nodus_investigation_runs
                SET status=%s, result_status=%s, graph_revision=COALESCE(%s, graph_revision),
                    usage_json=%s, failure_reason=%s, updated_at=now(),
                    completed_at = CASE WHEN %s = 'COMPLETED' THEN now() ELSE completed_at END
                WHERE run_id = %s
                """, (status, result_status, graph_revision, json.dumps(usage), failure_reason, status, run_id))

    def record_event(self, run_id: str, event_type: str, actor_subject: str, *,
                     reference_id: str | None = None, resulting_state: str | None = None) -> None:
        """ND-14B section 8: append-only lifecycle provenance. Never updated or deleted."""
        with self._transaction() as cur:
            cur.execute("""
                INSERT INTO nodus_investigation_events
                    (event_id, run_id, event_type, actor_subject, reference_id, resulting_state)
                VALUES (%s,%s,%s,%s,%s,%s)
                """, (f"EV-{uuid4().hex}", run_id, event_type, actor_subject, reference_id, resulting_state))

    def list_events(self, run_id: str) -> list[dict[str, Any]]:
        with psycopg.connect(self.dsn, row_factory=dict_row, autocommit=True,
                             connect_timeout=self.CONNECT_TIMEOUT_SECONDS) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM nodus_investigation_events WHERE run_id = %s ORDER BY occurred_at",
                           (run_id,))
                return cur.fetchall()

    def pause_for_access(self, run_id: str, *, actor_subject: str, reason: str, usage: dict) -> None:
        """ND-14B section 3: a run that has lost required authorization stops safely. ``reason`` is a
        generic, non-disclosing string — never a restricted source name or classification (the caller is
        responsible for passing only a generic reason; this method does not itself inspect it)."""
        with self._transaction() as cur:
            cur.execute("""
                UPDATE nodus_investigation_runs
                SET status='PAUSED_ACCESS', failure_reason=%s, usage_json=%s, updated_at=now()
                WHERE run_id = %s
                """, (reason, json.dumps(usage), run_id))
        self.record_event(run_id, "ACCESS_PAUSED", actor_subject, resulting_state="PAUSED_ACCESS")

    def request_handoff(self, run_id: str, *, tenant_id: str, sender_subject: str,
                        recipient_subject: str, governance_store: "GovernanceStore | None" = None) -> str:
        """Section 4: only the run's CURRENT owner may request a handoff, and only within the same
        tenant. Returns the new ``handoff_id``.

        ``governance_store`` (ND-14C section 8, optional): when provided and the deployment has an
        authoritative principal registry for ``recipient_subject``'s identity shape, an unknown recipient
        is rejected with the same generic ``HandoffConflict`` — never a distinct error that would
        disclose which specific check failed. When no registry applies (``principal_exists`` returns
        ``None``, e.g. a non-NEXUS subject or no store given), this check is skipped entirely rather than
        inventing a validation outcome — see that function's own docstring for why."""
        if governance_store is not None:
            from backend.app.governance.identity import principal_exists
            if principal_exists(recipient_subject, governance_store) is False:
                raise HandoffConflict("handoff could not be requested")
        with self._transaction() as cur:
            cur.execute("""
                SELECT tenant_id, assignee_subject, status FROM nodus_investigation_runs
                WHERE run_id = %s FOR UPDATE
                """, (run_id,))
            row = cur.fetchone()
            if row is None or row["tenant_id"] != tenant_id or row["assignee_subject"] != sender_subject:
                raise HandoffConflict("handoff could not be requested")
            if row["status"] == "COMPLETED":
                raise HandoffConflict("handoff could not be requested")
            handoff_id = f"HO-{uuid4().hex}"
            cur.execute("""
                INSERT INTO nodus_investigation_handoffs (handoff_id, run_id, sender_subject, recipient_subject)
                VALUES (%s,%s,%s,%s)
                """, (handoff_id, run_id, sender_subject, recipient_subject))
        self.record_event(run_id, "HANDOFF_REQUESTED", sender_subject, reference_id=handoff_id,
                          resulting_state=f"recipient={recipient_subject}")
        return handoff_id

    def decide_handoff(self, handoff_id: str, *, recipient_subject: str, accept: bool) -> dict[str, Any]:
        """Section 4/5: accept transfers ``assignee_subject`` to the recipient, transactionally with the
        handoff's own state transition — a second, concurrent/duplicate decision on the SAME handoff
        (whether a re-accept, a decline-after-accept, or another worker racing the same accept) always
        raises ``HandoffConflict`` rather than silently succeeding twice, because the row lock below
        serializes it against whichever decision commits first and the status check then fails for the
        loser. No source grants, tokens, or execution context are ever copied — only the plain
        ``assignee_subject`` column changes."""
        with self._transaction() as cur:
            cur.execute("SELECT * FROM nodus_investigation_handoffs WHERE handoff_id = %s FOR UPDATE",
                       (handoff_id,))
            handoff_row = cur.fetchone()
            if handoff_row is None or handoff_row["status"] != "PENDING" or \
                    handoff_row["recipient_subject"] != recipient_subject:
                raise HandoffConflict("handoff could not be decided")
            new_status = "ACCEPTED" if accept else "DECLINED"
            cur.execute("""
                UPDATE nodus_investigation_handoffs SET status=%s, decided_at=now() WHERE handoff_id=%s
                RETURNING *
                """, (new_status, handoff_id))
            updated = cur.fetchone()
            if accept:
                cur.execute("""
                    UPDATE nodus_investigation_runs SET assignee_subject=%s, updated_at=now()
                    WHERE run_id = %s
                    """, (recipient_subject, handoff_row["run_id"]))
        self.record_event(handoff_row["run_id"], f"HANDOFF_{new_status}", recipient_subject,
                          reference_id=handoff_id, resulting_state=new_status)
        return updated

    def record_verification_revision(self, run_id: str, result: SpecialistResult, graph_revision: int | None, *,
                                     freshness_status: str, trigger_reason: str | None,
                                     actor_subject: str | None) -> bool:
        """ND-14C sections 2/7: appends one durable verification revision. Idempotent on
        ``(run_id, graph_revision)`` — repeated processing of the same graph revision returns ``False``
        (no new row, no history duplicated) rather than silently re-recording it. Returns ``True`` iff a
        genuinely new revision was inserted, in which case every PRIOR revision for this run is marked
        ``SUPERSEDED`` (never deleted — see ``list_verification_revisions``)."""
        with self._transaction() as cur:
            cur.execute("SELECT COALESCE(MAX(revision_number), 0) + 1 AS next FROM "
                       "nodus_verification_revisions WHERE run_id=%s", (run_id,))
            next_number = cur.fetchone()["next"]
            cur.execute("""
                INSERT INTO nodus_verification_revisions
                    (revision_id, run_id, revision_number, specialist_result_json, graph_revision,
                     freshness_status, trigger_reason, actor_subject)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (run_id, graph_revision) DO NOTHING
                RETURNING revision_id
                """, (f"VR-{uuid4().hex}", run_id, next_number, json.dumps(result.model_dump(mode="json")),
                      graph_revision, freshness_status, trigger_reason, actor_subject))
            inserted_row = cur.fetchone()
            if inserted_row is not None:
                cur.execute("""
                    UPDATE nodus_verification_revisions SET freshness_status='SUPERSEDED', superseded_at=now()
                    WHERE run_id=%s AND revision_id != %s AND superseded_at IS NULL
                    """, (run_id, inserted_row["revision_id"]))
        return inserted_row is not None

    def list_verification_revisions(self, run_id: str) -> list[dict[str, Any]]:
        """Full, never-deleted history — oldest first. Each row's ``specialist_result_json`` is the
        original, unmodified VERIFICATION ``SpecialistResult`` dump recorded at that revision."""
        with psycopg.connect(self.dsn, row_factory=dict_row, autocommit=True,
                             connect_timeout=self.CONNECT_TIMEOUT_SECONDS) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM nodus_verification_revisions WHERE run_id = %s "
                           "ORDER BY revision_number", (run_id,))
                return cur.fetchall()

    def latest_verification_revision(self, run_id: str) -> dict[str, Any] | None:
        with psycopg.connect(self.dsn, row_factory=dict_row, autocommit=True,
                             connect_timeout=self.CONNECT_TIMEOUT_SECONDS) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM nodus_verification_revisions WHERE run_id = %s "
                           "ORDER BY revision_number DESC LIMIT 1", (run_id,))
                return cur.fetchone()
