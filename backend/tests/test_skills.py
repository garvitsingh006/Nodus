"""GH-006B acceptance: skill discovery, trusted loading, versioning and
lifecycle. Fully offline/deterministic — no network, no LLM, no paid calls."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from pydantic import ValidationError

from backend.app.security import AuthorizationContext, Permission, Principal
from backend.app.skills.contracts import SkillManifest, SkillTrustStatus
from backend.app.skills.errors import (
    DuplicateSkillVersionError, NoActiveSkillVersionError, RevokedSkillError, SkillIntegrityError,
    SkillValidationError, ToolVersionIncompatibleError, UnknownSkillVersionError, UnknownToolDependencyError,
    UntrustedSkillError,
)
from backend.app.skills.loader import MANIFEST_FILENAME, compute_content_digest, discover_candidates
from backend.app.skills.registry import SkillRegistry
from backend.app.skills.resources import PathTraversalError, ResourceTooLargeError, SymlinkEscapeError
from backend.app.skills.semver import InvalidSemVerError
from backend.app.skills.trust import SkillTrustPolicy
from backend.app.tools import register_all_tools
from backend.app.tools.contracts import ToolInvocation, ToolStatus
from backend.app.tools.executor import ToolExecutor
from backend.app.tools.graph_tools import GRAPH_ENTITY_LOOKUP_TOOL_ID, GRAPH_ENTITY_LOOKUP_VERSION
from backend.app.tools.registry import ToolRegistry

FIXTURE_ROOT = Path(__file__).resolve().parent / "fixtures" / "skills"


def _write_skill(root: Path, name: str, *, frontmatter_extra: str = "", manifest: dict | None = None,
                 body: str = "Instructions body.\n") -> Path:
    skill_dir = root / name
    (skill_dir / "references").mkdir(parents=True, exist_ok=True)
    skill_md = (
        f"---\nname: {name}\ndescription: Fixture skill {name}.\n{frontmatter_extra}---\n{body}"
    )
    (skill_dir / "SKILL.md").write_text(skill_md, encoding="utf-8")
    import json
    (skill_dir / "nodus.json").write_text(json.dumps(manifest or {"version": "1.0.0"}), encoding="utf-8")
    return skill_dir


def _digest_of(skill_dir: Path) -> str:
    return compute_content_digest((skill_dir / "SKILL.md").read_bytes(),
                                  (skill_dir / MANIFEST_FILENAME).read_bytes())


def _trusting_registry(skill_dir: Path, skill_id: str) -> SkillRegistry:
    policy = SkillTrustPolicy(approved_digests={skill_id: frozenset({_digest_of(skill_dir)})})
    return SkillRegistry(trust_policy=policy)


def _tool_registry() -> ToolRegistry:
    registry = ToolRegistry()
    register_all_tools(registry)
    return registry


# 1. Valid skill registration (from the static fixture)
def test_valid_skill_registration_from_fixture():
    [candidate] = discover_candidates([FIXTURE_ROOT])
    assert candidate.skill_id == "example-skill"
    assert candidate.version == "1.0.0"
    registry = _trusting_registry(candidate.root, candidate.skill_id)
    descriptor = registry.register(candidate, tool_registry=_tool_registry())
    assert descriptor.trust_status == SkillTrustStatus.TRUSTED
    assert descriptor.skill_id == "example-skill"


# 2. Invalid frontmatter
def test_invalid_frontmatter_rejected(tmp_path):
    _write_skill(tmp_path, "Bad_Name", manifest={"version": "1.0.0"})  # uppercase/underscore violates name pattern
    with pytest.raises(SkillValidationError):
        discover_candidates([tmp_path])


def test_unknown_frontmatter_field_rejected(tmp_path):
    _write_skill(tmp_path, "good-skill", frontmatter_extra="unexpected_field: nope\n")
    with pytest.raises(SkillValidationError):
        discover_candidates([tmp_path])


# 3. Duplicate skill registration
def test_duplicate_skill_registration_rejected(tmp_path):
    skill_dir = _write_skill(tmp_path, "dup-skill")
    [candidate] = discover_candidates([tmp_path])
    registry = _trusting_registry(skill_dir, "dup-skill")
    registry.register(candidate)
    with pytest.raises(DuplicateSkillVersionError):
        registry.register(candidate)


# 4. Semantic version validation
def test_invalid_semantic_version_rejected():
    with pytest.raises((ValidationError, InvalidSemVerError)):
        SkillManifest(version="not-a-version")


# 5. Version pinning
def test_version_pinning_bypasses_the_active_pointer(tmp_path):
    dir_v1 = _write_skill(tmp_path, "pin-skill", manifest={"version": "1.0.0"}, body="v1 body\n")
    policy = SkillTrustPolicy()
    registry = SkillRegistry(trust_policy=policy)
    [candidate_v1] = discover_candidates([tmp_path])
    policy = policy.approve("pin-skill", candidate_v1.content_digest)
    registry.trust_policy = policy
    registry.register(candidate_v1)

    import shutil
    shutil.rmtree(dir_v1)
    dir_v2 = _write_skill(tmp_path, "pin-skill", manifest={"version": "2.0.0"}, body="v2 body\n")
    [candidate_v2] = discover_candidates([tmp_path])
    registry.trust_policy = policy.approve("pin-skill", candidate_v2.content_digest)
    registry.register(candidate_v2)
    registry.activate("pin-skill", "2.0.0")

    assert registry.resolve("pin-skill").version == "2.0.0"  # active
    assert registry.resolve("pin-skill", "1.0.0").version == "1.0.0"  # pinned, bypasses active


# 6/7. Lazy metadata/instruction loading
def test_discovery_never_loads_the_instruction_body(tmp_path):
    skill_dir = _write_skill(tmp_path, "lazy-skill", body="Secret instructions.\n")
    [candidate] = discover_candidates([tmp_path])
    registry = _trusting_registry(skill_dir, "lazy-skill")
    descriptor = registry.register(candidate)
    assert not hasattr(descriptor, "body")
    assert "Secret instructions" not in descriptor.model_dump_json()

    instructions = registry.get_instructions("lazy-skill", "1.0.0")
    assert "Secret instructions" in instructions.body


# 8. Resource path traversal rejected
def test_resource_path_traversal_rejected(tmp_path):
    skill_dir = _write_skill(tmp_path, "traversal-skill")
    [candidate] = discover_candidates([tmp_path])
    registry = _trusting_registry(skill_dir, "traversal-skill")
    registry.register(candidate)
    with pytest.raises(PathTraversalError):
        registry.load_resource("traversal-skill", "1.0.0", "../../etc/passwd")
    with pytest.raises(PathTraversalError):
        registry.load_resource("traversal-skill", "1.0.0", "references/../../../etc/passwd")
    with pytest.raises(PathTraversalError):
        registry.load_resource("traversal-skill", "1.0.0", "SKILL.md")  # outside allowed subdirs


# 9. Symlink escape rejected
def test_symlink_escape_rejected(tmp_path):
    skill_dir = _write_skill(tmp_path, "symlink-skill")
    outside = tmp_path / "outside_secret.txt"
    outside.write_text("do not leak\n", encoding="utf-8")
    link = skill_dir / "references" / "escape.txt"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("Symlink creation is not permitted in this environment")

    [candidate] = discover_candidates([tmp_path])
    registry = _trusting_registry(skill_dir, "symlink-skill")
    registry.register(candidate)
    with pytest.raises(SymlinkEscapeError):
        registry.load_resource("symlink-skill", "1.0.0", "references/escape.txt")


# 10. Untrusted/tampered skill rejection
def test_untrusted_skill_rejected():
    skill_dir = FIXTURE_ROOT / "example-skill"
    [candidate] = discover_candidates([FIXTURE_ROOT])
    registry = SkillRegistry()  # no approved digests -> UNTRUSTED
    descriptor = registry.register(candidate)
    assert descriptor.trust_status == SkillTrustStatus.UNTRUSTED
    with pytest.raises(UntrustedSkillError):
        registry.activate("example-skill", "1.0.0")
    with pytest.raises(UntrustedSkillError):
        registry.get_instructions("example-skill", "1.0.0")


def test_tampered_skill_rejected_on_instruction_load(tmp_path):
    skill_dir = _write_skill(tmp_path, "tamper-skill", body="original body\n")
    [candidate] = discover_candidates([tmp_path])
    registry = _trusting_registry(skill_dir, "tamper-skill")
    registry.register(candidate)
    (skill_dir / "SKILL.md").write_text(
        (skill_dir / "SKILL.md").read_text(encoding="utf-8") + "\ntampered extra line\n", encoding="utf-8")
    with pytest.raises(SkillIntegrityError):
        registry.get_instructions("tamper-skill", "1.0.0")


# 11. Unknown tool dependency
def test_unknown_tool_dependency_rejected(tmp_path):
    skill_dir = _write_skill(tmp_path, "unknown-tool-skill",
                             manifest={"version": "1.0.0",
                                      "compatible_tools": [{"tool_id": "no.such.tool", "version": "1.0.0"}]})
    [candidate] = discover_candidates([tmp_path])
    registry = _trusting_registry(skill_dir, "unknown-tool-skill")
    with pytest.raises(UnknownToolDependencyError):
        registry.register(candidate, tool_registry=_tool_registry())


# 12. Tool version incompatibility
def test_tool_version_incompatibility_rejected(tmp_path):
    skill_dir = _write_skill(tmp_path, "bad-version-skill",
                             manifest={"version": "1.0.0", "compatible_tools": [
                                 {"tool_id": GRAPH_ENTITY_LOOKUP_TOOL_ID, "version": "99.0.0"}]})
    [candidate] = discover_candidates([tmp_path])
    registry = _trusting_registry(skill_dir, "bad-version-skill")
    with pytest.raises(ToolVersionIncompatibleError):
        registry.register(candidate, tool_registry=_tool_registry())


# 13. Skill cannot escalate permissions
def test_skill_metadata_cannot_escalate_tool_permissions(tmp_path):
    skill_dir = _write_skill(tmp_path, "escalation-skill",
                             frontmatter_extra=f"allowed-tools: {GRAPH_ENTITY_LOOKUP_TOOL_ID}\n",
                             manifest={"version": "1.0.0", "required_capabilities": ["admin.override"],
                                      "compatible_tools": [
                                          {"tool_id": GRAPH_ENTITY_LOOKUP_TOOL_ID, "version": GRAPH_ENTITY_LOOKUP_VERSION}]})
    [candidate] = discover_candidates([tmp_path])
    registry = _trusting_registry(skill_dir, "escalation-skill")
    registry.register(candidate, tool_registry=_tool_registry())

    # The skill *claims* graph.entity_lookup is allowed and asks for an
    # "admin.override" capability — neither grants anything. A principal
    # without READ_GRAPH is still forbidden by the real executor.
    executor = ToolExecutor(_tool_registry())
    principal = Principal(subject="u", tenant_id="t", workspaces=("w",), permissions=frozenset())
    context = AuthorizationContext(principal=principal, tenant_id="t", workspace_id="w", task_id="t1",
                                   permissions=frozenset())
    result = executor.execute(ToolInvocation(tool_id=GRAPH_ENTITY_LOOKUP_TOOL_ID, version=GRAPH_ENTITY_LOOKUP_VERSION,
                                             arguments={"entity_id": "x"}, context=context))
    assert result.status == ToolStatus.FORBIDDEN


# 14. Controlled activation/deactivation
def test_controlled_activation_and_deactivation(tmp_path):
    skill_dir = _write_skill(tmp_path, "activation-skill")
    [candidate] = discover_candidates([tmp_path])
    registry = _trusting_registry(skill_dir, "activation-skill")
    registry.register(candidate)
    with pytest.raises(NoActiveSkillVersionError):
        registry.resolve("activation-skill")
    registry.activate("activation-skill", "1.0.0")
    assert registry.resolve("activation-skill").version == "1.0.0"
    registry.deactivate("activation-skill")
    with pytest.raises(NoActiveSkillVersionError):
        registry.resolve("activation-skill")


# 15. Rollback to a trusted version
def test_rollback_to_a_previously_trusted_version(tmp_path):
    import json, shutil
    v1_dir = _write_skill(tmp_path, "rollback-skill", manifest={"version": "1.0.0"})
    policy = SkillTrustPolicy()
    registry = SkillRegistry(trust_policy=policy)
    [v1] = discover_candidates([tmp_path])
    policy = policy.approve("rollback-skill", v1.content_digest)
    registry.trust_policy = policy
    registry.register(v1)
    registry.activate("rollback-skill", "1.0.0")

    shutil.rmtree(v1_dir)
    _write_skill(tmp_path, "rollback-skill", manifest={"version": "2.0.0"}, body="broken v2\n")
    [v2] = discover_candidates([tmp_path])
    registry.trust_policy = policy.approve("rollback-skill", v2.content_digest)
    registry.register(v2)
    registry.activate("rollback-skill", "2.0.0")
    assert registry.resolve("rollback-skill").version == "2.0.0"

    registry.rollback("rollback-skill", "1.0.0")
    assert registry.resolve("rollback-skill").version == "1.0.0"


# 16. Revoked skill rejection
def test_revoked_skill_rejected(tmp_path):
    skill_dir = _write_skill(tmp_path, "revoke-skill")
    [candidate] = discover_candidates([tmp_path])
    registry = _trusting_registry(skill_dir, "revoke-skill")
    registry.register(candidate)
    registry.activate("revoke-skill", "1.0.0")
    registry.revoke("revoke-skill", "1.0.0", reason="found to be unsafe")
    with pytest.raises(RevokedSkillError):
        registry.resolve("revoke-skill", "1.0.0")
    with pytest.raises(NoActiveSkillVersionError):
        registry.resolve("revoke-skill")  # active pointer was cleared by revocation
    with pytest.raises(RevokedSkillError):
        registry.activate("revoke-skill", "1.0.0")


# 17. Concurrent registry access
def test_concurrent_registration_and_resolution(tmp_path):
    registry = SkillRegistry()
    policy = SkillTrustPolicy()
    digests = {}
    for i in range(10):
        _write_skill(tmp_path, f"concurrent-skill-{i}")
    for candidate in discover_candidates([tmp_path]):
        digests[candidate.skill_id] = candidate.content_digest
    policy = SkillTrustPolicy(approved_digests={k: frozenset({v}) for k, v in digests.items()})
    registry.trust_policy = policy
    candidates = discover_candidates([tmp_path])
    errors = []

    def register_and_activate(candidate):
        try:
            registry.register(candidate)
            registry.activate(candidate.skill_id, candidate.version)
        except Exception as exc:  # pragma: no cover - failure path surfaced via errors list
            errors.append(exc)

    with ThreadPoolExecutor(max_workers=10) as pool:
        list(pool.map(register_and_activate, candidates))

    assert not errors
    for candidate in candidates:
        assert registry.resolve(candidate.skill_id).version == candidate.version


# 18. Bounded instruction/resource loading
def test_oversized_skill_md_rejected(tmp_path):
    _write_skill(tmp_path, "huge-skill", body="x" * 70_000)
    with pytest.raises(SkillValidationError):
        discover_candidates([tmp_path])


def test_oversized_resource_rejected(tmp_path):
    skill_dir = _write_skill(tmp_path, "huge-resource-skill")
    (skill_dir / "references" / "big.txt").write_bytes(b"x" * 2048)
    [candidate] = discover_candidates([tmp_path])
    registry = _trusting_registry(skill_dir, "huge-resource-skill")
    registry.register(candidate)
    with pytest.raises(ResourceTooLargeError):
        registry.load_resource("huge-resource-skill", "1.0.0", "references/big.txt", max_bytes=1024)


# 19. Existing tools and model gateway remain unaffected
def test_tools_and_gateway_do_not_depend_on_skills():
    import ast

    for package_dir in ("tools", "gateway"):
        base = Path(__file__).resolve().parents[2] / "backend" / "app" / package_dir
        for path in base.glob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module:
                    assert "skills" not in node.module, f"{path} must not depend on the skill registry"
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        assert "skills" not in alias.name, f"{path} must not depend on the skill registry"
