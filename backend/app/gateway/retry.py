"""Bounded retry/backoff policy for transient provider failures only.

Not stacked on top of unbounded SDK-internal retries: this policy bounds the
*total* number of attempts this gateway makes, independent of whatever the
SDK does inside a single call, so SDK-internal retries cannot create
unbounded cost on their own — the outer bound here still applies.
"""
from __future__ import annotations

import random
from dataclasses import dataclass

RETRYABLE_CATEGORIES = frozenset({"RATE_LIMITED", "PROVIDER_UNAVAILABLE", "TIMEOUT"})


@dataclass(frozen=True)
class RetryPolicy:
    max_retries: int = 2
    base_delay_seconds: float = 0.5
    max_delay_seconds: float = 8.0
    jitter_seconds: float = 0.25

    def __post_init__(self) -> None:
        if self.max_retries < 0:
            raise ValueError("max_retries cannot be negative")

    def delay_for(self, attempt: int) -> float:
        backoff = min(self.max_delay_seconds, self.base_delay_seconds * (2 ** attempt))
        return backoff + random.uniform(0, self.jitter_seconds)

    def is_retryable(self, category: str) -> bool:
        return category in RETRYABLE_CATEGORIES
