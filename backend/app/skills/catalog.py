"""Builds a ``SkillRegistry`` populated with this repository's shipped,
first-party production skills (``skills/`` at the repo root).

Trust comes only from ``TRUSTED_RELEASES.json`` (see ``trusted_releases.py``)
— a committed ledger of digests approved by a separate, deliberate step
(``scripts/approve_skill_release.py``), never from recomputing a digest over
whatever is on disk at the moment this function runs. A skill directory
whose ``SKILL.md``/``nodus.json`` has been altered since its last approval
registers as ``UNTRUSTED``: altering files on disk is never, by itself,
sufficient to gain trust, including at process startup.
"""
from __future__ import annotations

from pathlib import Path

from backend.app.skills.config import default_skill_roots
from backend.app.skills.loader import discover_candidates
from backend.app.skills.registry import SkillRegistry
from backend.app.skills.trust import SkillTrustPolicy
from backend.app.skills.trusted_releases import load_trusted_releases
from backend.app.tools.registry import ToolRegistry


def build_production_skill_registry(tool_registry: ToolRegistry | None = None,
                                    roots: "tuple[Path, ...] | None" = None) -> SkillRegistry:
    roots = roots if roots is not None else default_skill_roots()
    candidates = discover_candidates(roots)

    approved: dict[str, set[str]] = {}
    for root in roots:
        for skill_id, digests in load_trusted_releases(Path(root)).items():
            approved.setdefault(skill_id, set()).update(digests)
    policy = SkillTrustPolicy(approved_digests={skill_id: frozenset(digests)
                                                for skill_id, digests in approved.items()})

    registry = SkillRegistry(trust_policy=policy)
    for candidate in candidates:
        registry.register(candidate, tool_registry=tool_registry)
    return registry
