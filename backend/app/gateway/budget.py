"""Atomic, in-process shared token-budget reservation.

Prevents concurrent requests sharing a budget key from each spending the
full remaining allowance: ``reserve`` deducts under a lock before any
provider call is made, and ``reconcile``/``release`` credit back unused
tokens after the call completes or fails.

DEPLOYMENT LIMITATION (disclosed, not solved, here): this tracker's state is
a plain in-process ``dict`` guarded by a ``threading.Lock`` — it enforces a
shared budget correctly only *within a single Python process*. Running
multiple worker processes (e.g. several ``uvicorn``/gunicorn workers) each
gets its own independent tracker instance and its own independent view of
"remaining budget", so a budget meant to be shared across workers would be
silently over-spent by a factor of roughly the worker count. This is not a
production multi-worker budget-enforcement mechanism. A durable, cross-process
ledger (e.g. Postgres-backed, per the Stage 01 ``StorageAdoptionCriteria``) is
required before deploying a shared budget across more than one process;
building that preemptively here would be speculative infrastructure this
task does not need yet.
"""
from __future__ import annotations

import threading
import uuid


class BudgetNotConfiguredError(ValueError):
    pass


class BudgetExceededError(ValueError):
    pass


class SharedTokenBudgetTracker:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._limits: dict[str, int] = {}
        self._reserved: dict[str, int] = {}
        self._reservations: dict[str, tuple[str, int]] = {}

    def configure(self, key: str, limit: int) -> None:
        if limit < 0:
            raise ValueError("limit must be non-negative")
        with self._lock:
            self._limits[key] = limit
            self._reserved.setdefault(key, 0)

    def remaining(self, key: str) -> int:
        with self._lock:
            if key not in self._limits:
                raise BudgetNotConfiguredError(f"No shared budget configured for {key!r}")
            return self._limits[key] - self._reserved.get(key, 0)

    def reserve(self, key: str, tokens: int) -> str:
        if tokens <= 0:
            raise ValueError("tokens must be positive")
        with self._lock:
            if key not in self._limits:
                raise BudgetNotConfiguredError(f"No shared budget configured for {key!r}")
            used = self._reserved.get(key, 0)
            limit = self._limits[key]
            if used + tokens > limit:
                raise BudgetExceededError(
                    f"Shared budget {key!r} exhausted: {used}/{limit} reserved, {tokens} requested")
            self._reserved[key] = used + tokens
            reservation_id = uuid.uuid4().hex
            self._reservations[reservation_id] = (key, tokens)
            return reservation_id

    def reconcile(self, reservation_id: str, actual_tokens: int) -> None:
        """Release a reservation, crediting back unused tokens. ``actual_tokens``
        is capped at the reserved amount — output is already bounded by the
        request's ``max_output_tokens``, so an over-report never overdraws."""
        with self._lock:
            entry = self._reservations.pop(reservation_id, None)
            if entry is None:
                return
            key, reserved_tokens = entry
            used = max(0, min(actual_tokens, reserved_tokens))
            self._reserved[key] = max(0, self._reserved.get(key, 0) - (reserved_tokens - used))

    def release(self, reservation_id: str) -> None:
        """Full release on failure: nothing was actually spent."""
        self.reconcile(reservation_id, actual_tokens=0)
