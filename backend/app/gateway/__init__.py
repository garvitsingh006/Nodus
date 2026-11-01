"""Reusable Gemini model gateway (Stage 05 — GH-006A).

Provides controlled, deterministic model access for *future* NODUS skills
and Google ADK agents. This package implements no agent, skill or Temporal
dependency; it is a standalone, injectable gateway over one provider
(Gemini) with typed requests/responses, explicit model routing, structured
output validation, token/budget accounting and bounded retries.
"""
from __future__ import annotations

from backend.app.gateway.budget import SharedTokenBudgetTracker
from backend.app.gateway.config import GatewayConfig
from backend.app.gateway.gateway import ModelGateway
from backend.app.gateway.gemini_client import GeminiProviderClient

__all__ = [
    "GatewayConfig",
    "ModelGateway",
    "GeminiProviderClient",
    "SharedTokenBudgetTracker",
    "build_gateway_from_env",
]


def build_gateway_from_env(budget_tracker: SharedTokenBudgetTracker | None = None) -> ModelGateway:
    """Convenience factory; fails clearly if GEMINI_API_KEY is not configured."""
    config = GatewayConfig.from_env()
    if not config.api_key:
        raise RuntimeError("GEMINI_API_KEY is required to build a live Gemini gateway")
    client = GeminiProviderClient(api_key=config.api_key)
    return ModelGateway(provider=client, routing=config.routing, pricing=config.pricing,
                        retry_policy=config.retry_policy, budget_tracker=budget_tracker)
