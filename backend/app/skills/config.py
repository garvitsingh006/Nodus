"""Trusted skill-root configuration, following the same env-driven
convention as ``backend.app.config.Settings`` / ``backend.app.gateway.config``.
"""
from __future__ import annotations

import os
from pathlib import Path


def default_skill_roots() -> tuple[Path, ...]:
    """``GRAPH_HACKS_SKILL_ROOTS`` (comma-separated absolute paths) if set,
    otherwise the repository's own ``skills/`` directory — the Agent Skills
    directory convention's natural home for this project's shipped,
    first-party skills."""
    raw = os.getenv("GRAPH_HACKS_SKILL_ROOTS", "").strip()
    if raw:
        return tuple(Path(entry.strip()).resolve() for entry in raw.split(",") if entry.strip())
    repo_root = Path(__file__).resolve().parents[3]
    return (repo_root / "skills",)
