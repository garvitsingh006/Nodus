"""Trusted-root skill discovery. Walks exactly one level inside each
explicitly configured root looking for ``<root>/<skill-name>/SKILL.md`` —
never recurses further, never follows a symlinked skill directory, and
never downloads or executes anything. This is Level A: it produces a
``SkillCandidate`` carrying parsed (but not yet registered) metadata; the
instruction body is read only transiently here, to compute the content
digest and locate the frontmatter block, and is not retained.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from pydantic import ValidationError

from backend.app.skills.contracts import SkillFrontmatter, SkillManifest
from backend.app.skills.errors import SkillValidationError
from backend.app.skills.frontmatter import parse_skill_markdown

MAX_SKILL_MD_BYTES = 64_000
MANIFEST_FILENAME = "nodus.json"


@dataclass(frozen=True)
class SkillCandidate:
    skill_id: str
    version: str
    root: Path
    frontmatter: SkillFrontmatter
    manifest: SkillManifest
    content_digest: str


def compute_content_digest(skill_md_bytes: bytes, manifest_bytes: bytes) -> str:
    """Covers *both* ``SKILL.md`` (instructions) and ``nodus.json``
    (manifest: declared tool compatibility, capabilities, effect) — a
    manifest-only edit (e.g. widening ``compatible_tools``) must be caught
    by integrity verification exactly like an instruction edit. Length
    prefixes avoid any boundary ambiguity between the two files."""
    hasher = hashlib.sha256()
    hasher.update(len(skill_md_bytes).to_bytes(8, "big"))
    hasher.update(skill_md_bytes)
    hasher.update(len(manifest_bytes).to_bytes(8, "big"))
    hasher.update(manifest_bytes)
    return hasher.hexdigest()


def _load_candidate(skill_dir: Path) -> SkillCandidate:
    skill_md_path = skill_dir / "SKILL.md"
    raw_skill_md = skill_md_path.read_bytes()
    if len(raw_skill_md) > MAX_SKILL_MD_BYTES:
        raise SkillValidationError(f"{skill_dir.name}: SKILL.md exceeds the {MAX_SKILL_MD_BYTES}-byte limit")

    frontmatter, _body = parse_skill_markdown(raw_skill_md.decode("utf-8"))
    if frontmatter.name != skill_dir.name:
        raise SkillValidationError(
            f"Skill directory {skill_dir.name!r} does not match frontmatter name {frontmatter.name!r}")

    manifest_path = skill_dir / MANIFEST_FILENAME
    if not manifest_path.is_file():
        raise SkillValidationError(f"{skill_dir.name}: missing {MANIFEST_FILENAME} manifest")
    raw_manifest = manifest_path.read_bytes()
    try:
        manifest_data = json.loads(raw_manifest.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise SkillValidationError(f"{skill_dir.name}: invalid {MANIFEST_FILENAME}: {exc}") from exc
    try:
        manifest = SkillManifest.model_validate(manifest_data)
    except ValidationError as exc:
        raise SkillValidationError(f"{skill_dir.name}: invalid manifest: {exc}") from exc

    content_digest = compute_content_digest(raw_skill_md, raw_manifest)
    return SkillCandidate(skill_id=frontmatter.name, version=manifest.version, root=skill_dir,
                          frontmatter=frontmatter, manifest=manifest, content_digest=content_digest)


def discover_candidates(trusted_roots: "list[Path] | tuple[Path, ...]") -> list[SkillCandidate]:
    candidates: list[SkillCandidate] = []
    for root in trusted_roots:
        root = Path(root).resolve(strict=True)
        if not root.is_dir():
            raise SkillValidationError(f"Trusted skill root is not a directory: {root}")
        for entry in sorted(root.iterdir()):
            if entry.is_symlink() or not entry.is_dir():
                continue
            if not (entry / "SKILL.md").is_file():
                continue
            candidates.append(_load_candidate(entry))
    return candidates
