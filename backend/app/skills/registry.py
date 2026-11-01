"""Typed, versioned skill registry: registration, trust/compatibility
validation, progressive loading (discovery -> activation -> resource
access) and the release/rollback lifecycle.

Skills carry no authorization of their own. Every method here only ever
returns metadata or text; nothing in this module touches
``AuthorizationContext`` or grants a ``Permission`` — tool permission
enforcement remains entirely in ``backend.app.tools.executor``. A skill's
``allowed_tools``/``required_capabilities`` are descriptive data a future
caller may *consult*, never a grant this registry acts on.
"""
from __future__ import annotations

import dataclasses
import threading
from dataclasses import dataclass
from pathlib import Path

from backend.app.skills.contracts import SkillDescriptor, SkillInstructions, SkillManifest, SkillTrustStatus
from backend.app.skills.errors import (
    DuplicateSkillVersionError, NoActiveSkillVersionError, RevokedSkillError, SkillIntegrityError,
    SkillValidationError, ToolVersionIncompatibleError, UnknownSkillVersionError, UnknownToolDependencyError,
    UntrustedSkillError,
)
from backend.app.skills.frontmatter import parse_skill_markdown
from backend.app.skills.loader import MANIFEST_FILENAME, MAX_SKILL_MD_BYTES, SkillCandidate, compute_content_digest
from backend.app.skills.resources import MAX_RESOURCE_BYTES, load_resource
from backend.app.skills.trust import SkillTrustPolicy
from backend.app.tools.registry import ToolRegistry


@dataclass(frozen=True)
class _SkillRecord:
    descriptor: SkillDescriptor
    manifest: SkillManifest
    root: Path
    content_digest: str
    revoked: bool = False
    revocation_reason: str | None = None


def _validate_tool_compatibility(manifest: SkillManifest, tool_registry: ToolRegistry) -> None:
    known_ids = tool_registry.tool_ids()
    for dependency in manifest.compatible_tools:
        if (dependency.tool_id, dependency.version) in tool_registry:
            continue
        if dependency.tool_id in known_ids:
            raise ToolVersionIncompatibleError(
                f"Tool {dependency.tool_id!r} version {dependency.version!r} is not registered")
        raise UnknownToolDependencyError(f"Tool {dependency.tool_id!r} required by skill is not registered")


