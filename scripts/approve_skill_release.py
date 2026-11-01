"""Explicit approval step for a skill release.

Computes the current (SKILL.md, nodus.json) content digest for one skill
directory and records it in that skill root's TRUSTED_RELEASES.json. This is
the *only* way a skill version becomes trusted for
``build_production_skill_registry`` — run it deliberately, after reviewing
the skill's content, for a new or changed version. Registry startup never
self-approves; see backend/app/skills/trusted_releases.py.

Usage:
    python scripts/approve_skill_release.py skills/verify-evidence
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.app.skills.loader import _load_candidate  # noqa: E402
from backend.app.skills.trusted_releases import record_trusted_release  # noqa: E402


def main(skill_dir_arg: str) -> None:
    skill_dir = Path(skill_dir_arg).resolve()
    candidate = _load_candidate(skill_dir)
    record_trusted_release(skill_dir.parent, candidate.skill_id, candidate.version, candidate.content_digest)
    print(f"Approved {candidate.skill_id}@{candidate.version} -> {candidate.content_digest}")
    print(f"Recorded in {skill_dir.parent / 'TRUSTED_RELEASES.json'}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("usage: approve_skill_release.py <skill-directory>", file=sys.stderr)
        raise SystemExit(2)
    main(sys.argv[1])
