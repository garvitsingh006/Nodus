"""Versioned ontology registry: entity types and the typed relationship-type
registry that constrains which source/target entity types a relationship may
connect. This is the single place application code consults before a
relationship is written to the factual graph (see
``GraphRepository.ingest``); it is deliberately data, not inference, and never
promotes an unsupported combination into the graph.

Extending the ontology (new entity/relationship types, or a later version)
means adding entries here, bumping ``ONTOLOGY_VERSION``, and leaving existing
entries unchanged so previously ingested data keeps resolving. Automatic
ontology evolution is out of scope here (see roadmap Stage 10); this module
only prevents undefined or mismatched combinations from being accepted.
"""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from backend.app.graph.models import AssertionOrigin, EntityType, RelationType

ONTOLOGY_VERSION = "1.6.0"


class EntityTypeDefinition(BaseModel):
    model_config = ConfigDict(frozen=True)
    type: EntityType
    description: str
    since_version: str = ONTOLOGY_VERSION


class RelationshipDefinition(BaseModel):
    """A directed, type-safe edge definition.

    ``evidence_required`` records whether this relationship type is expected
    to always carry a justifying source record; ``RELATED_TO`` is the one
    deliberate exception (the generic fallback), so it is marked optional
    rather than required.
    """
    model_config = ConfigDict(frozen=True)
    type: RelationType
    description: str
    allowed_source_types: frozenset[EntityType]
    allowed_target_types: frozenset[EntityType]
    directed: bool = True
    evidence_required: bool = True
    since_version: str = ONTOLOGY_VERSION
    disallowed_origins: frozenset[AssertionOrigin] = frozenset({AssertionOrigin.HYPOTHETICAL})


def _all(*types: EntityType) -> frozenset[EntityType]:
    return frozenset(types)


_ALL_ENTITY_TYPES: frozenset[EntityType] = frozenset(EntityType)


ENTITY_TYPES: dict[EntityType, EntityTypeDefinition] = {
    EntityType.PERSON: EntityTypeDefinition(
        type=EntityType.PERSON,
        since_version="1.0.0",
        description="An individual identified by a connector-authorized source (e.g. a GitHub user, a document author)."),
    EntityType.TEAM: EntityTypeDefinition(
        type=EntityType.TEAM,
        since_version="1.0.0",
        description="A group of people with shared membership, e.g. a reporting unit or on-call rotation."),
    EntityType.REPOSITORY: EntityTypeDefinition(
        type=EntityType.REPOSITORY,
        since_version="1.0.0",
        description="A source-control repository recorded by a connector."),
    EntityType.FILE: EntityTypeDefinition(
        type=EntityType.FILE,
        since_version="1.0.0",
        description="A file tracked within a repository or document store."),
    EntityType.PULL_REQUEST: EntityTypeDefinition(
        type=EntityType.PULL_REQUEST,
        since_version="1.0.0",
        description="A proposed code change (PR/MR) with an author and target repository."),
    EntityType.TASK: EntityTypeDefinition(
        type=EntityType.TASK,
        since_version="1.0.0",
        description="A unit of planned or tracked work, e.g. a ticket or issue."),
    EntityType.DOCUMENT: EntityTypeDefinition(
        type=EntityType.DOCUMENT,
        since_version="1.0.0",
        description="Authored documentation, a decision record, or other written artifact."),
    EntityType.SERVICE: EntityTypeDefinition(
        type=EntityType.SERVICE,
        since_version="1.0.0",
        description="A deployable or operated software service."),
    EntityType.SYSTEM: EntityTypeDefinition(
        type=EntityType.SYSTEM,
        since_version="1.0.0",
        description="A higher-level system or platform composed of services, repositories or files."),
    EntityType.EVENT: EntityTypeDefinition(
        type=EntityType.EVENT,
        since_version="1.0.0",
        description="A time-bound occurrence (e.g. an incident or operational event) with observed effects."),
    EntityType.OTHER: EntityTypeDefinition(
        type=EntityType.OTHER,
        since_version="1.0.0",
        description="An entity whose connector-supplied type does not yet map to a dedicated ontology type."),
    EntityType.PROJECT: EntityTypeDefinition(
        type=EntityType.PROJECT,
        since_version="1.3.0",
        description="A product-aligned organizational and ownership boundary composed of teams and services "
                     "(e.g. a company's product line); distinct from SYSTEM, which is a technical composition "
                     "of services/repositories rather than an org/ownership unit."),
    EntityType.COMMIT: EntityTypeDefinition(
        type=EntityType.COMMIT,
        since_version="1.3.0",
        description="A single source-control commit, distinct from the PULL_REQUEST that may include it."),
    EntityType.BUILD: EntityTypeDefinition(
        type=EntityType.BUILD,
        since_version="1.3.0",
        description="A CI build execution triggered by one or more commits."),
    EntityType.ARTIFACT: EntityTypeDefinition(
        type=EntityType.ARTIFACT,
        since_version="1.3.0",
        description="A release artifact (e.g. container image) produced by a build execution."),
    EntityType.DEPLOYMENT: EntityTypeDefinition(
        type=EntityType.DEPLOYMENT,
        since_version="1.3.0",
        description="A recorded deployment of an artifact to a runtime environment."),
    EntityType.PACKAGE: EntityTypeDefinition(
        type=EntityType.PACKAGE,
        since_version="1.3.0",
        description="A versioned runtime dependency (library/component) recorded in a service's dependency "
                     "inventory, including transitive dependencies of another package."),
    EntityType.CONTRACT: EntityTypeDefinition(
        type=EntityType.CONTRACT,
        since_version="1.3.0",
        description="An interface/API contract a service provides or consumes (e.g. an event schema or "
                     "REST/gRPC interface definition)."),
    EntityType.GRANT: EntityTypeDefinition(
        type=EntityType.GRANT,
        since_version="1.3.0",
        description="A resource access grant record (identity, scope, validity window), metadata-only — "
                     "never carries the restricted payload of the resource it authorizes access to."),
    EntityType.DECISION: EntityTypeDefinition(
        type=EntityType.DECISION,
        since_version="1.3.0",
        description="An administrative decision (approve/narrow/deny/revoke/expire) on an access request or "
                     "grant, metadata-only."),
}


