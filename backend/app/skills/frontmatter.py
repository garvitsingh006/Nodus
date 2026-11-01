"""Parses and validates the ``SKILL.md`` frontmatter block per the official
Agent Skills specification: a YAML block delimited by ``---`` lines at the
very start of the file, followed by the Markdown instruction body."""
from __future__ import annotations

import re

import yaml
from pydantic import ValidationError

from backend.app.skills.contracts import SkillFrontmatter
from backend.app.skills.errors import SkillValidationError

_FRONTMATTER_RE = re.compile(r"\A---[ \t]*\n(.*?\n)---[ \t]*\n?", re.DOTALL)


def parse_skill_markdown(text: str) -> tuple[SkillFrontmatter, str]:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    match = _FRONTMATTER_RE.match(text)
    if not match:
        raise SkillValidationError("SKILL.md must begin with a YAML frontmatter block delimited by '---'")

    try:
        data = yaml.safe_load(match.group(1)) or {}
    except yaml.YAMLError as exc:
        raise SkillValidationError(f"Invalid frontmatter YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise SkillValidationError("Frontmatter must be a YAML mapping")

    try:
        frontmatter = SkillFrontmatter.model_validate(data)
    except ValidationError as exc:
        raise SkillValidationError(f"Invalid SKILL.md frontmatter: {exc}") from exc

    body = text[match.end():]
    if not body.strip():
        raise SkillValidationError("SKILL.md body (instructions) must not be empty")
    return frontmatter, body
