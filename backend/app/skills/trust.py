"""Explicit allowlist/approval policy for installed skills.

Discovery and registration never imply trust: a skill is classified
``TRUSTED`` only when its content digest is explicitly approved for its
skill ID. Everything else is ``UNTRUSTED`` — visible for inspection via
``SkillRegistry.resolve``/discovery listings, but never activatable or
instruction-loadable (see ``registry.py``).
"""
from __future__ import annotations

from dataclasses import dataclass, field

from backend.app.skills.contracts import SkillTrustStatus


@dataclass(frozen=True)
class SkillTrustPolicy:
    approved_digests: dict[str, frozenset[str]] = field(default_factory=dict)

    def classify(self, skill_id: str, content_digest: str) -> SkillTrustStatus:
        allowed = self.approved_digests.get(skill_id)
        if allowed and content_digest in allowed:
            return SkillTrustStatus.TRUSTED
        return SkillTrustStatus.UNTRUSTED

    def approve(self, skill_id: str, content_digest: str) -> "SkillTrustPolicy":
        """Returns a new policy with one more approved digest. Approval only
        affects registrations made against the new policy — it never
        retroactively changes the trust status already recorded on a
        ``SkillDescriptor`` for a version registered earlier."""
        current = self.approved_digests.get(skill_id, frozenset())
        updated = dict(self.approved_digests)
        updated[skill_id] = current | {content_digest}
        return SkillTrustPolicy(approved_digests=updated)