RELATIONSHIPS: dict[RelationType, RelationshipDefinition] = {
    RelationType.DEPENDS_ON: RelationshipDefinition(
        type=RelationType.DEPENDS_ON,
        since_version="1.0.0",
        description="Source requires target to function; a declared or observed structural dependency.",
        allowed_source_types=_all(EntityType.SERVICE, EntityType.SYSTEM, EntityType.REPOSITORY,
                                   EntityType.FILE, EntityType.TASK, EntityType.PACKAGE),
        allowed_target_types=_all(EntityType.SERVICE, EntityType.SYSTEM, EntityType.REPOSITORY,
                                   EntityType.FILE, EntityType.TASK, EntityType.PACKAGE)),
    RelationType.PART_OF: RelationshipDefinition(
        type=RelationType.PART_OF,
        since_version="1.0.0",
        description="Source is a structural component of target "
                     "(1.2.0: also covers a child document nested under a parent document; "
                     "1.3.0: also covers a repository that builds a service, and a service owned by a project).",
        allowed_source_types=_all(EntityType.FILE, EntityType.SERVICE, EntityType.TASK,
                                   EntityType.TEAM, EntityType.REPOSITORY, EntityType.DOCUMENT),
        allowed_target_types=_all(EntityType.REPOSITORY, EntityType.SYSTEM, EntityType.TASK,
                                   EntityType.TEAM, EntityType.DOCUMENT, EntityType.SERVICE,
                                   EntityType.PROJECT)),
    RelationType.OWNS: RelationshipDefinition(
        type=RelationType.OWNS,
        since_version="1.0.0",
        description="Source holds formal, source-asserted ownership of target "
                     "(never inferred from contribution alone; see R05; 1.3.0: also covers project-level "
                     "service ownership).",
        allowed_source_types=_all(EntityType.PERSON, EntityType.TEAM, EntityType.PROJECT),
        allowed_target_types=_all(EntityType.SERVICE, EntityType.SYSTEM, EntityType.REPOSITORY,
                                   EntityType.DOCUMENT),
        disallowed_origins=frozenset({AssertionOrigin.HYPOTHETICAL, AssertionOrigin.INFERRED})),
    RelationType.AUTHORED: RelationshipDefinition(
        type=RelationType.AUTHORED,
        since_version="1.0.0",
        description="Source person created target "
                     "(1.1.0: also covers a tracker issue's reporter, who authored the issue record; "
                     "1.4.0: also covers a repository commit's author).",
        allowed_source_types=_all(EntityType.PERSON),
        allowed_target_types=_all(EntityType.PULL_REQUEST, EntityType.DOCUMENT, EntityType.FILE,
                                   EntityType.TASK, EntityType.COMMIT)),
    RelationType.REFERENCES: RelationshipDefinition(
        type=RelationType.REFERENCES,
        since_version="1.0.0",
        description="Source cites or links to target without asserting structural dependency "
                     "(1.5.0: also covers a pull request's own declared affected-service/relevant-contract "
                     "claims — a source-asserted reference, never proof that the PR's change actually alters "
                     "that service's or contract's behavior).",
        allowed_source_types=_all(EntityType.DOCUMENT, EntityType.TASK, EntityType.PULL_REQUEST),
        allowed_target_types=_all(EntityType.DOCUMENT, EntityType.TASK, EntityType.PULL_REQUEST,
                                   EntityType.SERVICE, EntityType.SYSTEM, EntityType.FILE,
                                   EntityType.PROJECT, EntityType.CONTRACT)),
    RelationType.MODIFIES: RelationshipDefinition(
        type=RelationType.MODIFIES,
        since_version="1.0.0",
        description="Source change touches target file or repository.",
        allowed_source_types=_all(EntityType.PULL_REQUEST, EntityType.PERSON),
        allowed_target_types=_all(EntityType.FILE, EntityType.REPOSITORY)),
    RelationType.RELATED_TO: RelationshipDefinition(
        type=RelationType.RELATED_TO,
        since_version="1.0.0",
        description="Untyped fallback connection. Prefer a specific relationship type when the "
                     "connection's semantics are known; this exists for connections a connector "
                     "can observe but not yet characterize.",
        allowed_source_types=_ALL_ENTITY_TYPES,
        allowed_target_types=_ALL_ENTITY_TYPES,
        evidence_required=False),
    RelationType.ASSIGNED_TO: RelationshipDefinition(
        type=RelationType.ASSIGNED_TO,
        since_version="1.0.0",
        description="Source work item is assigned to target person or team.",
        allowed_source_types=_all(EntityType.TASK),
        allowed_target_types=_all(EntityType.PERSON, EntityType.TEAM)),
    RelationType.BELONGS_TO: RelationshipDefinition(
        type=RelationType.BELONGS_TO,
        since_version="1.0.0",
        description="Source is a member of, or scoped under, target "
                     "(1.3.0: also covers an employee's project assignment).",
        allowed_source_types=_all(EntityType.PERSON, EntityType.FILE, EntityType.REPOSITORY),
        allowed_target_types=_all(EntityType.TEAM, EntityType.SYSTEM, EntityType.PROJECT)),
    RelationType.AFFECTS: RelationshipDefinition(
        type=RelationType.AFFECTS,
        since_version="1.0.0",
        description="Source event has an observed effect on target.",
        allowed_source_types=_all(EntityType.EVENT),
        allowed_target_types=_all(EntityType.SERVICE, EntityType.SYSTEM, EntityType.TASK)),
    RelationType.BLOCKS: RelationshipDefinition(
        type=RelationType.BLOCKS,
        since_version="1.0.0",
        description="Source work item blocks target work item from proceeding.",
        allowed_source_types=_all(EntityType.TASK),
        allowed_target_types=_all(EntityType.TASK)),
    RelationType.REVIEWED: RelationshipDefinition(
        type=RelationType.REVIEWED,
        since_version="1.1.0",
        description="Source person reviewed target pull request — distinct from authorship and never "
                     "promoted to ownership (see R05; 1.6.0: also covers a document's recorded approver/"
                     "reviewer, e.g. an architecture decision's approver_or_reviewer_employee_id — never "
                     "promoted to formal approval authority beyond what the source explicitly records).",
        allowed_source_types=_all(EntityType.PERSON),
        allowed_target_types=_all(EntityType.PULL_REQUEST, EntityType.DOCUMENT)),
    RelationType.REPORTED: RelationshipDefinition(
        type=RelationType.REPORTED,
        since_version="1.2.0",
        description="Source person is recorded as a work item's reporter — distinct from authorship: a "
                     "tracker's reporter field can be administratively reassigned after creation, so it is "
                     "never treated as creation/authorship evidence on its own (use AUTHORED only when the "
                     "source's own creator/authorship field supports it).",
        allowed_source_types=_all(EntityType.PERSON),
        allowed_target_types=_all(EntityType.TASK)),
    RelationType.INCLUDED_IN: RelationshipDefinition(
        type=RelationType.INCLUDED_IN,
        since_version="1.3.0",
        description="Source commit is included in target pull request.",
        allowed_source_types=_all(EntityType.COMMIT),
        allowed_target_types=_all(EntityType.PULL_REQUEST)),
    RelationType.TRIGGERED: RelationshipDefinition(
        type=RelationType.TRIGGERED,
        since_version="1.3.0",
        description="Source commit triggered target build execution.",
        allowed_source_types=_all(EntityType.COMMIT),
        allowed_target_types=_all(EntityType.BUILD)),
    RelationType.PRODUCED: RelationshipDefinition(
        type=RelationType.PRODUCED,
        since_version="1.3.0",
        description="Source build execution produced target release artifact.",
        allowed_source_types=_all(EntityType.BUILD),
        allowed_target_types=_all(EntityType.ARTIFACT)),
    RelationType.DEPLOYED_AS: RelationshipDefinition(
        type=RelationType.DEPLOYED_AS,
        since_version="1.3.0",
        description="Source release artifact was deployed as target recorded deployment.",
        allowed_source_types=_all(EntityType.ARTIFACT),
        allowed_target_types=_all(EntityType.DEPLOYMENT)),
    RelationType.PROVIDES: RelationshipDefinition(
        type=RelationType.PROVIDES,
        since_version="1.3.0",
        description="Source service is the declared provider of target interface contract or event "
                     "(the producer side of a producer/consumer data flow; see also the "
                     "NEX-DEP-003/NEX-DEP-008 canonical-edge-vs-event-direction distinction, which this "
                     "relation does not resolve or override).",
        allowed_source_types=_all(EntityType.SERVICE),
        allowed_target_types=_all(EntityType.CONTRACT, EntityType.EVENT)),
    RelationType.CONSUMES: RelationshipDefinition(
        type=RelationType.CONSUMES,
        since_version="1.3.0",
        description="Source service is a known consumer of target interface contract or event "
                     "(the consumer side of a producer/consumer data flow).",
        allowed_source_types=_all(EntityType.SERVICE),
        allowed_target_types=_all(EntityType.CONTRACT, EntityType.EVENT)),
    RelationType.GRANTS_ACCESS_TO: RelationshipDefinition(
        type=RelationType.GRANTS_ACCESS_TO,
        since_version="1.3.0",
        description="Source access grant authorizes target employee or resource "
                     "(metadata-only: scope/validity, never the restricted resource's payload; "
                     "a grant must be source-asserted/observed from an actual governance record, "
                     "never inferred or hypothetical).",
        allowed_source_types=_all(EntityType.GRANT),
        allowed_target_types=_all(EntityType.PERSON, EntityType.SERVICE, EntityType.DOCUMENT,
                                   EntityType.REPOSITORY, EntityType.SYSTEM),
        disallowed_origins=frozenset({AssertionOrigin.HYPOTHETICAL, AssertionOrigin.INFERRED})),
    RelationType.DECIDED: RelationshipDefinition(
        type=RelationType.DECIDED,
        since_version="1.3.0",
        description="Source administrative decision resolved target access grant "
                     "(approval is not evidence of execution; a pending request grants nothing; see Stage 7D "
                     "governance conventions). A decision must be source-asserted/observed from an actual "
                     "administrative audit record, never inferred or hypothetical.",
        allowed_source_types=_all(EntityType.DECISION),
        allowed_target_types=_all(EntityType.GRANT),
        disallowed_origins=frozenset({AssertionOrigin.HYPOTHETICAL, AssertionOrigin.INFERRED})),
    RelationType.SUPERSEDES: RelationshipDefinition(
        type=RelationType.SUPERSEDES,
        since_version="1.6.0",
        description="Source decision document formally, explicitly replaces target decision document (ND-12C, "
                     "E14 architecture decision genealogy). A declared, source-asserted (or directly observed) "
                     "genealogy link only -- never inferred from chronological ordering, descriptive/topical "
                     "similarity, or a later decision merely discussing an earlier one.",
        allowed_source_types=_all(EntityType.DOCUMENT),
        allowed_target_types=_all(EntityType.DOCUMENT),
        disallowed_origins=frozenset({AssertionOrigin.HYPOTHETICAL, AssertionOrigin.INFERRED})),
}

