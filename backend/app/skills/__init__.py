"""Skill infrastructure (Stage 05 — GH-006B): discovery, trusted loading,
versioning and lifecycle for the official Agent Skills ``SKILL.md`` format.

No production skills live here (that is GH-006C), no agent runtime, no ADK
and no Gemini dependency — this package only ever returns metadata/text to
a caller. Permission enforcement remains entirely in
``backend.app.tools.executor``/``backend.app.security``.
"""
from __future__ import annotations

from backend.app.skills.catalog import build_production_skill_registry
from backend.app.skills.config import default_skill_roots
from backend.app.skills.contracts import (
    CompatibleTool, SkillDescriptor, SkillEffect, SkillFrontmatter, SkillInstructions, SkillManifest,
    SkillTrustStatus,
)
from backend.app.skills.loader import SkillCandidate, discover_candidates
from backend.app.skills.registry import SkillRegistry
from backend.app.skills.trust import SkillTrustPolicy

__all__ = [
    "CompatibleTool",
    "SkillCandidate",
    "SkillDescriptor",
    "SkillEffect",
    "SkillFrontmatter",
    "SkillInstructions",
    "SkillManifest",
    "SkillRegistry",
    "SkillTrustPolicy",
    "SkillTrustStatus",
    "build_production_skill_registry",
    "default_skill_roots",
    "discover_candidates",
]
