"""Level C: bounded, contained access to a skill's bundled resources
(``references/``, ``scripts/``, ``assets/``). Never executes a script file —
this module only ever returns bytes to the caller.
"""
from __future__ import annotations

from pathlib import Path, PurePosixPath

_ALLOWED_SUBDIRS = ("references", "scripts", "assets")
MAX_RESOURCE_BYTES = 1_000_000


class ResourceAccessError(ValueError):
    pass


class ResourceNotFoundError(ResourceAccessError):
    pass


class PathTraversalError(ResourceAccessError):
    pass


class SymlinkEscapeError(ResourceAccessError):
    pass


class ResourceTooLargeError(ResourceAccessError):
    pass


def load_resource(skill_root: Path, relative_path: str, max_bytes: int = MAX_RESOURCE_BYTES) -> bytes:
    if not relative_path or relative_path.strip() != relative_path:
        raise PathTraversalError(f"Invalid resource path: {relative_path!r}")

    posix_path = PurePosixPath(relative_path.replace("\\", "/"))
    if posix_path.is_absolute() or ".." in posix_path.parts or not posix_path.parts:
        raise PathTraversalError(f"Unsafe resource path: {relative_path!r}")
    if posix_path.parts[0] not in _ALLOWED_SUBDIRS:
        raise PathTraversalError(
            f"Resource path must be under one of {_ALLOWED_SUBDIRS}: {relative_path!r}")

    root_real = skill_root.resolve(strict=True)

    # Reject a symlink anywhere along the path *before* resolving — resolving
    # first would silently follow it and only the final containment check
    # would (maybe) catch an escape, which is a weaker guarantee.
    walked = skill_root
    for part in posix_path.parts:
        walked = walked / part
        if walked.is_symlink():
            raise SymlinkEscapeError(f"Symlink not allowed in resource path: {relative_path!r}")

    candidate = skill_root.joinpath(*posix_path.parts)
    if not candidate.exists():
        raise ResourceNotFoundError(f"Resource not found: {relative_path!r}")
    if not candidate.is_file():
        raise ResourceNotFoundError(f"Resource is not a file: {relative_path!r}")

    resolved = candidate.resolve(strict=True)
    try:
        resolved.relative_to(root_real)
    except ValueError:
        raise PathTraversalError(f"Resource escapes the skill root: {relative_path!r}") from None

    size = resolved.stat().st_size
    if size > max_bytes:
        raise ResourceTooLargeError(f"Resource {relative_path!r} exceeds {max_bytes} bytes")
    return resolved.read_bytes()