class SkillRegistry:
    def __init__(self, trust_policy: SkillTrustPolicy | None = None) -> None:
        self.trust_policy = trust_policy or SkillTrustPolicy()
        self._lock = threading.Lock()
        self._versions: dict[tuple[str, str], _SkillRecord] = {}
        self._active: dict[str, str] = {}

    # -- registration -----------------------------------------------------

    def register(self, candidate: SkillCandidate, tool_registry: ToolRegistry | None = None) -> SkillDescriptor:
        key = (candidate.skill_id, candidate.version)
        with self._lock:
            if key in self._versions:
                raise DuplicateSkillVersionError(f"{candidate.skill_id}@{candidate.version} is already registered")
            if tool_registry is not None:
                _validate_tool_compatibility(candidate.manifest, tool_registry)

            trust_status = self.trust_policy.classify(candidate.skill_id, candidate.content_digest)
            descriptor = SkillDescriptor(
                skill_id=candidate.skill_id, version=candidate.version,
                description=candidate.frontmatter.description, effect=candidate.manifest.effect,
                required_capabilities=candidate.manifest.required_capabilities,
                content_digest=candidate.content_digest, trust_status=trust_status,
            )
            self._versions[key] = _SkillRecord(
                descriptor=descriptor, manifest=candidate.manifest, root=candidate.root,
                content_digest=candidate.content_digest,
            )
            return descriptor

    # -- lifecycle ----------------------------------------------------------

    def activate(self, skill_id: str, version: str) -> SkillDescriptor:
        with self._lock:
            record = self._require_usable(skill_id, version)
            self._active[skill_id] = version
            return record.descriptor

    def deactivate(self, skill_id: str) -> None:
        with self._lock:
            self._active.pop(skill_id, None)

    def revoke(self, skill_id: str, version: str, reason: str) -> None:
        with self._lock:
            key = (skill_id, version)
            record = self._versions.get(key)
            if record is None:
                raise UnknownSkillVersionError(f"{skill_id}@{version} is not registered")
            self._versions[key] = dataclasses.replace(record, revoked=True, revocation_reason=reason)
            if self._active.get(skill_id) == version:
                del self._active[skill_id]

    def rollback(self, skill_id: str, to_version: str) -> SkillDescriptor:
        """Activate a previously registered, still-trusted, non-revoked
        version — e.g. after discovering the current active version is bad."""
        return self.activate(skill_id, to_version)

    # -- Level A: discovery ---------------------------------------------

    def resolve(self, skill_id: str, version: str | None = None) -> SkillDescriptor:
        """Pin to an explicit version, or fall back to the active one.
        Resolving never mutates registry state, so a caller that resolved a
        version keeps using exactly that version even if ``activate``/
        ``revoke`` is later called for the same ``skill_id`` elsewhere."""
        with self._lock:
            if version is None:
                version = self._active.get(skill_id)
                if version is None:
                    raise NoActiveSkillVersionError(f"No active version for skill {skill_id!r}")
            record = self._require_usable(skill_id, version)
            return record.descriptor

    def active_version(self, skill_id: str) -> str | None:
        with self._lock:
            return self._active.get(skill_id)

    def describe(self, skill_id: str, version: str) -> SkillDescriptor:
        """Inspect a registered version's metadata regardless of its trust
        or revocation state — e.g. for an operator reviewing a pending
        approval. Never a path to execution: ``resolve()``/``activate()``
        still enforce trust and must be used for that."""
        with self._lock:
            record = self._versions.get((skill_id, version))
            if record is None:
                raise UnknownSkillVersionError(f"{skill_id}@{version} is not registered")
            return record.descriptor

    def manifest(self, skill_id: str, version: str | None = None) -> SkillManifest:
        """The NODUS manifest (declared tool compatibility, capabilities,
        effect) for a trusted, non-revoked version — e.g. so a caller can
        resolve exactly which ``(tool_id, version)`` pairs a skill declared
        before invoking them through ``ToolExecutor``."""
        descriptor = self.resolve(skill_id, version)
        with self._lock:
            return self._versions[(descriptor.skill_id, descriptor.version)].manifest

    def _require_usable(self, skill_id: str, version: str) -> _SkillRecord:
        """Caller must hold ``self._lock``."""
        record = self._versions.get((skill_id, version))
        if record is None:
            raise UnknownSkillVersionError(f"{skill_id}@{version} is not registered")
        if record.revoked:
            raise RevokedSkillError(f"{skill_id}@{version} has been revoked: {record.revocation_reason}")
        if record.descriptor.trust_status is not SkillTrustStatus.TRUSTED:
            raise UntrustedSkillError(f"{skill_id}@{version} is not trusted and cannot be used")
        return record

    # -- Level B: activation (instructions) ------------------------------

    def get_instructions(self, skill_id: str, version: str | None = None) -> SkillInstructions:
        descriptor = self.resolve(skill_id, version)
        with self._lock:
            record = self._versions[(descriptor.skill_id, descriptor.version)]

        skill_md_path = record.root / "SKILL.md"
        raw = skill_md_path.read_bytes()
        if len(raw) > MAX_SKILL_MD_BYTES:
            raise SkillValidationError(
                f"{descriptor.skill_id}@{descriptor.version}: SKILL.md exceeds the {MAX_SKILL_MD_BYTES}-byte limit")

        manifest_path = record.root / MANIFEST_FILENAME
        raw_manifest = manifest_path.read_bytes()
        current_digest = compute_content_digest(raw, raw_manifest)
        if current_digest != record.content_digest:
            raise SkillIntegrityError(
                f"{descriptor.skill_id}@{descriptor.version}: content (instructions or manifest) "
                f"changed since registration")

        frontmatter, body = parse_skill_markdown(raw.decode("utf-8"))
        return SkillInstructions(skill_id=descriptor.skill_id, version=descriptor.version,
                                 description=frontmatter.description, body=body)

    # -- Level C: resource access -----------------------------------------

    def load_resource(self, skill_id: str, version: str | None, relative_path: str,
                      max_bytes: int = MAX_RESOURCE_BYTES) -> bytes:
        descriptor = self.resolve(skill_id, version)
        with self._lock:
            record = self._versions[(descriptor.skill_id, descriptor.version)]
        return load_resource(record.root, relative_path, max_bytes=max_bytes)
