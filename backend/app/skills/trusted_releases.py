"""The trust boundary for production skills: a committed, independently
maintained approval ledger (``TRUSTED_RELEASES.json``), separate from each
skill's own ``SKILL.md``/``nodus.json``.

Trust must never be derived by recomputing a digest from whatever content
happens to be on disk at startup — that would let anyone who can write to
the skill root (or tamper with a file before a process restart) silently
bless their own content as trusted. Instead, a skill version becomes
trusted only when its digest has been separately recorded here, via the
explicit approval step in ``scripts/approve_skill_release.py`` (run
deliberately, after reviewing the content, exactly once per intentional
release) — not as a side effect of registry startup.
"""
from __future__ import annotations

import json
from pathlib import Path

TRUSTED_RELEASES_FILENAME = "TRUSTED_RELEASES.json"


class TrustedReleaseError(ValueError):
    pass


def load_trusted_releases(root: Path) -> dict[str, frozenset[str]]:
    """``skill_id -> {approved content digests}`` from ``<root>/TRUSTED_RELEASES.json``.
    Missing file means no skill under this root is approved — fails closed,
    not open."""
    path = Path(root) / TRUSTED_RELEASES_FILENAME
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise TrustedReleaseError(f"Invalid {TRUSTED_RELEASES_FILENAME}: {exc}") from exc
    if not isinstance(data, dict):
        raise TrustedReleaseError(f"{TRUSTED_RELEASES_FILENAME} must be a JSON object of skill_id -> {{version: digest}}")

    result: dict[str, frozenset[str]] = {}
    for skill_id, versions in data.items():
        if not isinstance(versions, dict) or not all(isinstance(v, str) for v in versions.values()):
            raise TrustedReleaseError(
                f"{TRUSTED_RELEASES_FILENAME}: {skill_id!r} must map version strings to digest strings")
        result[skill_id] = frozenset(versions.values())
    return result


def record_trusted_release(root: Path, skill_id: str, version: str, content_digest: str) -> None:
    """Appends/updates one approved (skill_id, version) -> digest entry.
    Only ``scripts/approve_skill_release.py`` calls this — never registry
    startup, and never anything reachable from discovered skill content."""
    path = Path(root) / TRUSTED_RELEASES_FILENAME
    data = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    data.setdefault(skill_id, {})[version] = content_digest
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
