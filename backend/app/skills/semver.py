"""Minimal semantic version parsing/comparison (major.minor.patch[-prerelease]).
No external dependency; just enough to validate manifests and order versions
for rollback/activation decisions."""
from __future__ import annotations

import re

_SEMVER_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?$")


class InvalidSemVerError(ValueError):
    pass


def parse_semver(value: str) -> tuple[int, int, int, str | None]:
    match = _SEMVER_RE.match(value)
    if not match:
        raise InvalidSemVerError(f"{value!r} is not a valid semantic version (expected major.minor.patch)")
    major, minor, patch, prerelease = match.groups()
    return int(major), int(minor), int(patch), prerelease


def semver_sort_key(value: str) -> tuple[int, int, int, int, str]:
    major, minor, patch, prerelease = parse_semver(value)
    # a release (no prerelease) outranks any prerelease at the same major.minor.patch
    return (major, minor, patch, 0 if prerelease is None else -1, prerelease or "")
