"""Environment-driven gateway configuration, following the same
``load_dotenv``/``.env`` convention as ``backend.app.config.Settings``. All
model identifiers, the allowlist, retry/timeout bounds and pricing are
configurable — none are assumed available or hardcoded as billing fact.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

from backend.app.gateway.contracts import ModelRole
from backend.app.gateway.pricing import ModelPricing, PricingTable
from backend.app.gateway.retry import RetryPolicy
from backend.app.gateway.routing import RoleRouter

_DEFAULT_LIGHTWEIGHT_MODEL = "gemini-3.5-flash-lite"
_DEFAULT_DEFAULT_MODEL = "gemini-3.8-flash"


def _env_path() -> Path:
    return Path(__file__).resolve().parents[3] / ".env"


@dataclass(frozen=True)
class GatewayConfig:
    api_key: str | None
    routing: RoleRouter
    pricing: PricingTable
    retry_policy: RetryPolicy
    request_timeout_seconds: float

    @classmethod
    def from_env(cls) -> "GatewayConfig":
        load_dotenv(dotenv_path=_env_path(), override=False)

        role_models: dict[ModelRole, str] = {
            ModelRole.LIGHTWEIGHT: os.getenv("GEMINI_MODEL_LIGHTWEIGHT", _DEFAULT_LIGHTWEIGHT_MODEL),
            ModelRole.DEFAULT: os.getenv("GEMINI_MODEL_DEFAULT", _DEFAULT_DEFAULT_MODEL),
        }
        escalation = os.getenv("GEMINI_MODEL_ESCALATION", "").strip()
        if escalation:
            role_models[ModelRole.ESCALATION] = escalation

        allowlist_raw = os.getenv("GEMINI_MODEL_ALLOWLIST", "").strip()
        allowlist = frozenset(m.strip() for m in allowlist_raw.split(",") if m.strip()) or None

        prices: dict[str, ModelPricing] = {}
        pricing_raw = os.getenv("GEMINI_PRICING_JSON", "").strip()
        if pricing_raw:
            for model_id, entry in json.loads(pricing_raw).items():
                prices[model_id] = ModelPricing(
                    input_per_million=float(entry["input_per_million"]),
                    output_per_million=float(entry["output_per_million"]),
                    currency=entry.get("currency", "USD"),
                )
        pricing = PricingTable(version=os.getenv("GEMINI_PRICING_VERSION", "unconfigured"), prices=prices)

        retry_policy = RetryPolicy(
            max_retries=int(os.getenv("GEMINI_MAX_RETRIES", "2")),
            base_delay_seconds=float(os.getenv("GEMINI_RETRY_BASE_DELAY_SECONDS", "0.5")),
        )

        return cls(
            api_key=os.getenv("GEMINI_API_KEY") or None,
            routing=RoleRouter(role_models=role_models, allowlist=allowlist),
            pricing=pricing,
            retry_policy=retry_policy,
            request_timeout_seconds=float(os.getenv("GEMINI_REQUEST_TIMEOUT_SECONDS", "30")),
        )
