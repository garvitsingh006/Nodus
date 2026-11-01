"""Expose the existing ontology registry (``backend.app.graph.ontology``) as a
read-only tool. No alternative ontology implementation is introduced — this
is a thin adapter around ``ontology.validate_relationship``."""
from __future__ import annotations

from pydantic import BaseModel

from backend.app.graph import ontology
from backend.app.graph.models import AssertionOrigin, EntityType, RelationType
from backend.app.security import AuthorizationContext, Permission
from backend.app.tools.contracts import ExecutionLimits
from backend.app.tools.registry import SideEffect, ToolDependencies, ToolRegistry, ToolSpec

ONTOLOGY_VALIDATE_RELATIONSHIP_TOOL_ID = "ontology.validate_relationship"
ONTOLOGY_VALIDATE_RELATIONSHIP_VERSION = "1.0.0"


class OntologyValidateRelationshipInput(BaseModel):
    relation_type: RelationType
    source_type: EntityType
    target_type: EntityType
    origin: AssertionOrigin = AssertionOrigin.SOURCE_ASSERTED


class OntologyValidateRelationshipOutput(BaseModel):
    valid: bool
    reason: str | None = None
    ontology_version: str


def _validate_relationship_handler(context: AuthorizationContext, input_model: OntologyValidateRelationshipInput,
                                   dependencies: ToolDependencies) -> dict:
    try:
        ontology.validate_relationship(input_model.relation_type, input_model.source_type,
                                       input_model.target_type, input_model.origin)
    except ValueError as exc:
        return {"valid": False, "reason": str(exc), "ontology_version": ontology.ONTOLOGY_VERSION}
    return {"valid": True, "reason": None, "ontology_version": ontology.ONTOLOGY_VERSION}


def register_ontology_tools(registry: ToolRegistry) -> None:
    registry.register(ToolSpec(
        tool_id=ONTOLOGY_VALIDATE_RELATIONSHIP_TOOL_ID,
        version=ONTOLOGY_VALIDATE_RELATIONSHIP_VERSION,
        description="Validate a relationship type/direction/origin against the versioned ontology registry.",
        input_model=OntologyValidateRelationshipInput,
        output_model=OntologyValidateRelationshipOutput,
        handler=_validate_relationship_handler,
        required_permissions=frozenset({Permission.READ_GRAPH}),
        side_effect=SideEffect.READ_ONLY,
        limits=ExecutionLimits(max_input_bytes=512, max_output_bytes=2048,
                               max_result_count=1, timeout_seconds=5.0),
        capabilities=frozenset({"ontology.read"}),
        dependencies=frozenset(),
    ))
