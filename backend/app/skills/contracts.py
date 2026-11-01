"""Typed skill contracts, split into two layers:

* ``SkillFrontmatter`` — validated against the official Agent Skills
  ``SKILL.md`` frontmatter spec (agentskills.io/specification): only
  ``name``/``description`` are required; ``license``/``compatibility``/
  ``allowed-tools``/``metadata`` are the only other permitted keys.
  ``allowed_tools`` is carried through for visibility only — it is never
  treated as authorization (see ``backend/app/skills/registry.py``).
* ``SkillManifest`` — NODUS-specific execution metadata (version, required
  capabilities, declared tool compatibility, effect classification) that has
  no place in the official frontmatter, kept in a separate ``nodus.json``
  file beside ``SKILL.md`` rather than overloading the spec's format.

Neither contract carries any permission, credential or authorization field.
A skill cannot expand what its loader grants, because the loader grants
nothing — permission enforcement happens only in
``backend.app.tools.executor``/``backend.app.security`` against the
caller's own ``AuthorizationContext``.
"""
from __future__ import annotations

import re
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from backend.app.contracts import Record
from backend.app.skills.semver import parse_semver

_NAME_PATTERN = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")


class SkillEffect(StrEnum):
    READ_ONLY = "READ_ONLY"
    MUTATION = "MUTATION"


class SkillTrustStatus(StrEnum):
    TRUSTED = "TRUSTED"
    UNTRUSTED = "UNTRUSTED"


class SkillFrontmatter(BaseModel):
    """Validated ``SKILL.md`` YAML frontmatter. Only the spec's documented
    keys are permitted; anything else fails validation rather than being
    silently accepted (``extra="forbid"``)."""
    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)

    name: str = Field(min_length=1, max_length=64)
    description: str = Field(min_length=1, max_length=1024)
    license: str | None = None
    compatibility: str | None = None
    allowed_tools: tuple[str, ...] = Field(default=(), alias="allowed-tools")
    metadata: dict | None = None

    @field_validator("allowed_tools", mode="before")
    @classmethod
    def _coerce_allowed_tools(cls, value):
        if value is None:
            return ()
        if isinstance(value, str):
            return tuple(value.split())
        if isinstance(value, (list, tuple)):
            return tuple(str(item) for item in value)
        raise ValueError("allowed-tools must be a string or a list of strings")

    @model_validator(mode="after")
    def valid_name(self):
        if not _NAME_PATTERN.match(self.name):
            raise ValueError(
                "name must be lowercase letters, numbers and single hyphens only, "
                "and must not start or end with a hyphen")
        return self


class CompatibleTool(Record):
    tool_id: str = Field(min_length=1)
    version: str = Field(min_length=1)


class SkillManifest(Record):
    """NODUS-specific metadata, kept out of the official frontmatter."""
    version: str = Field(min_length=1)
    required_capabilities: tuple[str, ...] = ()
    compatible_tools: tuple[CompatibleTool, ...] = ()
    effect: SkillEffect = SkillEffect.READ_ONLY
    input_expectations: str | None = Field(default=None, max_length=2000)
    output_expectations: str | None = Field(default=None, max_length=2000)
    compatibility_constraints: str | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def valid_version(self):
        parse_semver(self.version)
        return self


class SkillDescriptor(Record):
    """Level A (discovery): compact metadata only — never the instruction
    body — so listing many skills stays cheap."""
    skill_id: str = Field(min_length=1, max_length=64)
    version: str = Field(min_length=1)
    description: str = Field(min_length=1, max_length=1024)
    effect: SkillEffect
    required_capabilities: tuple[str, ...] = ()
    content_digest: str = Field(min_length=64, max_length=64)
    trust_status: SkillTrustStatus


class SkillInstructions(Record):
    """Level B (activation): the validated instruction body for exactly one
    pinned version. Never executed — only ever handed to a caller as text."""
    skill_id: str = Field(min_length=1, max_length=64)
    version: str = Field(min_length=1)
    description: str = Field(min_length=1, max_length=1024)
    body: str = Field(min_length=1)
