from __future__ import annotations

from datetime import datetime
from pydantic import BaseModel, Field, model_validator

from backend.app.graph.models import (
    AssertionOrigin, EntityInput, EntityType, RelationInput, RelationType, SourceRecordInput,
)


class LocalEntity(BaseModel):
    id: str = Field(min_length=1, max_length=500)
    kind: EntityType
    name: str = Field(min_length=1, max_length=500)
    description: str = Field(default="", max_length=10000)
    canonical_id: str | None = Field(default=None, min_length=1, max_length=500)
    valid_from: datetime | None = None
    valid_to: datetime | None = None


class LocalLink(BaseModel):
    from_id: str = Field(alias="from", min_length=1)
    to_id: str = Field(alias="to", min_length=1)
    kind: RelationType
    observed_at: datetime | None = None
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    origin: AssertionOrigin = AssertionOrigin.SOURCE_ASSERTED
    derivation_rule_version: str | None = Field(default=None, min_length=1, max_length=200)


class LocalRecord(BaseModel):
    id: str = Field(min_length=1, max_length=500)
    title: str = Field(min_length=1, max_length=500)
    uri: str | None = Field(default=None, max_length=2000)
    revision: str | None = Field(default=None, max_length=200)
    observed_at: datetime | None = None
    acl_subjects: list[str] = Field(default_factory=list, max_length=100)
    entities: list[LocalEntity] = Field(default_factory=list, max_length=500)
    links: list[LocalLink] = Field(default_factory=list, max_length=1000)

    @model_validator(mode="after")
    def validate_links(self):
        ids = {entity.id for entity in self.entities}
        if len(ids) != len(self.entities):
            raise ValueError("Duplicate entity ID in local record")
        if any(link.from_id not in ids or link.to_id not in ids for link in self.links):
            raise ValueError("Link endpoints must be entities in the same local record")
        return self


class LocalImport(BaseModel):
    source_id: str = Field(min_length=1, max_length=200)
    records: list[LocalRecord] = Field(default_factory=list, max_length=1000)
    deleted_record_ids: list[str] = Field(default_factory=list, max_length=1000)

    @model_validator(mode="after")
    def unique_records(self):
        ids = [record.id for record in self.records]
        if len(ids) != len(set(ids)) or set(ids) & set(self.deleted_record_ids):
            raise ValueError("Duplicate or contradictory record actions")
        return self


def normalize(source_id: str, record: LocalRecord) -> SourceRecordInput:
    """Identity uses an explicit canonical ID or the source's stable ID, never a name."""
    return SourceRecordInput(
        source_id=source_id, record_id=record.id, title=record.title, uri=record.uri,
        revision=record.revision, observed_at=record.observed_at,
        acl_subjects=record.acl_subjects,
        entities=[
            EntityInput(external_id=e.id, canonical_id=e.canonical_id, type=e.kind,
                        name=e.name, description=e.description,
                        valid_from=e.valid_from, valid_to=e.valid_to)
            for e in record.entities
        ],
        relationships=[
            RelationInput(source_external_id=link.from_id, target_external_id=link.to_id,
                          type=link.kind, observed_at=link.observed_at,
                          valid_from=link.valid_from, valid_to=link.valid_to,
                          origin=link.origin, derivation_rule_version=link.derivation_rule_version)
            for link in record.links
        ],
    )
