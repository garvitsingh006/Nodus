"""Typed errors for skill discovery, validation, compatibility and the
registry lifecycle. All are plain ``ValueError`` subclasses — callers that
only care about "did this fail" can catch ``ValueError``; callers that need
to react differently catch the specific type."""
from __future__ import annotations

from backend.app.contracts import Record


class SkillValidationIssue(Record):
    field: str
    message: str


class SkillValidationError(ValueError):
    """Structured validation failure: frontmatter, manifest, directory
    layout or size-limit violations discovered before registration."""

    def __init__(self, issues: "str | list[SkillValidationIssue]") -> None:
        if isinstance(issues, str):
            issues = [SkillValidationIssue(field="skill", message=issues)]
        self.issues = issues
        super().__init__("; ".join(f"{issue.field}: {issue.message}" for issue in issues))


class DuplicateSkillVersionError(ValueError):
    pass


class UnknownSkillVersionError(ValueError):
    pass


class UnknownToolDependencyError(ValueError):
    pass


class ToolVersionIncompatibleError(ValueError):
    pass


class UntrustedSkillError(ValueError):
    pass


class RevokedSkillError(ValueError):
    pass


class NoActiveSkillVersionError(ValueError):
    pass


class SkillIntegrityError(ValueError):
    """Raised when a registered skill's on-disk content no longer matches
    the content digest recorded at registration time."""
