"""Configurable pricing metadata and estimated-cost calculation.

No pricing is hardcoded here: the default table is empty, so
``estimate_cost`` returns ``None`` (unknown) until an operator supplies
current rates via ``GEMINI_PRICING_JSON``. An estimated cost is always an
estimate from configured per-token rates, never a claim of actual provider
billing.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ModelPricing:
    input_per_million: float
    output_per_million: float
    currency: str = "USD"


@dataclass(frozen=True)
class PricingTable:
    version: str = "unconfigured"
    prices: dict[str, ModelPricing] = field(default_factory=dict)

    def estimate_cost(self, model: str, input_tokens: int | None, output_tokens: int | None) -> float | None:
        price = self.prices.get(model)
        if price is None or input_tokens is None or output_tokens is None:
            return None
        return (input_tokens / 1_000_000 * price.input_per_million
                + output_tokens / 1_000_000 * price.output_per_million)
