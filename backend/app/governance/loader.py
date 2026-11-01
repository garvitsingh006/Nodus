"""Pure transform layer: parses the NEXUS 7A-7E JSON datasets into
(a) ``nexus_records`` rows (verbatim provenance), (b) governance table rows,
and (c) ``SourceRecordInput``s for the existing Neo4j ingestion pipeline.

No I/O beyond reading the given file paths; no network/database calls here.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from backend.app.graph.models import (
    AssertionOrigin, EntityInput, EntityType, RelationInput, RelationType, SourceRecordInput,
)

FOUNDATION_SOURCE_ID = "NEX-SRC-COMPANY_FOUNDATION"
COMPANY_ID = "nexus"


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _load(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


@dataclass
class NexusRecordRow:
    collection: str
    record_id: str
    source_id: str
    stage: str
    data: dict


@dataclass
class ImportPlan:
    """Everything the importer needs, pre-built and side-effect free."""
    records: list[NexusRecordRow] = field(default_factory=list)
    companies: list[dict] = field(default_factory=list)
    employees: list[dict] = field(default_factory=list)
    project_assignments: list[dict] = field(default_factory=list)
    team_memberships: list[dict] = field(default_factory=list)
    sources: list[dict] = field(default_factory=list)
    roles: list[dict] = field(default_factory=list)
    baseline_entitlements: list[dict] = field(default_factory=list)
    demo_identities: list[dict] = field(default_factory=list)
    resource_access_catalog: list[dict] = field(default_factory=list)
    resource_grants: list[dict] = field(default_factory=list)
    access_requests: list[dict] = field(default_factory=list)
    access_decisions: list[dict] = field(default_factory=list)
    restricted_disclosures: list[dict] = field(default_factory=list)
    administrative_audit_events: list[dict] = field(default_factory=list)
    governance_policies: list[dict] = field(default_factory=list)
    # source_id -> list[SourceRecordInput]
    source_records: dict[str, list[SourceRecordInput]] = field(default_factory=dict)

    def add_record(self, record_input: SourceRecordInput, *, collection: str, stage: str,
                   source_id_override: str | None = None) -> None:
        # A record may reference the same canonical entity twice (e.g. an
        # issue's reporter and assignee are the same employee, or a data-flow
        # producer/consumer resolve to the same service) — GraphRepository.ingest
        # rejects a duplicate external_id, so dedupe by first occurrence here,
        # once, for every caller rather than at each transform call site.
        seen: dict[str, EntityInput] = {}
        for entity in record_input.entities:
            seen.setdefault(entity.external_id, entity)
        if len(seen) != len(record_input.entities):
            record_input = record_input.model_copy(update={"entities": list(seen.values())})
        source_id = source_id_override or record_input.source_id
        self.records.append(NexusRecordRow(
            collection=collection, record_id=record_input.record_id,
            source_id=source_id, stage=stage, data=record_input.model_dump(mode="json")))
        self.source_records.setdefault(record_input.source_id, []).append(record_input)


class _NameCache:
    """Resolves display names for stub entity mentions (e.g. an
    author_employee_id referenced from a commit record needs a PERSON stub
    entity even though the employee's full definition lives in a 7A record)."""

    def __init__(self):
        self.employees: dict[str, str] = {}
        self.teams: dict[str, str] = {}
        self.projects: dict[str, str] = {}
        self.services: dict[str, str] = {}
        self.repositories: dict[str, str] = {}
        self.contracts: dict[str, str] = {}
        self.documents: dict[str, str] = {}
        self.commits: dict[str, str] = {}
        self.artifacts: dict[str, str] = {}
        self.deployments: dict[str, str] = {}
        # ADR id -> {"title", "description"}: populated once in load_7b so any
        # later stub reference (e.g. a repository's related_architecture_
        # decision_ids in load_7c) asserts the exact same name/description as
        # the canonical record, instead of a default "" that would otherwise
        # read as a genuine conflicting claim (GraphRepository._resolve).
        self.architecture_decisions: dict[str, dict[str, str]] = {}

    def name_for(self, kind: dict[str, str], entity_id: str | None) -> str | None:
        if not entity_id:
            return None
        return kind.get(entity_id, entity_id)


def _entity(cache_name: str | None, external_id: str, etype: EntityType, description: str = "") -> EntityInput:
    return EntityInput(external_id=external_id, canonical_id=external_id, type=etype,
                        name=cache_name or external_id, description=description)


def _rel(src: str, tgt: str, rtype: RelationType, *, origin: AssertionOrigin = AssertionOrigin.SOURCE_ASSERTED,
         observed_at: datetime | None = None, valid_from: datetime | None = None,
         valid_to: datetime | None = None) -> RelationInput:
    return RelationInput(source_external_id=src, target_external_id=tgt, type=rtype, origin=origin,
                          observed_at=observed_at, valid_from=valid_from, valid_to=valid_to)


def load_7a(root: Path, names: _NameCache, plan: ImportPlan) -> None:
    company = _load(root / "company.json")["companies"][0]
    plan.companies.append({
        "company_id": company["id"], "company_key": company["company_key"],
        "official_name": company["official_name"], "data": company,
    })
    company_entities = [_entity(company["official_name"], company["id"], EntityType.SYSTEM)]
    record = SourceRecordInput(
        source_id=FOUNDATION_SOURCE_ID, record_id=company["id"], title=company["official_name"],
        entities=company_entities, relationships=[])
    plan.add_record(record, collection="company", stage="7A")

    projects = _load(root / "projects.json")["projects"]
    for p in projects:
        names.projects[p["id"]] = p["name"]
    for p in projects:
        # No PART_OF/OWNS edge to the company SYSTEM node: PROJECT is not an
        # allowed source type for PART_OF, and "project owns company" would
        # invert OWNS's semantics; company->project containment isn't a
        # required relationship family, so the PROJECT entity stands alone.
        entities = [_entity(p["name"], p["id"], EntityType.PROJECT)]
        rec = SourceRecordInput(source_id=FOUNDATION_SOURCE_ID, record_id=p["id"], title=p["name"],
                                observed_at=_parse_dt(p.get("created_at")), entities=entities, relationships=[])
        plan.add_record(rec, collection="projects", stage="7A")

    teams = _load(root / "teams.json")["teams"]
    for t in teams:
        names.teams[t["id"]] = t["name"]
    for t in teams:
        entities = [_entity(t["name"], t["id"], EntityType.TEAM)]
        rels = []
        for project_id in t.get("associated_project_ids", []):
            entities.append(_entity(names.name_for(names.projects, project_id), project_id, EntityType.PROJECT))
            rels.append(_rel(t["id"], project_id, RelationType.PART_OF))
        rec = SourceRecordInput(source_id=FOUNDATION_SOURCE_ID, record_id=t["id"], title=t["name"],
                                observed_at=_parse_dt(t.get("formed_at")), entities=entities, relationships=rels)
        plan.add_record(rec, collection="teams", stage="7A")

    people = _load(root / "people.json")["people"]
    for person in people:
        names.employees[person["id"]] = person["full_name"]
    for person in people:
        entities = [_entity(person["full_name"], person["id"], EntityType.PERSON)]
        rels = []
        for team_id in person.get("team_ids", []):
            entities.append(_entity(names.name_for(names.teams, team_id), team_id, EntityType.TEAM))
            rels.append(_rel(person["id"], team_id, RelationType.BELONGS_TO))
        for project_id in person.get("project_ids", []):
            entities.append(_entity(names.name_for(names.projects, project_id), project_id, EntityType.PROJECT))
            rels.append(_rel(person["id"], project_id, RelationType.BELONGS_TO))
        rec = SourceRecordInput(source_id=FOUNDATION_SOURCE_ID, record_id=person["id"], title=person["full_name"],
                                observed_at=_parse_dt(person.get("joined_at")), entities=entities, relationships=rels)
        plan.add_record(rec, collection="people", stage="7A")
        plan.employees.append({"employee_id": person["id"], "display_name": person["full_name"], "data": person})

    services = _load(root / "services.json")["services"]
    for svc in services:
        names.services[svc["id"]] = svc["name"]
    for svc in services:
        entities = [_entity(svc["name"], svc["id"], EntityType.SERVICE)]
        rels = []
        if svc.get("owning_project_id"):
            entities.append(_entity(names.name_for(names.projects, svc["owning_project_id"]),
                                     svc["owning_project_id"], EntityType.PROJECT))
            rels.append(_rel(svc["owning_project_id"], svc["id"], RelationType.OWNS))
        if svc.get("owning_team_id"):
            entities.append(_entity(names.name_for(names.teams, svc["owning_team_id"]),
                                     svc["owning_team_id"], EntityType.TEAM))
            rels.append(_rel(svc["owning_team_id"], svc["id"], RelationType.OWNS))
        for owner_field in ("primary_engineering_owner_id", "secondary_engineering_owner_id"):
            owner_id = svc.get(owner_field)
            if owner_id:
                entities.append(_entity(names.name_for(names.employees, owner_id), owner_id, EntityType.PERSON))
                rels.append(_rel(owner_id, svc["id"], RelationType.OWNS))
        rec = SourceRecordInput(source_id=FOUNDATION_SOURCE_ID, record_id=svc["id"], title=svc["name"],
                                observed_at=_parse_dt(svc.get("first_released_at")), entities=entities,
                                relationships=rels)
        plan.add_record(rec, collection="services", stage="7A")

    memberships = _load(root / "memberships.json")
    for m in memberships["team_project_responsibilities"]:
        entities = [_entity(names.name_for(names.teams, m["team_id"]), m["team_id"], EntityType.TEAM),
                    _entity(names.name_for(names.projects, m["project_id"]), m["project_id"], EntityType.PROJECT)]
        rels = [_rel(m["team_id"], m["project_id"], RelationType.PART_OF,
                     valid_from=_parse_dt(m.get("effective_from")), valid_to=_parse_dt(m.get("effective_to")))]
        rec = SourceRecordInput(source_id=FOUNDATION_SOURCE_ID, record_id=m["id"],
                                title=f"Team-project responsibility {m['id']}", entities=entities, relationships=rels)
        plan.add_record(rec, collection="team_project_responsibilities", stage="7A")
    for m in memberships["service_project_ownerships"]:
        entities = [_entity(names.name_for(names.projects, m["project_id"]), m["project_id"], EntityType.PROJECT),
                    _entity(names.name_for(names.services, m["service_id"]), m["service_id"], EntityType.SERVICE)]
        rels = [_rel(m["project_id"], m["service_id"], RelationType.OWNS)]
        rec = SourceRecordInput(source_id=FOUNDATION_SOURCE_ID, record_id=m["id"],
                                title=f"Service-project ownership {m['id']}", entities=entities, relationships=rels)
        plan.add_record(rec, collection="service_project_ownerships", stage="7A")
    for m in memberships["service_team_ownerships"]:
        entities = [_entity(names.name_for(names.teams, m["team_id"]), m["team_id"], EntityType.TEAM),
                    _entity(names.name_for(names.services, m["service_id"]), m["service_id"], EntityType.SERVICE)]
        rels = [_rel(m["team_id"], m["service_id"], RelationType.OWNS)]
        rec = SourceRecordInput(source_id=FOUNDATION_SOURCE_ID, record_id=m["id"],
                                title=f"Service-team ownership {m['id']}", entities=entities, relationships=rels)
        plan.add_record(rec, collection="service_team_ownerships", stage="7A")
    for m in memberships["team_memberships"]:
        entities = [_entity(names.name_for(names.employees, m["employee_id"]), m["employee_id"], EntityType.PERSON),
                    _entity(names.name_for(names.teams, m["team_id"]), m["team_id"], EntityType.TEAM)]
        rels = [_rel(m["employee_id"], m["team_id"], RelationType.BELONGS_TO,
                     valid_from=_parse_dt(m.get("effective_from")), valid_to=_parse_dt(m.get("effective_to")))]
        rec = SourceRecordInput(source_id=FOUNDATION_SOURCE_ID, record_id=m["id"],
                                title=f"Team membership {m['id']}", entities=entities, relationships=rels)
        plan.add_record(rec, collection="team_memberships", stage="7A")
        plan.team_memberships.append({"employee_id": m["employee_id"], "team_id": m["team_id"], "data": m})
    for m in memberships["project_assignments"]:
        entities = [_entity(names.name_for(names.employees, m["employee_id"]), m["employee_id"], EntityType.PERSON),
                    _entity(names.name_for(names.projects, m["project_id"]), m["project_id"], EntityType.PROJECT)]
        rels = [_rel(m["employee_id"], m["project_id"], RelationType.BELONGS_TO,
                     valid_from=_parse_dt(m.get("effective_from")), valid_to=_parse_dt(m.get("effective_to")))]
        rec = SourceRecordInput(source_id=FOUNDATION_SOURCE_ID, record_id=m["id"],
                                title=f"Project assignment {m['id']}", entities=entities, relationships=rels)
        plan.add_record(rec, collection="project_assignments", stage="7A")
        plan.project_assignments.append({"employee_id": m["employee_id"], "project_id": m["project_id"], "data": m})

    for soa in _load(root / "service_ownership.json")["service_owner_assignments"]:
        entities = [_entity(names.name_for(names.employees, soa["employee_id"]), soa["employee_id"], EntityType.PERSON),
                    _entity(names.name_for(names.services, soa["service_id"]), soa["service_id"], EntityType.SERVICE)]
        rels = [_rel(soa["employee_id"], soa["service_id"], RelationType.OWNS,
                     valid_from=_parse_dt(soa.get("effective_from")), valid_to=_parse_dt(soa.get("effective_to")))]
        rec = SourceRecordInput(source_id=FOUNDATION_SOURCE_ID, record_id=soa["id"],
                                title=f"Service owner assignment {soa['id']}", entities=entities, relationships=rels)
        plan.add_record(rec, collection="service_owner_assignments", stage="7A")

    for dep in _load(root / "declared_dependencies.json")["declared_dependencies"]:
        entities = [_entity(names.name_for(names.services, dep["source_service_id"]), dep["source_service_id"],
                             EntityType.SERVICE),
                    _entity(names.name_for(names.services, dep["target_service_id"]), dep["target_service_id"],
                             EntityType.SERVICE)]
        rels = [_rel(dep["source_service_id"], dep["target_service_id"], RelationType.DEPENDS_ON)]
        rec = SourceRecordInput(source_id=FOUNDATION_SOURCE_ID, record_id=dep["id"],
                                title=f"Declared dependency {dep['id']}",
                                observed_at=_parse_dt(dep.get("catalog_revision_date") and
                                                      dep["catalog_revision_date"] + "T00:00:00Z"),
                                entities=entities, relationships=rels)
        plan.add_record(rec, collection="declared_dependencies", stage="7A")

    roles_doc = _load(root / "roles_and_demo_identities.json")
    role_code_by_id = {}
    for role in roles_doc["roles"]:
        role_code_by_id[role["id"]] = role["role_code"]
        plan.roles.append({"role_id": role["id"], "role_code": role["role_code"], "data": role})
    for ent in roles_doc["baseline_entitlements"]:
        plan.baseline_entitlements.append({
            "entitlement_id": ent["id"], "role_id": ent["role_id"], "resource_class": ent["resource_class"],
            "baseline_decision": ent["baseline_decision"], "scope": ent["scope"],
            "assigned_project_required": ent["assigned_project_required"],
            "explicit_resource_grant_required": ent["explicit_resource_grant_required"], "data": ent,
        })
    for demo in roles_doc["demo_identities"]:
        plan.demo_identities.append({
            "demo_identity_id": demo["id"], "demo_token": demo["demo_token"],
            "employee_id": demo["employee_id"], "role_id": demo["role_id"], "data": demo,
        })


def _access_record(raw: dict, *, record_id_field: str = "id") -> tuple[str, str, datetime | None]:
    source_id = raw.get("source_id", FOUNDATION_SOURCE_ID)
    record_id = raw[record_id_field]
    observed_at = _parse_dt(raw.get("recorded_at") or raw.get("created_at") or raw.get("observed_at"))
    return source_id, record_id, observed_at


def load_7b(root: Path, names: _NameCache, plan: ImportPlan) -> None:
    for s in _load(root / "source_registry.json")["sources"]:
        plan.sources.append({
            "source_id": s["source_id"], "stage": "7B", "classification": s["access_classification"],
            "owner_project_id": None, "selectable": True,
            "eligible_role_codes": [r.split("-")[-1] for r in s.get("eligible_role_ids", [])], "data": s,
        })

    shared_documents = _load(root / "shared_documents.json")["shared_documents"]
    for doc in shared_documents:
        names.documents[doc["id"]] = doc["title"]
    for doc in shared_documents:
        # ND-12C: two passes so `referenced_document_ids` below resolves the
        # real title (even for a forward reference to a document later in
        # this same file) instead of a same-as-id fallback that would
        # otherwise read as a conflicting claim against the referenced
        # document's own canonical name.
        entities = [_entity(doc["title"], doc["id"], EntityType.DOCUMENT)]
        rels = []
        author = doc.get("author_employee_id")
        if author:
            entities.append(_entity(names.name_for(names.employees, author), author, EntityType.PERSON))
            rels.append(_rel(author, doc["id"], RelationType.AUTHORED, origin=AssertionOrigin.OBSERVED))
        for pid in doc.get("relevant_project_ids", []):
            entities.append(_entity(names.name_for(names.projects, pid), pid, EntityType.PROJECT))
            rels.append(_rel(doc["id"], pid, RelationType.REFERENCES, origin=AssertionOrigin.SOURCE_ASSERTED))
        for sid in doc.get("relevant_service_ids", []):
            entities.append(_entity(names.name_for(names.services, sid), sid, EntityType.SERVICE))
            rels.append(_rel(doc["id"], sid, RelationType.REFERENCES))
        for ref in doc.get("referenced_document_ids", []):
            entities.append(_entity(names.name_for(names.documents, ref), ref, EntityType.DOCUMENT))
            rels.append(_rel(doc["id"], ref, RelationType.REFERENCES))
        rec = SourceRecordInput(source_id=doc["source_id"], record_id=doc["id"], title=doc["title"],
                                revision=str(doc.get("revision_number", "")),
                                observed_at=_parse_dt(doc.get("revised_at")), entities=entities, relationships=rels)
        plan.add_record(rec, collection="shared_documents", stage="7B")

    for cat in _load(root / "service_catalog_records.json")["service_catalog_records"]:
        entities = [_entity(cat.get("service_display_name"), cat["service_id"], EntityType.SERVICE)]
        rec = SourceRecordInput(source_id=cat["source_id"], record_id=cat["id"],
                                title=f"Service catalog: {cat.get('service_display_name', cat['service_id'])}",
                                revision=str(cat.get("revision_number", "")),
                                observed_at=_parse_dt(cat.get("revised_at")), entities=entities, relationships=[])
        plan.add_record(rec, collection="service_catalog_records", stage="7B")

    for ifc in _load(root / "interface_contracts.json")["interface_contracts"]:
        names.contracts[ifc["id"]] = ifc["contract_name"]
        entities = [_entity(ifc["contract_name"], ifc["id"], EntityType.CONTRACT)]
        rels = []
        provider = ifc.get("provider_service_id")
        if provider:
            entities.append(_entity(names.name_for(names.services, provider), provider, EntityType.SERVICE))
            rels.append(_rel(provider, ifc["id"], RelationType.PROVIDES))
        for consumer in ifc.get("known_consumer_service_ids", []):
            entities.append(_entity(names.name_for(names.services, consumer), consumer, EntityType.SERVICE))
            rels.append(_rel(consumer, ifc["id"], RelationType.CONSUMES))
        rec = SourceRecordInput(source_id=ifc["source_id"], record_id=ifc["id"], title=ifc["contract_name"],
                                revision=str(ifc.get("revision_number", "")),
                                observed_at=_parse_dt(ifc.get("revised_at")), entities=entities, relationships=rels)
        plan.add_record(rec, collection="interface_contracts", stage="7B")

    for adr in _load(root / "architecture_decisions.json")["architecture_decisions"]:
        # ND-12C: the fixture's rich decision fields (rationale, alternatives,
        # context, etc.) have no structured home in the current Entity model
        # (name/description only) and adding one would be a broad model
        # change this slice avoids; instead the fields this record already
        # carries are preserved verbatim as a compact JSON payload in the
        # DOCUMENT entity's otherwise-unused `description` -- read back
        # structurally (not re-inferred) by decision.genealogy.
        adr_description = json.dumps({
            "decision_status": adr.get("decision_status"),
            "decision_date": adr.get("decision_date"),
            "context": adr.get("context"),
            "technical_problem": adr.get("technical_problem"),
            "alternatives_considered": adr.get("alternatives_considered", []),
            "selected_decision": adr.get("selected_decision"),
            "rationale": adr.get("rationale"),
            "expected_consequences": adr.get("expected_consequences", []),
        }, sort_keys=True)
        names.architecture_decisions[adr["id"]] = {"title": adr["title"], "description": adr_description}
        entities = [_entity(adr["title"], adr["id"], EntityType.DOCUMENT, description=adr_description)]
        rels = []
        author = adr.get("decision_author_employee_id") or adr.get("author_employee_id")
        if author:
            entities.append(_entity(names.name_for(names.employees, author), author, EntityType.PERSON))
            rels.append(_rel(author, adr["id"], RelationType.AUTHORED, origin=AssertionOrigin.OBSERVED))
        approver = adr.get("approver_or_reviewer_employee_id")
        if approver:
            entities.append(_entity(names.name_for(names.employees, approver), approver, EntityType.PERSON))
            rels.append(_rel(approver, adr["id"], RelationType.REVIEWED, origin=AssertionOrigin.OBSERVED))
        for pid in adr.get("relevant_project_ids", []):
            entities.append(_entity(names.name_for(names.projects, pid), pid, EntityType.PROJECT))
            rels.append(_rel(adr["id"], pid, RelationType.REFERENCES))
        for sid in adr.get("relevant_service_ids", []):
            entities.append(_entity(names.name_for(names.services, sid), sid, EntityType.SERVICE))
            rels.append(_rel(adr["id"], sid, RelationType.REFERENCES))
        for ref_id in adr.get("related_source_record_ids", []):
            # Id-prefix convention used throughout this fixture (NEX-IFC-* for
            # interface contracts, NEX-DOC-* for shared documents, NEX-ADR-*
            # for other decisions). This is a source-asserted *reference*
            # only (ADR -> related resource) -- never a predecessor/successor
            # genealogy claim; SUPERSEDES is only ever projected from a field
            # that explicitly says so, which no current record declares.
            if ref_id.startswith("NEX-IFC-"):
                entities.append(_entity(names.name_for(names.contracts, ref_id), ref_id, EntityType.CONTRACT))
            elif ref_id.startswith("NEX-ADR-"):
                cached = names.architecture_decisions.get(ref_id)
                entities.append(_entity(cached["title"] if cached else None, ref_id, EntityType.DOCUMENT,
                                        description=cached["description"] if cached else ""))
            else:
                entities.append(_entity(names.name_for(names.documents, ref_id), ref_id, EntityType.DOCUMENT))
            rels.append(_rel(adr["id"], ref_id, RelationType.REFERENCES))
        rec = SourceRecordInput(source_id=adr["source_id"], record_id=adr["id"], title=adr["title"],
                                revision=str(adr.get("revision_number", "")),
                                observed_at=_parse_dt(adr.get("revised_at")), entities=entities, relationships=rels)
        plan.add_record(rec, collection="architecture_decisions", stage="7B")

    for term in _load(root / "technical_glossary.json")["technical_glossary"]:
        entities = [_entity(term["term"], term["id"], EntityType.DOCUMENT)]
        rels = []
        for sid in term.get("relevant_service_ids", []):
            entities.append(_entity(names.name_for(names.services, sid), sid, EntityType.SERVICE))
            rels.append(_rel(term["id"], sid, RelationType.REFERENCES))
        for pid in term.get("relevant_project_ids", []):
            entities.append(_entity(names.name_for(names.projects, pid), pid, EntityType.PROJECT))
            rels.append(_rel(term["id"], pid, RelationType.REFERENCES))
        rec = SourceRecordInput(source_id=term["source_id"], record_id=term["id"], title=f"Glossary: {term['term']}",
                                revision=str(term.get("revision_number", "")),
                                observed_at=_parse_dt(term.get("revised_at")), entities=entities, relationships=rels)
        plan.add_record(rec, collection="technical_glossary", stage="7B")



def load_7c(root: Path, names: _NameCache, plan: ImportPlan) -> None:
    for s in _load(root / "engineering_source_registry.json")["engineering_source_registry"]:
        plan.sources.append({
            "source_id": s["source_id"], "stage": "7C", "classification": s["access_classification"],
            "owner_project_id": s.get("owner_project_id"), "selectable": True,
            "eligible_role_codes": [r.split("-")[-1] for r in s.get("eligible_role_ids", [])], "data": s,
        })

    for repo in _load(root / "repositories.json")["repositories"]:
        names.repositories[repo["id"]] = repo["name"]
        entities = [_entity(repo["name"], repo["id"], EntityType.REPOSITORY)]
        rels = []
        for sid in repo.get("related_service_ids", []):
            entities.append(_entity(names.name_for(names.services, sid), sid, EntityType.SERVICE))
            rels.append(_rel(repo["id"], sid, RelationType.PART_OF))
        for adr_id in repo.get("related_architecture_decision_ids", []):
            # ND-12C: RELATED_TO (the ontology's deliberate untyped fallback)
            # because no specific relation type permits a REPOSITORY<->
            # DOCUMENT edge; reuses the exact title/description load_7b
            # already cached for this ADR so this second, independent record
            # never asserts a conflicting claim about the same entity.
            cached = names.architecture_decisions.get(adr_id)
            entities.append(_entity(cached["title"] if cached else None, adr_id, EntityType.DOCUMENT,
                                    description=cached["description"] if cached else ""))
            rels.append(_rel(repo["id"], adr_id, RelationType.RELATED_TO))
        rec = SourceRecordInput(source_id=repo["source_id"], record_id=repo["id"], title=repo["name"],
                                observed_at=_parse_dt(repo.get("revised_at")), entities=entities, relationships=rels)
        plan.add_record(rec, collection="repositories", stage="7C")

    for commit in _load(root / "repository_commits.json")["repository_commits"]:
        names.commits[commit["id"]] = commit["message"][:120]
        entities = [_entity(commit["message"][:120], commit["id"], EntityType.COMMIT)]
        rels = []
        author = commit.get("author_employee_id")
        if author:
            entities.append(_entity(names.name_for(names.employees, author), author, EntityType.PERSON))
            rels.append(_rel(author, commit["id"], RelationType.AUTHORED, origin=AssertionOrigin.OBSERVED))
        rec = SourceRecordInput(source_id=commit["source_id"], record_id=commit["id"],
                                title=f"Commit {commit['commit_hash'][:12]}",
                                observed_at=_parse_dt(commit.get("committed_at")), entities=entities,
                                relationships=rels)
        plan.add_record(rec, collection="repository_commits", stage="7C")

    for pr in _load(root / "pull_requests.json")["pull_requests"]:
        # ND-12E: a PR's own status/approval_status/merge_status/
        # deployment_authorized fields are source-asserted claims the
        # fixture already carries but nothing previously projected;
        # pr.change_impact needs them to distinguish proposed/approved/
        # merged from an actually-deployed state (never inferred from
        # deployment_authorized alone -- see loader module notes).
        pr_description = json.dumps({
            "status": pr.get("status"),
            "approval_status": pr.get("approval_status"),
            "merge_status": pr.get("merge_status"),
            "deployment_authorized": pr.get("deployment_authorized"),
        }, sort_keys=True)
        entities = [_entity(pr["title"], pr["id"], EntityType.PULL_REQUEST, description=pr_description)]
        rels = []
        author = pr.get("author_employee_id")
        if author:
            entities.append(_entity(names.name_for(names.employees, author), author, EntityType.PERSON))
            rels.append(_rel(author, pr["id"], RelationType.AUTHORED, origin=AssertionOrigin.OBSERVED))
        for reviewer in pr.get("reviewer_employee_ids", []):
            entities.append(_entity(names.name_for(names.employees, reviewer), reviewer, EntityType.PERSON))
            rels.append(_rel(reviewer, pr["id"], RelationType.REVIEWED, origin=AssertionOrigin.OBSERVED))
        for commit_id in pr.get("associated_commit_ids", []):
            entities.append(_entity(names.name_for(names.commits, commit_id), commit_id, EntityType.COMMIT))
            rels.append(_rel(commit_id, pr["id"], RelationType.INCLUDED_IN, origin=AssertionOrigin.OBSERVED))
        # ND-12A change-scope projection: a PR's own changed_file_paths/
        # affected_service_ids/relevant_contract_ids are source-asserted
        # claims the PR record already carries, but were not previously
        # projected into the graph at all -- closing this gap is what lets
        # pr.change_impact resolve "what did this PR actually touch" from
        # real source-backed edges instead of guessing. repository_id uses
        # MODIFIES (same relation already used for PR->FILE elsewhere); the
        # repository's own PART_OF->SERVICE edge (see repositories.json
        # below) then lets the service be derived rather than re-declared
        # here. affected_service_ids/relevant_contract_ids use REFERENCES:
        # the PR's own claim of relevance, never promoted to a proven
        # structural dependency (see ontology.py 1.5.0 REFERENCES note).
        repository_id = pr.get("repository_id")
        if repository_id:
            entities.append(_entity(names.name_for(names.repositories, repository_id), repository_id,
                                    EntityType.REPOSITORY))
            rels.append(_rel(pr["id"], repository_id, RelationType.MODIFIES))
        for path in pr.get("changed_file_paths", []):
            file_id = f"{repository_id}:{path}" if repository_id else path
            entities.append(_entity(path, file_id, EntityType.FILE))
            rels.append(_rel(pr["id"], file_id, RelationType.MODIFIES))
        for service_id in pr.get("affected_service_ids", []):
            entities.append(_entity(names.name_for(names.services, service_id), service_id, EntityType.SERVICE))
            rels.append(_rel(pr["id"], service_id, RelationType.REFERENCES))
        for contract_id in pr.get("relevant_contract_ids", []):
            # ND-12E: resolve the authoritative contract_name load_7b already
            # cached (interface_contracts.json loads before this point in
            # build_import_plan) instead of a same-as-id fallback, which
            # previously read as a conflicting claim against the contract's
            # own canonical record (~75 affected entities).
            entities.append(_entity(names.name_for(names.contracts, contract_id), contract_id, EntityType.CONTRACT))
            rels.append(_rel(pr["id"], contract_id, RelationType.REFERENCES))
        rec = SourceRecordInput(source_id=pr["source_id"], record_id=pr["id"], title=pr["title"],
                                observed_at=_parse_dt(pr.get("revised_at")), entities=entities, relationships=rels)
        plan.add_record(rec, collection="pull_requests", stage="7C")

    for issue in _load(root / "engineering_issues.json")["engineering_issues"]:
        # ND-12B: issue_status ("OPEN"/"IN_REVIEW"/"DONE") is a source-asserted
        # workflow field the fixture already carries but the TASK entity's
        # otherwise-unused description previously dropped; work.blocker_chain
        # needs it to distinguish an unresolved blocker from a resolved one.
        entities = [EntityInput(external_id=issue["id"], canonical_id=issue["id"], type=EntityType.TASK,
                                name=issue["title"], description=issue.get("issue_status") or "")]
        rels = []
        owner_team = issue.get("owner_team_id")
        if owner_team:
            entities.append(_entity(names.name_for(names.teams, owner_team), owner_team, EntityType.TEAM))
            rels.append(_rel(issue["id"], owner_team, RelationType.ASSIGNED_TO))
        owner_project = issue.get("owner_project_id")
        if owner_project:
            entities.append(_entity(names.name_for(names.projects, owner_project), owner_project,
                                    EntityType.PROJECT))
            rels.append(_rel(issue["id"], owner_project, RelationType.PART_OF))
        reporter = issue.get("reporter_employee_id")
        if reporter:
            entities.append(_entity(names.name_for(names.employees, reporter), reporter, EntityType.PERSON))
            rels.append(_rel(reporter, issue["id"], RelationType.REPORTED, origin=AssertionOrigin.OBSERVED))
        assignee = issue.get("assignee_employee_id")
        if assignee:
            entities.append(_entity(names.name_for(names.employees, assignee), assignee, EntityType.PERSON))
            rels.append(_rel(issue["id"], assignee, RelationType.ASSIGNED_TO, origin=AssertionOrigin.OBSERVED))
        # ND-12D: the issue's own declared related_service_ids is a field the
        # fixture already carries but nothing previously projected -- needed
        # so people.find_experts can trace "this issue's assignee worked on
        # this service" from a real source-asserted edge rather than guessing.
        for sid in issue.get("related_service_ids", []):
            entities.append(_entity(names.name_for(names.services, sid), sid, EntityType.SERVICE))
            rels.append(_rel(issue["id"], sid, RelationType.REFERENCES))
        rec = SourceRecordInput(source_id=issue["source_id"], record_id=issue["id"], title=issue["title"],
                                observed_at=_parse_dt(issue.get("updated_at") or issue.get("revised_at")),
                                entities=entities, relationships=rels)
        plan.add_record(rec, collection="engineering_issues", stage="7C")

    for build in _load(root / "build_executions.json")["build_executions"]:
        entities = [_entity(f"Build {build['id']}", build["id"], EntityType.BUILD)]
        rels = []
        commit_id = build.get("commit_id")
        if commit_id:
            entities.append(_entity(names.name_for(names.commits, commit_id), commit_id, EntityType.COMMIT))
            rels.append(_rel(commit_id, build["id"], RelationType.TRIGGERED, origin=AssertionOrigin.OBSERVED))
        rec = SourceRecordInput(source_id=build["source_id"], record_id=build["id"], title=f"Build {build['id']}",
                                observed_at=_parse_dt(build.get("completed_at") or build.get("started_at")),
                                entities=entities, relationships=rels)
        plan.add_record(rec, collection="build_executions", stage="7C")

    for test in _load(root / "test_executions.json")["test_executions"]:
        entities = [_entity(f"Test run {test['id']}", test["id"], EntityType.TASK)]
        rec = SourceRecordInput(source_id=test["source_id"], record_id=test["id"], title=f"Test run {test['id']}",
                                observed_at=_parse_dt(test.get("completed_at") or test.get("started_at")),
                                entities=entities, relationships=[])
        plan.add_record(rec, collection="test_executions", stage="7C")

    for art in _load(root / "release_artifacts.json")["release_artifacts"]:
        names.artifacts[art["id"]] = art.get("artifact_version", art["id"])
        entities = [_entity(art.get("artifact_version", art["id"]), art["id"], EntityType.ARTIFACT)]
        rels = []
        build_id = art.get("build_id")
        if build_id:
            entities.append(_entity(f"Build {build_id}", build_id, EntityType.BUILD))
            rels.append(_rel(build_id, art["id"], RelationType.PRODUCED, origin=AssertionOrigin.OBSERVED))
        rec = SourceRecordInput(source_id=art["source_id"], record_id=art["id"],
                                title=f"Artifact {art.get('artifact_version', art['id'])}",
                                observed_at=_parse_dt(art.get("built_at")), entities=entities, relationships=rels)
        plan.add_record(rec, collection="release_artifacts", stage="7C")

    for dep in _load(root / "deployments.json")["deployments"]:
        names.deployments[dep["id"]] = f"Deployment {dep['id']}"
        entities = [_entity(f"Deployment {dep['id']}", dep["id"], EntityType.DEPLOYMENT)]
        rels = []
        artifact_id = dep.get("artifact_id")
        if artifact_id:
            entities.append(_entity(names.name_for(names.artifacts, artifact_id), artifact_id, EntityType.ARTIFACT))
            rels.append(_rel(artifact_id, dep["id"], RelationType.DEPLOYED_AS, origin=AssertionOrigin.OBSERVED))
        rec = SourceRecordInput(source_id=dep["source_id"], record_id=dep["id"], title=f"Deployment {dep['id']}",
                                observed_at=_parse_dt(dep.get("deployed_at")), entities=entities, relationships=rels)
        plan.add_record(rec, collection="deployments", stage="7C")

    for inv in _load(root / "dependency_inventories.json")["dependency_inventories"]:
        entities = [_entity(names.name_for(names.services, inv["service_id"]), inv["service_id"], EntityType.SERVICE)]
        by_name = {p["package_name"]: p for p in inv.get("packages", [])}
        rels: list[RelationInput] = []
        seen_edges: set[tuple[str, str]] = set()
        for pkg in inv.get("packages", []):
            pkg_id, pkg_name = pkg["id"], f"{pkg['package_name']}@{pkg['exact_version']}"
            entities.append(_entity(pkg_name, pkg_id, EntityType.PACKAGE))
            # Walk the package's declared dependency_path (service -> direct
            # package -> ... -> this package) instead of a flat
            # service->package edge, so a TRANSITIVE package gets a real
            # package->package edge distinct from the direct package's own
            # service->package edge (R-correctness: a transitive path must
            # never collapse into a direct dependency edge). Path elements
            # beyond the first are package names resolved against this same
            # inventory's own records where possible, so a package that is
            # both a standalone DIRECT entry and an intermediate hop for a
            # TRANSITIVE dependent always resolves to the same NEX-PKG-* id.
            path = pkg.get("dependency_path") or []
            node_ids = [pkg.get("service_id", inv["service_id"])]
            for element in path[1:]:
                matched = by_name.get(element)
                if matched is not None:
                    node_ids.append(matched["id"])
                    if matched["id"] != pkg_id:
                        entities.append(_entity(f"{matched['package_name']}@{matched['exact_version']}",
                                                matched["id"], EntityType.PACKAGE))
                else:
                    # No standalone record for this intermediate package in
                    # this inventory: a stable, inventory-scoped synthetic id
                    # (never a fabricated version) rather than dropping the hop.
                    synthetic_id = f"{inv['id']}:{element}"
                    node_ids.append(synthetic_id)
                    entities.append(_entity(element, synthetic_id, EntityType.PACKAGE))
            for src, tgt in zip(node_ids, node_ids[1:]):
                if (src, tgt) in seen_edges:
                    continue
                seen_edges.add((src, tgt))
                rels.append(_rel(src, tgt, RelationType.DEPENDS_ON, observed_at=_parse_dt(pkg.get("observed_at"))))
        rec = SourceRecordInput(source_id=inv["source_id"], record_id=inv["id"],
                                title=f"Dependency inventory {inv['id']}",
                                observed_at=_parse_dt(inv.get("observed_at")), entities=entities, relationships=rels)
        plan.add_record(rec, collection="dependency_inventories", stage="7C")

    for thread in _load(root / "engineering_discussions.json")["engineering_discussions"]:
        entities = [_entity(thread["title"], thread["id"], EntityType.DOCUMENT)]
        rels = []
        for author in thread.get("author_employee_ids", []):
            entities.append(_entity(names.name_for(names.employees, author), author, EntityType.PERSON))
            rels.append(_rel(author, thread["id"], RelationType.AUTHORED, origin=AssertionOrigin.OBSERVED))
        rec = SourceRecordInput(source_id=thread["source_id"], record_id=thread["id"], title=thread["title"],
                                observed_at=_parse_dt(thread.get("revised_at")), entities=entities,
                                relationships=rels)
        plan.add_record(rec, collection="engineering_discussions", stage="7C")


def load_7d(root: Path, names: _NameCache, plan: ImportPlan) -> None:
    for s in _load(root / "administrative_source_registry.json")["administrative_source_registry"]:
        plan.sources.append({
            "source_id": s["source_id"], "stage": "7D", "classification": s["access_classification"],
            "owner_project_id": s.get("owner_project_id"), "selectable": True,
            "eligible_role_codes": [r.split("-")[-1] for r in s.get("eligible_role_ids", [])], "data": s,
        })

    for res in _load(root / "resource_access_catalog.json")["resource_access_catalog"]:
        plan.resource_access_catalog.append({
            "resource_id": res["resource_id"], "access_classification": res["access_classification"],
            "owner_project_id": res.get("owner_project_id"), "data": res,
        })
        source_id, record_id, observed_at = _access_record(res)
        rec = SourceRecordInput(source_id=source_id, record_id=record_id,
                                title=f"Resource access catalog entry {record_id}", observed_at=observed_at,
                                entities=[], relationships=[])
        plan.add_record(rec, collection="resource_access_catalog", stage="7D")

    for grant in _load(root / "resource_grants.json")["resource_grants"]:
        plan.resource_grants.append({
            "grant_id": grant["grant_id"], "employee_id": grant["employee_id"], "resource_id": grant["resource_id"],
            "scope_project_id": grant.get("scope_project_id"), "permission_level": grant.get("permission_level"),
            "status": grant.get("grant_status", grant.get("status")),
            "valid_from": _parse_dt(grant.get("valid_from")), "expires_at": _parse_dt(grant.get("expires_at")),
            "revoked_at": _parse_dt(grant.get("revoked_at")),
            "administrative_decision_id": grant.get("administrative_decision_id"), "data": grant,
        })
        entities = [_entity(f"Grant {grant['grant_id']}", grant["grant_id"], EntityType.GRANT),
                    _entity(names.name_for(names.employees, grant["employee_id"]), grant["employee_id"],
                            EntityType.PERSON)]
        rels = [_rel(grant["grant_id"], grant["employee_id"], RelationType.GRANTS_ACCESS_TO,
                     origin=AssertionOrigin.SOURCE_ASSERTED, observed_at=_parse_dt(grant.get("issued_at")),
                     valid_from=_parse_dt(grant.get("valid_from")), valid_to=_parse_dt(grant.get("expires_at")))]
        rec = SourceRecordInput(source_id=grant["source_id"], record_id=grant["id"],
                                title=f"Resource grant {grant['grant_id']}",
                                observed_at=_parse_dt(grant.get("issued_at")), entities=entities, relationships=rels)
        plan.add_record(rec, collection="resource_grants", stage="7D")

    for req in _load(root / "access_requests.json")["access_requests"]:
        plan.access_requests.append({
            "request_id": req["request_id"], "employee_id": req.get("requester_employee_id"),
            "resource_id": req.get("requested_resource_id"), "status": req.get("status"), "data": req,
        })
        source_id, record_id, observed_at = _access_record(req)
        rec = SourceRecordInput(source_id=source_id, record_id=record_id, title=f"Access request {record_id}",
                                observed_at=observed_at, entities=[], relationships=[])
        plan.add_record(rec, collection="access_requests", stage="7D")

    for dec in _load(root / "access_decisions.json")["access_decisions"]:
        plan.access_decisions.append({
            "decision_id": dec["decision_id"], "request_id": dec.get("request_id"),
            "grant_id": dec.get("grant_reference_id") or dec.get("resulting_grant_id"),
            "decision_actor_employee_id": dec.get("deciding_admin_employee_id"), "data": dec,
        })
        entities = [_entity(f"Decision {dec['decision_id']}", dec["decision_id"], EntityType.DECISION)]
        rels = []
        grant_id = dec.get("grant_reference_id") or dec.get("resulting_grant_id")
        if grant_id:
            entities.append(_entity(f"Grant {grant_id}", grant_id, EntityType.GRANT))
            rels.append(_rel(dec["decision_id"], grant_id, RelationType.DECIDED,
                             observed_at=_parse_dt(dec.get("decided_at"))))
        rec = SourceRecordInput(source_id=dec["source_id"], record_id=dec["id"],
                                title=f"Access decision {dec['decision_id']}",
                                observed_at=_parse_dt(dec.get("decided_at")), entities=entities, relationships=rels)
        plan.add_record(rec, collection="access_decisions", stage="7D")

    for rule_set in _load(root / "restricted_disclosure_rules.json")["restricted_disclosure_rules"]:
        plan.restricted_disclosures.append({
            "disclosure_id": rule_set["id"], "recipient_employee_id": None, "data": rule_set,
        })
        source_id, record_id, observed_at = _access_record(rule_set)
        rec = SourceRecordInput(source_id=source_id, record_id=record_id,
                                title=f"Restricted disclosure rules {record_id}", observed_at=observed_at,
                                entities=[], relationships=[])
        plan.add_record(rec, collection="restricted_disclosure_rules", stage="7D")

    for collection_name in ("privileged_change_records", "vendor_governance_records", "security_governance_records"):
        for raw in _load(root / f"{collection_name}.json")[collection_name]:
            source_id, record_id, observed_at = _access_record(raw)
            rec = SourceRecordInput(source_id=source_id, record_id=record_id,
                                    title=f"{collection_name} {record_id}", observed_at=observed_at,
                                    entities=[], relationships=[])
            plan.add_record(rec, collection=collection_name, stage="7D")

    for event in _load(root / "administrative_audit_events.json")["administrative_audit_events"]:
        plan.administrative_audit_events.append({
            "event_id": event["event_id"], "actor_employee_id": event.get("actor_employee_id"),
            "occurred_at": _parse_dt(event.get("created_at")), "data": event,
        })
        source_id, record_id, observed_at = _access_record(event)
        rec = SourceRecordInput(source_id=source_id, record_id=record_id, title=f"Audit event {record_id}",
                                observed_at=observed_at, entities=[], relationships=[])
        plan.add_record(rec, collection="administrative_audit_events", stage="7D")

    for policy in _load(root / "governance_policies.json")["governance_policies"]:
        plan.governance_policies.append({"policy_id": policy["policy_id"], "data": policy})
        source_id, record_id, observed_at = _access_record(policy)
        rec = SourceRecordInput(source_id=source_id, record_id=record_id, title=f"Governance policy {record_id}",
                                observed_at=observed_at, entities=[], relationships=[])
        plan.add_record(rec, collection="governance_policies", stage="7D")


def load_7e(root: Path, names: _NameCache, plan: ImportPlan) -> None:
    """Temporal overlay: re-asserts existing relationship types with
    recorded-time/valid-time metadata from each timeline collection. Purely
    structural meta-collections (record_revision_transitions,
    evidence_correction_events) are preserved verbatim in ``nexus_records``
    only — see loader module docstring / final report for rationale."""
    for own in _load(root / "service_ownership_timeline.json")["service_ownership_timeline"]:
        # ND-12E evidence independence: this collection's own fields prove,
        # per record, whether it is new information or a restatement:
        # - `is_temporary_contact`/TEMPORARY_RELEASE_CONTACT rows are an
        #   operational release-window contact, never formal ownership --
        #   projecting them as OWNS was a real correctness bug.
        # - `historical_evidence_status == SOURCE_DECLARED_CURRENT_ASSIGNMENT`
        #   rows explicitly cite (`parent_record_id`/`evidence_record_ids`)
        #   the exact Stage 7A `service_ownership.json` assignment already
        #   projected as OWNS there -- re-asserting it here would double the
        #   apparent evidence count for the same underlying fact. Skipped,
        #   not deduplicated post hoc, since the two would otherwise look
        #   like independent confirmations to any reader of the graph.
        # A genuinely distinct (if weakly-sourced) historical assignment --
        # RETROSPECTIVE_NOT_SEPARATELY_VERIFIED / HISTORICAL_RETIRED -- is
        # still projected: it is different information (an earlier owner,
        # already correctly time-bound to the past via valid_to), not a
        # duplicate of the current assignment.
        is_duplicate_citation = (own.get("is_temporary_contact")
                                or own.get("historical_evidence_status") == "SOURCE_DECLARED_CURRENT_ASSIGNMENT")
        if is_duplicate_citation:
            # Provenance is still preserved verbatim (nexus_records, via
            # plan.add_record below) -- only the graph-level OWNS assertion
            # that would double-count an already-projected fact is skipped.
            entities, rels = [], []
        else:
            entities = [_entity(names.name_for(names.employees, own["employee_id"]), own["employee_id"],
                                EntityType.PERSON),
                       _entity(names.name_for(names.services, own["service_id"]), own["service_id"],
                               EntityType.SERVICE)]
            rels = [_rel(own["employee_id"], own["service_id"], RelationType.OWNS,
                        observed_at=_parse_dt(own.get("recorded_at")), valid_from=_parse_dt(own.get("valid_from")),
                        valid_to=_parse_dt(own.get("valid_to_exclusive")))]
        rec = SourceRecordInput(source_id=own["source_id"], record_id=own["id"],
                                title=f"Historical service ownership {own['id']}",
                                observed_at=_parse_dt(own.get("recorded_at")), entities=entities, relationships=rels)
        plan.add_record(rec, collection="service_ownership_timeline", stage="7E")

    for dep in _load(root / "dependency_lifecycle_timeline.json")["dependency_lifecycle_timeline"]:
        src, tgt = dep.get("source_service_id"), dep.get("target_service_id")
        if not (src and tgt):
            continue
        entities = [_entity(names.name_for(names.services, src), src, EntityType.SERVICE),
                    _entity(names.name_for(names.services, tgt), tgt, EntityType.SERVICE)]
        # NOTE: this collection's valid-time window lives in `valid_from`/
        # `valid_to_exclusive` (not `operational_valid_from`/`superseded_at`,
        # which are null for every record here) -- using the wrong fields
        # left all 27 records (23 CURRENT_DECLARED + 4 RETIRED_HISTORICAL)
        # with valid_from=valid_to=None, making a retired historical
        # dependency indistinguishable from a currently-declared one.
        rels = [_rel(src, tgt, RelationType.DEPENDS_ON, observed_at=_parse_dt(dep.get("recorded_at")),
                     valid_from=_parse_dt(dep.get("valid_from")),
                     valid_to=_parse_dt(dep.get("valid_to_exclusive")))]
        rec = SourceRecordInput(source_id=dep["source_id"], record_id=dep["id"],
                                title=f"Historical dependency {dep['id']}",
                                observed_at=_parse_dt(dep.get("recorded_at")), entities=entities, relationships=rels)
        plan.add_record(rec, collection="dependency_lifecycle_timeline", stage="7E")

    for ctr in _load(root / "contract_compatibility_timeline.json")["contract_compatibility_timeline"]:
        contract_id = ctr.get("contract_id")
        # ND-12E: the second, independent source of the ~75 contract name
        # conflicts (the first was PR relevant_contract_ids) -- resolve the
        # same names.contracts cache load_7b already populated, instead of a
        # same-as-id fallback that reads as a conflicting claim.
        entities = ([_entity(names.name_for(names.contracts, contract_id), contract_id, EntityType.CONTRACT)]
                   if contract_id else [])
        rec = SourceRecordInput(source_id=ctr.get("source_id", FOUNDATION_SOURCE_ID), record_id=ctr["id"],
                                title=f"Contract compatibility {ctr['id']}",
                                observed_at=_parse_dt(ctr.get("recorded_at")), entities=entities, relationships=[])
        plan.add_record(rec, collection="contract_compatibility_timeline", stage="7E")

    for cfg in _load(root / "configuration_change_events.json")["configuration_change_events"]:
        rec = SourceRecordInput(source_id=cfg.get("source_id", FOUNDATION_SOURCE_ID), record_id=cfg["id"],
                                title=f"Configuration change {cfg['id']}",
                                observed_at=_parse_dt(cfg.get("recorded_at") or cfg.get("observed_at")),
                                entities=[], relationships=[])
        plan.add_record(rec, collection="configuration_change_events", stage="7E")

    for dpl in _load(root / "deployment_state_intervals.json")["deployment_state_intervals"]:
        deployment_id = dpl.get("original_deployment_id")
        artifact_id = dpl.get("artifact_id")
        entities, rels = [], []
        if deployment_id and artifact_id:
            entities = [_entity(names.name_for(names.artifacts, artifact_id), artifact_id, EntityType.ARTIFACT),
                        _entity(names.name_for(names.deployments, deployment_id), deployment_id,
                                EntityType.DEPLOYMENT)]
            rels = [_rel(artifact_id, deployment_id, RelationType.DEPLOYED_AS,
                         observed_at=_parse_dt(dpl.get("recorded_at")),
                         valid_from=_parse_dt(dpl.get("deployed_at")))]
        rec = SourceRecordInput(source_id=dpl.get("source_id", FOUNDATION_SOURCE_ID), record_id=dpl["id"],
                                title=f"Deployment state interval {dpl['id']}",
                                observed_at=_parse_dt(dpl.get("recorded_at")), entities=entities, relationships=rels)
        plan.add_record(rec, collection="deployment_state_intervals", stage="7E")

    for cmp_ in _load(root / "component_version_timeline.json")["component_version_timeline"]:
        rec = SourceRecordInput(source_id=cmp_.get("source_id", FOUNDATION_SOURCE_ID), record_id=cmp_["id"],
                                title=f"Component version observation {cmp_['id']}",
                                observed_at=_parse_dt(cmp_.get("observed_at")), entities=[], relationships=[])
        plan.add_record(rec, collection="component_version_timeline", stage="7E")

    for flow in _load(root / "data_flow_evolution.json")["data_flow_evolution"]:
        producer = flow.get("canonical_source_service_id")
        consumer = flow.get("consumer_service_id") or flow.get("canonical_target_service_id")
        entities, rels = [], []
        if producer and consumer:
            event_id = flow["id"]
            entities = [_entity(flow.get("data_category", event_id), event_id, EntityType.EVENT),
                        _entity(names.name_for(names.services, producer), producer, EntityType.SERVICE),
                        _entity(names.name_for(names.services, consumer), consumer, EntityType.SERVICE)]
            rels = [_rel(producer, event_id, RelationType.PROVIDES, observed_at=_parse_dt(flow.get("observed_at")),
                        valid_from=_parse_dt(flow.get("documented_from")),
                        valid_to=_parse_dt(flow.get("documented_to_exclusive"))),
                   _rel(consumer, event_id, RelationType.CONSUMES, observed_at=_parse_dt(flow.get("observed_at")),
                        valid_from=_parse_dt(flow.get("documented_from")),
                        valid_to=_parse_dt(flow.get("documented_to_exclusive")))]
        rec = SourceRecordInput(source_id=flow.get("source_id", FOUNDATION_SOURCE_ID), record_id=flow["id"],
                                title=f"Data flow {flow['id']}", observed_at=_parse_dt(flow.get("observed_at")),
                                entities=entities, relationships=rels)
        plan.add_record(rec, collection="data_flow_evolution", stage="7E")

    for acl in _load(root / "access_entitlement_timeline.json").get("access_entitlement_timeline", []):
        grant_id = acl.get("grant_id")
        entities, rels = [], []
        if grant_id and acl.get("employee_id"):
            entities = [_entity(f"Grant {grant_id}", grant_id, EntityType.GRANT),
                        _entity(names.name_for(names.employees, acl["employee_id"]), acl["employee_id"],
                                EntityType.PERSON)]
            rels = [_rel(grant_id, acl["employee_id"], RelationType.GRANTS_ACCESS_TO,
                        observed_at=_parse_dt(acl.get("issued_at")), valid_from=_parse_dt(acl.get("issued_at")),
                        valid_to=_parse_dt(acl.get("approved_expiration_at")))]
        rec = SourceRecordInput(source_id=acl.get("source_id", "NEX-SRC-ACCESS_GOVERNANCE"), record_id=acl["id"],
                                title=f"Access entitlement timeline {acl['id']}",
                                observed_at=_parse_dt(acl.get("recorded_at") or acl.get("issued_at")),
                                entities=entities, relationships=rels)
        plan.add_record(rec, collection="access_entitlement_timeline", stage="7E")

    # record_revision_transitions and evidence_correction_events: preserved
    # verbatim in nexus_records only (provenance/meta-record of a *field
    # value change on another record*, not itself a new entity/relationship
    # assertion) — see loader module docstring.
    for rev in _load(root / "record_revision_transitions.json")["record_revision_transitions"]:
        source_id, record_id, observed_at = _access_record(rev)
        rec = SourceRecordInput(source_id=source_id, record_id=record_id, title=f"Revision transition {record_id}",
                                observed_at=observed_at, entities=[], relationships=[])
        plan.add_record(rec, collection="record_revision_transitions", stage="7E")
    for corr in _load(root / "evidence_correction_events.json")["evidence_correction_events"]:
        source_id, record_id, observed_at = _access_record(corr)
        rec = SourceRecordInput(source_id=source_id, record_id=record_id, title=f"Evidence correction {record_id}",
                                observed_at=observed_at, entities=[], relationships=[])
        plan.add_record(rec, collection="evidence_correction_events", stage="7E")


def build_import_plan(stage_7a: Path, stage_7b: Path, stage_7c: Path, stage_7d: Path, stage_7e: Path) -> ImportPlan:
    plan = ImportPlan()
    names = _NameCache()
    load_7a(stage_7a, names, plan)
    load_7b(stage_7b, names, plan)
    load_7c(stage_7c, names, plan)
    load_7d(stage_7d, names, plan)
    load_7e(stage_7e, names, plan)
    return plan
