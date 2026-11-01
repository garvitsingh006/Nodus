from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from pydantic import BaseModel, Field, model_validator


class EntityType(StrEnum):
    PERSON = "PERSON"
    TEAM = "TEAM"
    REPOSITORY = "REPOSITORY"
    FILE = "FILE"
    PULL_REQUEST = "PULL_REQUEST"
    TASK = "TASK"
    DOCUMENT = "DOCUMENT"
    SERVICE = "SERVICE"
    SYSTEM = "SYSTEM"
    EVENT = "EVENT"
    OTHER = "OTHER"
    PROJECT = "PROJECT"
    COMMIT = "COMMIT"
    BUILD = "BUILD"
    ARTIFACT = "ARTIFACT"
    DEPLOYMENT = "DEPLOYMENT"
    PACKAGE = "PACKAGE"
    CONTRACT = "CONTRACT"
    GRANT = "GRANT"
    DECISION = "DECISION"


class RelationType(StrEnum):
    DEPENDS_ON = "DEPENDS_ON"
    PART_OF = "PART_OF"
    OWNS = "OWNS"
    AUTHORED = "AUTHORED"
    REFERENCES = "REFERENCES"
    MODIFIES = "MODIFIES"
    RELATED_TO = "RELATED_TO"
    ASSIGNED_TO = "ASSIGNED_TO"
    BELONGS_TO = "BELONGS_TO"
    AFFECTS = "AFFECTS"
    BLOCKS = "BLOCKS"
    REVIEWED = "REVIEWED"
    REPORTED = "REPORTED"
    INCLUDED_IN = "INCLUDED_IN"
    TRIGGERED = "TRIGGERED"
    PRODUCED = "PRODUCED"
    DEPLOYED_AS = "DEPLOYED_AS"
    PROVIDES = "PROVIDES"
    CONSUMES = "CONSUMES"
    GRANTS_ACCESS_TO = "GRANTS_ACCESS_TO"
    DECIDED = "DECIDED"
    SUPERSEDES = "SUPERSEDES"


class AssertionOrigin(StrEnum):
    """Four distinct relationship-origin semantics (R05).

    ``source_asserted`` is a source system's direct claim (e.g. a declared
    CODEOWNERS entry); ``observed`` is directly witnessed activity (e.g. a
    commit author); ``inferred`` is derived by an explicit rule/model and must
    carry ``derivation_rule_version``; ``hypothetical`` is speculative and is
    rejected before it ever reaches the factual graph (see
    ``GraphRepository.ingest``) — it exists only so a future counterfactual
    overlay (R35) has a name for the state it must stay outside of.
    """
    SOURCE_ASSERTED = "source_asserted"
    OBSERVED = "observed"
    INFERRED = "inferred"
    HYPOTHETICAL = "hypothetical"


class EntityInput(BaseModel):
    external_id: str = Field(min_length=1, max_length=500)
    canonical_id: str | None = Field(default=None, min_length=1, max_length=500)
    type: EntityType
    name: str = Field(min_length=1, max_length=500)
    description: str = Field(default="", max_length=10000)
    valid_from: datetime | None = None
    valid_to: datetime | None = None

    @model_validator(mode="after")
    def valid_interval(self):
        if self.valid_from and self.valid_to and self.valid_to <= self.valid_from:
            raise ValueError("valid_to must be after valid_from")
        return self


class RelationInput(BaseModel):
    source_external_id: str = Field(min_length=1)
    target_external_id: str = Field(min_length=1)
    type: RelationType
    observed_at: datetime | None = None
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    origin: AssertionOrigin = AssertionOrigin.SOURCE_ASSERTED
    derivation_rule_version: str | None = Field(default=None, min_length=1, max_length=200)

    @model_validator(mode="after")
    def valid_interval(self):
        if self.valid_from and self.valid_to and self.valid_to <= self.valid_from:
            raise ValueError("valid_to must be after valid_from")
        return self

    @model_validator(mode="after")
    def valid_origin(self):
        if self.origin == AssertionOrigin.INFERRED and not self.derivation_rule_version:
            raise ValueError("An inferred relationship must identify its derivation rule/model version")
        if self.origin != AssertionOrigin.INFERRED and self.derivation_rule_version:
            raise ValueError("derivation_rule_version is only meaningful for an inferred relationship")
        return self


class SourceRecordInput(BaseModel):
    source_id: str = Field(min_length=1, max_length=200)
    record_id: str = Field(min_length=1, max_length=500)
    title: str = Field(min_length=1, max_length=500)
    uri: str | None = Field(default=None, max_length=2000)
    acl_subjects: list[str] = Field(default_factory=list, max_length=100)
    observed_at: datetime | None = None
    revision: str | None = Field(default=None, max_length=200)
    entities: list[EntityInput] = Field(default_factory=list, max_length=500)
    relationships: list[RelationInput] = Field(default_factory=list, max_length=1000)


class Entity(BaseModel):
    id: str
    type: EntityType | None
    name: str | None
    description: str | None = None
    active: bool = True
    conflicts: dict[str, list[str]] = Field(default_factory=dict)
    # Per conflicting field: a structured, inspectable authority verdict (R07)
    # — "why this claim is or is not authoritative" — never a resolved value;
    # `conflicts` above remains the complete, unmodified source of truth.
    authority: dict[str, dict] = Field(default_factory=dict)


class Relationship(BaseModel):
    id: str
    source_id: str
    target_id: str
    type: RelationType
    active: bool
    observed_at: str | None = None
    valid_from: str | None = None
    valid_to: str | None = None
    source_id_ref: str | None = None
    record_id: str | None = None
    ontology_version: str | None = None
    origin: str | None = None
    derivation_rule_version: str | None = None
    ingested_at: str | None = None


class Evidence(BaseModel):
    source_id: str
    record_id: str
    title: str
    uri: str | None = None
    observed_at: str | None = None


class GraphPage(BaseModel):
    nodes: list[Entity]
    relationships: list[Relationship]
    truncated: bool = False
    coverage: str = "source_backed_only"