assert set(RELATIONSHIPS) == set(RelationType), "Every RelationType must have a registry definition"
assert set(ENTITY_TYPES) == set(EntityType), "Every EntityType must have a registry definition"


def validate_relationship(
    relation_type: RelationType,
    source_type: EntityType,
    target_type: EntityType,
    origin: AssertionOrigin = AssertionOrigin.SOURCE_ASSERTED,
    *,
    extra_definitions: dict[RelationType, RelationshipDefinition] | None = None,
) -> None:
    """Reject a relationship whose endpoint types or origin are not permitted by the registry.

    ``extra_definitions`` is the extension point for a future tenant-specific
    registry overlay (merged over the base registry); no caller passes it yet.
    """
    registry = RELATIONSHIPS if not extra_definitions else {**RELATIONSHIPS, **extra_definitions}
    definition = registry.get(relation_type)
    if definition is None:
        raise ValueError(f"Relationship type {relation_type.value!r} is not defined in the ontology registry")
    if source_type not in definition.allowed_source_types:
        allowed = sorted(item.value for item in definition.allowed_source_types)
        raise ValueError(
            f"{relation_type.value} does not permit a {source_type.value} source (allowed: {allowed})")
    if target_type not in definition.allowed_target_types:
        allowed = sorted(item.value for item in definition.allowed_target_types)
        raise ValueError(
            f"{relation_type.value} does not permit a {target_type.value} target (allowed: {allowed})")
    if origin in definition.disallowed_origins:
        raise ValueError(f"{relation_type.value} does not permit an origin of {origin.value!r}")
