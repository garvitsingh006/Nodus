"""Pure unit tests for the ontology/relationship registry (no Neo4j required)."""
from __future__ import annotations

import pytest

from backend.app.graph import ontology
from backend.app.graph.models import EntityType, RelationType


def test_every_relation_type_has_a_registry_definition():
    assert set(ontology.RELATIONSHIPS) == set(RelationType)


def test_every_entity_type_has_a_registry_definition():
    assert set(ontology.ENTITY_TYPES) == set(EntityType)


def test_valid_endpoint_combination_is_accepted():
    ontology.validate_relationship(RelationType.ASSIGNED_TO, EntityType.TASK, EntityType.PERSON)
    ontology.validate_relationship(RelationType.MODIFIES, EntityType.PULL_REQUEST, EntityType.REPOSITORY)
    ontology.validate_relationship(RelationType.DEPENDS_ON, EntityType.SERVICE, EntityType.SERVICE)


def test_invalid_source_type_is_rejected():
    with pytest.raises(ValueError, match="does not permit a SERVICE source"):
        ontology.validate_relationship(RelationType.ASSIGNED_TO, EntityType.SERVICE, EntityType.PERSON)


def test_invalid_target_type_is_rejected():
    with pytest.raises(ValueError, match="does not permit a SERVICE target"):
        ontology.validate_relationship(RelationType.ASSIGNED_TO, EntityType.TASK, EntityType.SERVICE)


def test_related_to_is_the_only_universal_fallback():
    for entity_type in EntityType:
        ontology.validate_relationship(RelationType.RELATED_TO, entity_type, entity_type)
    assert ontology.RELATIONSHIPS[RelationType.RELATED_TO].evidence_required is False
    non_fallback = [key for key, value in ontology.RELATIONSHIPS.items()
                    if key is not RelationType.RELATED_TO
                    and value.allowed_source_types == frozenset(EntityType)
                    and value.allowed_target_types == frozenset(EntityType)]
    assert non_fallback == []


def test_extra_definitions_hook_overlays_without_mutating_base_registry():
    override = ontology.RelationshipDefinition(
        type=RelationType.BLOCKS, description="tenant override",
        allowed_source_types=frozenset({EntityType.SERVICE}),
        allowed_target_types=frozenset({EntityType.SERVICE}))
    ontology.validate_relationship(RelationType.BLOCKS, EntityType.SERVICE, EntityType.SERVICE,
                                    extra_definitions={RelationType.BLOCKS: override})
    # Base registry is untouched by the overlay passed above: without it, SERVICE
    # is still rejected and the original TASK->TASK combination still works.
    with pytest.raises(ValueError):
        ontology.validate_relationship(RelationType.BLOCKS, EntityType.SERVICE, EntityType.SERVICE)
    ontology.validate_relationship(RelationType.BLOCKS, EntityType.TASK, EntityType.TASK)
