from __future__ import annotations

import hashlib
import time
from collections import deque
from datetime import datetime, timezone
from typing import Any

from backend.app.graph import ontology
from backend.app.graph.authority import AuthorityRule, resolve_entity_field_authority
from backend.app.graph.driver import Neo4jStore
from backend.app.graph.models import EntityType, Entity, Evidence, GraphPage, Relationship, SourceRecordInput


def scoped_key(*parts: str) -> str:
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()


def iso(value: datetime | None) -> str | None:
    return value.astimezone(timezone.utc).isoformat() if value else None


def _record_fingerprint(record: SourceRecordInput) -> str:
    """Content hash of a source record's meaning, ignoring ingestion wall-clock
    time. Used to tell a true content change apart from a byte-identical
    replay, so idempotent replays never advance the graph revision."""
    entities_repr = tuple(sorted(
        (entity.external_id, entity.canonical_id or "", entity.type.value, entity.name,
         entity.description, iso(entity.valid_from) or "", iso(entity.valid_to) or "")
        for entity in record.entities
    ))
    relationships_repr = tuple(sorted(
        (rel.source_external_id, rel.target_external_id, rel.type.value, rel.origin.value,
         rel.derivation_rule_version or "", iso(rel.observed_at) or "",
         iso(rel.valid_from) or "", iso(rel.valid_to) or "")
        for rel in record.relationships
    ))
    return scoped_key(
        record.title, record.uri or "", repr(sorted(record.acl_subjects)),
        iso(record.observed_at) or "", record.revision or "",
        repr(entities_repr), repr(relationships_repr),
    )


class GraphRepository:
    """All graph reads and writes require one authorized tenant/workspace context."""

    def __init__(self, store: Neo4jStore, tenant_id: str, workspace_id: str,
                 subject: str, allow_all: bool = False, can_evidence: bool = False,
                 authority_rules: tuple[AuthorityRule, ...] = (),
                 authorized_source_ids: frozenset[str] | None = None):
        if not tenant_id or not workspace_id or not subject:
            raise ValueError("Tenant, workspace and subject are required")
        self.store = store
        self.tenant_id = tenant_id
        self.workspace_id = workspace_id
        self.subject = subject
        self.allow_all = allow_all
        self.can_evidence = can_evidence or allow_all
        self.authority_rules = authority_rules
        # None preserves prior behavior exactly (no source restriction beyond
        # acl_subjects) — every existing caller that doesn't pass this gets the
        # unchanged query plan. A non-None set additionally requires the
        # asserting/relating SourceRecord's own source_id to be a member,
        # independent of and in addition to the acl_subjects allowlist.
        self.authorized_source_ids = authorized_source_ids

    def _params(self) -> dict[str, Any]:
        return dict(tenant=self.tenant_id, workspace=self.workspace_id,
                    subject=self.subject, allow_all=self.allow_all,
                    as_of=iso(datetime.now(timezone.utc)),
                    authorized_sources=(
                        sorted(self.authorized_source_ids)
                        if self.authorized_source_ids is not None else None))

    def _revision_key(self) -> str:
        return scoped_key(self.tenant_id, self.workspace_id, "graph_revision")

    def _current_revision_in_tx(self, tx) -> int:
        row = tx.run("MATCH (g:GraphRevision {key:$key}) RETURN g.revision AS revision",
                      key=self._revision_key()).single()
        return row["revision"] if row else 0

    def _bump_revision_in_tx(self, tx) -> int:
        """Advance the workspace-scoped graph revision. Only ever called from
        within a write transaction that also performs the factual mutation, so
        the revision bump commits atomically with that mutation (same tx)."""
        row = tx.run("""
            MERGE (g:GraphRevision {key:$key})
            ON CREATE SET g.tenant_id=$tenant, g.workspace_id=$workspace, g.revision=0
            SET g.revision = g.revision + 1
            RETURN g.revision AS revision
            """, key=self._revision_key(), tenant=self.tenant_id, workspace=self.workspace_id).single()
        return row["revision"]

    def graph_revision(self) -> int:
        return self.store.read(self._current_revision_in_tx)

    def _resolve(self, entity_id: str, assertions: list[dict[str, Any]], active: bool = True) -> Entity:
        values: dict[str, Any] = {}
        conflicts: dict[str, list[str]] = {}
        for field in ("type", "name", "description"):
            choices = sorted({claim[field] for claim in assertions if claim.get(field) is not None})
            if len(choices) == 1:
                values[field] = choices[0]
            elif len(choices) > 1:
                values[field] = None
                conflicts[field] = choices
            else:
                values[field] = None
        authority: dict[str, dict] = {}
        if conflicts:
            # Authority never resolves a "type" conflict against itself — if
            # the type is contested, which type's rules would even apply is
            # itself undecided, so every field falls back to "no_rule" rather
            # than guessing a type to key rules off of.
            resolved_type = EntityType(values["type"]) if values.get("type") else None
            for field in conflicts:
                candidate_sources = frozenset(
                    claim["source_id"] for claim in assertions if claim.get(field) is not None
                    and claim["source_id"] is not None)
                verdict = resolve_entity_field_authority(
                    tenant_id=self.tenant_id, workspace_id=self.workspace_id, entity_type=resolved_type,
                    candidate_source_ids=candidate_sources, rules=self.authority_rules)
                authority[field] = verdict.model_dump(mode="json")
        return Entity(id=entity_id, type=values["type"], name=values["name"],
                      description=values["description"], active=active, conflicts=conflicts,
                      authority=authority)

    def _relationship(self, rel, source_id: str, target_id: str) -> Relationship:
        return Relationship(
            id=rel["key"], source_id=source_id, target_id=target_id, type=rel.type,
            active=bool(rel.get("active")), observed_at=rel.get("observed_at"),
            valid_from=rel.get("valid_from"), valid_to=rel.get("valid_to"),
            source_id_ref=rel.get("source_id") if self.can_evidence else None,
            record_id=rel.get("record_id") if self.can_evidence else None,
            ontology_version=rel.get("ontology_version"),
            origin=rel.get("origin"), derivation_rule_version=rel.get("derivation_rule_version"),
            ingested_at=rel.get("ingested_at"),
        )

    @staticmethod
    def _claim(row) -> dict[str, Any]:
        assertion = row["a"]
        return dict(type=assertion.get("type"), name=assertion.get("name"),
                    description=assertion.get("description"), source_id=row["source_id"])

    def _entity_in_tx(self, tx, entity_id: str, history: bool = False) -> Entity | None:
        key = scoped_key(self.tenant_id, self.workspace_id, entity_id)
        rows = list(tx.run("""
            MATCH (e:Entity {key:$key, tenant_id:$tenant, workspace_id:$workspace})
            MATCH (s:SourceRecord)-[a:ASSERTS]->(e)
            WHERE ($history OR (s.active=true AND a.active=true
              AND (a.valid_from IS NULL OR a.valid_from <= $as_of)
              AND (a.valid_to IS NULL OR a.valid_to > $as_of)))
              AND s.tenant_id=$tenant AND s.workspace_id=$workspace
              AND ($allow_all OR size(s.acl_subjects)=0 OR $subject IN s.acl_subjects)
              AND ($allow_all OR size(coalesce(a.acl_subjects, []))=0 OR $subject IN a.acl_subjects)
              AND ($allow_all OR $authorized_sources IS NULL OR s.source_id IN $authorized_sources)
            RETURN e.public_id AS id, a, s.active AS source_active, s.source_id AS source_id
            """, key=key, history=history, **self._params()))
        if not rows:
            return None
        current = [row for row in rows if row["source_active"] and row["a"].get("active")
                   and (not row["a"].get("valid_from") or row["a"]["valid_from"] <= iso(datetime.now(timezone.utc)))
                   and (not row["a"].get("valid_to") or row["a"]["valid_to"] > iso(datetime.now(timezone.utc)))]
        selected = current if current else rows
        return self._resolve(rows[0]["id"], [self._claim(row) for row in selected], active=bool(current))

    def get_entity(self, entity_id: str) -> Entity | None:
        return self.store.read(lambda tx: self._entity_in_tx(tx, entity_id))

    def get_entity_history(self, entity_id: str) -> Entity | None:
        return self.store.read(lambda tx: self._entity_in_tx(tx, entity_id, history=True))

    def assertions(self, entity_id: str, limit: int = 50) -> dict[str, Any]:
        if not self.can_evidence:
            raise PermissionError("Evidence permission required")
        key = scoped_key(self.tenant_id, self.workspace_id, entity_id)
        def work(tx):
            rows = list(tx.run("""
                MATCH (s:SourceRecord {tenant_id:$tenant, workspace_id:$workspace})
                      -[a:ASSERTS]->(e:Entity {key:$key, tenant_id:$tenant, workspace_id:$workspace})
                WHERE ($allow_all OR size(s.acl_subjects)=0 OR $subject IN s.acl_subjects)
                  AND ($allow_all OR size(coalesce(a.acl_subjects, []))=0 OR $subject IN a.acl_subjects)
                  AND ($allow_all OR $authorized_sources IS NULL OR s.source_id IN $authorized_sources)
                RETURN s, a ORDER BY s.source_id, s.record_id, a.observed_at DESC LIMIT $fetch
                """, key=key, **self._params(), fetch=limit + 1))
            items = []
            now = iso(datetime.now(timezone.utc))
            for row in rows[:limit]:
                s, a = row["s"], row["a"]
                current = bool(s["active"] and a["active"]
                               and (not a.get("valid_from") or a["valid_from"] <= now)
                               and (not a.get("valid_to") or a["valid_to"] > now))
                items.append({
                    "source_id": s["source_id"], "record_id": s["record_id"],
                    "type": a.get("type"), "name": a.get("name"),
                    "description": a.get("description"), "current": current,
                    "observed_at": a.get("observed_at"), "ingested_at": a.get("ingested_at"),
                    "valid_from": a.get("valid_from"), "valid_to": a.get("valid_to"),
                })
            return {"items": items, "truncated": len(rows) > limit}
        return self.store.read(work)

    def ingest(self, record: SourceRecordInput) -> dict[str, Any]:
        external_ids = {entity.external_id for entity in record.entities}
        if len(external_ids) != len(record.entities):
            raise ValueError("Duplicate entity external_id in source record")
        if any(rel.source_external_id not in external_ids or rel.target_external_id not in external_ids
               for rel in record.relationships):
            raise ValueError("Relationship endpoints must be in the same source record")
        if len({(rel.source_external_id, rel.target_external_id, rel.type) for rel in record.relationships}) != len(record.relationships):
            raise ValueError("Duplicate relationship in source record")
        entity_types = {entity.external_id: entity.type for entity in record.entities}
        for rel in record.relationships:
            ontology.validate_relationship(
                rel.type, entity_types[rel.source_external_id], entity_types[rel.target_external_id],
                rel.origin)
        tenant, workspace = self.tenant_id, self.workspace_id
        record_key = scoped_key(tenant, workspace, record.source_id, record.record_id)
        content_hash = _record_fingerprint(record)
        entity_keys = {
            entity.external_id: scoped_key(
                tenant, workspace, entity.canonical_id or f"{record.source_id}::{entity.external_id}"
            ) for entity in record.entities
        }
        public_ids = {
            entity.external_id: entity.canonical_id or f"{record.source_id}::{entity.external_id}"
            for entity in record.entities
        }

        def work(tx):
            ingested_at = iso(datetime.now(timezone.utc))
            # MERGE acquires the node's write lock for the rest of this transaction;
            # the WITH captures the pre-write active/content_hash atomically under
            # that same lock, so a concurrent identical replay can never race past
            # this check before seeing the committed result of the first writer.
            previous = tx.run("""
                MERGE (s:SourceRecord {key:$key})
                ON CREATE SET s.content_hash=''
                WITH s, s.active AS was_active, s.content_hash AS old_hash
                SET s.tenant_id=$tenant, s.workspace_id=$workspace, s.source_id=$source_id,
                    s.record_id=$record_id, s.title=$title, s.uri=$uri,
                    s.acl_subjects=$acl, s.observed_at=$observed_at,
                    s.revision=$revision, s.active=true, s.content_hash=$content_hash,
                    s.ingested_at=$ingested_at
                RETURN was_active, old_hash
                """, key=record_key, tenant=tenant, workspace=workspace,
                source_id=record.source_id, record_id=record.record_id, title=record.title,
                uri=record.uri, acl=record.acl_subjects, observed_at=iso(record.observed_at),
                revision=record.revision, content_hash=content_hash, ingested_at=ingested_at).single()
            unchanged = bool(previous["was_active"] and previous["old_hash"] == content_hash)
            if unchanged:
                return {"source_id": record.source_id, "record_id": record.record_id,
                        "entities": len(record.entities), "relationships": len(record.relationships),
                        "graph_revision": self._current_revision_in_tx(tx), "changed": False}
            tx.run("""
                MATCH (s:SourceRecord {key:$key})-[a:ASSERTS]->(:Entity)
                SET a.active=false
                """, key=record_key).consume()
            tx.run("""
                MATCH (:Entity)-[r]->(:Entity)
                WHERE r.record_key=$key AND r.tenant_id=$tenant AND r.workspace_id=$workspace
                SET r.active=false
                """, key=record_key, tenant=tenant, workspace=workspace).consume()
            for entity in record.entities:
                entity_key = entity_keys[entity.external_id]
                assertion_key = scoped_key(
                    record_key, entity_key, entity.type.value, entity.name,
                    entity.description, iso(entity.valid_from) or "", iso(entity.valid_to) or "",
                    record.revision or "", *sorted(record.acl_subjects),
                )
                tx.run("""
                    MATCH (s:SourceRecord {key:$record_key, tenant_id:$tenant, workspace_id:$workspace})
                    MERGE (e:Entity {key:$entity_key})
                    SET e.tenant_id=$tenant, e.workspace_id=$workspace, e.public_id=$public_id
                    MERGE (s)-[a:ASSERTS {key:$assertion_key}]->(e)
                    SET a.active=true, a.type=$type, a.name=$name,
                        a.description=$description, a.observed_at=$observed_at,
                        a.acl_subjects=$acl, a.ingested_at=$ingested_at,
                        a.valid_from=$valid_from, a.valid_to=$valid_to
                    """, record_key=record_key, entity_key=entity_key,
                    assertion_key=assertion_key, tenant=tenant, workspace=workspace,
                    public_id=public_ids[entity.external_id], type=entity.type.value,
                    name=entity.name, description=entity.description,
                    acl=record.acl_subjects, ingested_at=ingested_at,
                    observed_at=iso(record.observed_at),
                    valid_from=iso(entity.valid_from), valid_to=iso(entity.valid_to)).consume()
            for rel in record.relationships:
                left, right = entity_keys[rel.source_external_id], entity_keys[rel.target_external_id]
                relation_key = scoped_key(
                    record_key, left, right, rel.type.value,
                    iso(rel.observed_at or record.observed_at) or "",
                    iso(rel.valid_from) or "", iso(rel.valid_to) or "",
                    record.revision or "", *sorted(record.acl_subjects),
                )
                # The token is an enum value. Request strings are never interpolated.
                tx.run(f"""
                    MATCH (a:Entity {{key:$left, tenant_id:$tenant, workspace_id:$workspace}})
                    MATCH (b:Entity {{key:$right, tenant_id:$tenant, workspace_id:$workspace}})
                    MERGE (a)-[r:{rel.type.value} {{key:$relation_key}}]->(b)
                    SET r.tenant_id=$tenant, r.workspace_id=$workspace, r.record_key=$record_key,
                        r.source_id=$source_id, r.record_id=$record_id, r.active=true,
                        r.observed_at=$observed_at, r.valid_from=$valid_from, r.valid_to=$valid_to,
                        r.acl_subjects=$acl, r.ontology_version=$ontology_version,
                        r.origin=$origin, r.derivation_rule_version=$derivation_rule_version,
                        r.ingested_at=$ingested_at
                    """, left=left, right=right, tenant=tenant, workspace=workspace,
                    relation_key=relation_key, record_key=record_key,
                    source_id=record.source_id, record_id=record.record_id,
                    acl=record.acl_subjects, ontology_version=ontology.ONTOLOGY_VERSION,
                    origin=rel.origin.value, derivation_rule_version=rel.derivation_rule_version,
                    ingested_at=ingested_at,
                    observed_at=iso(rel.observed_at or record.observed_at),
                    valid_from=iso(rel.valid_from), valid_to=iso(rel.valid_to)).consume()
            revision = self._bump_revision_in_tx(tx)
            return {"source_id": record.source_id, "record_id": record.record_id,
                    "entities": len(record.entities), "relationships": len(record.relationships),
                    "graph_revision": revision, "changed": True}

        return self.store.write(work)

    def retract_record(self, source_id: str, record_id: str) -> bool:
        key = scoped_key(self.tenant_id, self.workspace_id, source_id, record_id)

        def work(tx):
            # WITH captures the pre-write active flag under the same lock MATCH+SET
            # holds for the rest of this transaction, so a repeat retraction of an
            # already-inactive record is detected without a race.
            found = tx.run("""
                MATCH (s:SourceRecord {key:$key, tenant_id:$tenant, workspace_id:$workspace})
                WITH s, s.active AS was_active
                SET s.active=false
                RETURN s.key AS key, was_active
                """, key=key, **self._params()).single()
            if not found:
                return False
            if not found["was_active"]:
                return True
            tx.run("MATCH (s:SourceRecord {key:$key})-[a:ASSERTS]->() SET a.active=false",
                   key=key).consume()
            tx.run("""
                MATCH (:Entity)-[r]->(:Entity)
                WHERE r.record_key=$key AND r.tenant_id=$tenant AND r.workspace_id=$workspace
                SET r.active=false
                """, key=key, **self._params()).consume()
            self._bump_revision_in_tx(tx)
            return True

        return self.store.write(work)

    def search(self, text: str, limit: int = 25, offset: int = 0) -> dict[str, Any]:
        if not 1 <= limit <= 100 or not 0 <= offset <= 10000:
            raise ValueError("Invalid pagination")
        needle = text.casefold()

        def work(tx):
            rows = list(tx.run("""
                MATCH (e:Entity {tenant_id:$tenant, workspace_id:$workspace})
                MATCH (s:SourceRecord)-[a:ASSERTS {active:true}]->(e)
                WHERE s.active=true AND s.tenant_id=$tenant AND s.workspace_id=$workspace
                  AND (a.valid_from IS NULL OR a.valid_from <= $as_of)
                  AND (a.valid_to IS NULL OR a.valid_to > $as_of)
                  AND ($allow_all OR size(s.acl_subjects)=0 OR $subject IN s.acl_subjects)
                  AND ($allow_all OR size(coalesce(a.acl_subjects, []))=0 OR $subject IN a.acl_subjects)
                  AND ($allow_all OR $authorized_sources IS NULL OR s.source_id IN $authorized_sources)
                WITH e, collect({type:a.type, name:a.name, description:a.description, source_id:s.source_id}) AS claims
                WHERE toLower(e.public_id) CONTAINS $needle
                   OR any(claim IN claims WHERE toLower(claim.name) CONTAINS $needle)
                RETURN e.public_id AS id, claims
                ORDER BY id SKIP $offset LIMIT $fetch
                """, **self._params(), needle=needle, offset=offset, fetch=limit + 1))
            return {"items": [self._resolve(row["id"], row["claims"]) for row in rows[:limit]],
                    "has_more": len(rows) > limit, "limit": limit, "offset": offset}

        return self.store.read(work)

    def neighborhood(self, entity_id: str, limit: int = 50,
                     include_history: bool = False, reverse_only: bool = False,
                     reverse_relation_type: str = "DEPENDS_ON") -> GraphPage:
        """``reverse_relation_type`` only applies when ``reverse_only`` is set;
        it generalizes what was previously a DEPENDS_ON-only reverse lookup
        (still the default, so every existing caller is unaffected) to any
        single directed relation type -- e.g. ``blockers_of`` reuses this for
        BLOCKS instead of introducing a second traversal engine."""
        if not 1 <= limit <= 100:
            raise ValueError("Invalid limit")
        key = scoped_key(self.tenant_id, self.workspace_id, entity_id)

        def work(tx):
            if self._entity_in_tx(tx, entity_id, history=include_history) is None:
                return GraphPage(nodes=[], relationships=[])
            rows = list(tx.run("""
                MATCH (root:Entity {key:$key, tenant_id:$tenant, workspace_id:$workspace})
                MATCH (root)-[r]-(other:Entity {tenant_id:$tenant, workspace_id:$workspace})
                WHERE r.tenant_id=$tenant AND r.workspace_id=$workspace
                  AND ($history OR (r.active=true
                    AND (r.valid_from IS NULL OR r.valid_from <= $as_of)
                    AND (r.valid_to IS NULL OR r.valid_to > $as_of)))
                  AND (NOT $reverse OR (endNode(r)=root AND type(r)=$reverse_type))
                  AND EXISTS {
                    MATCH (source:SourceRecord {key:r.record_key, tenant_id:$tenant, workspace_id:$workspace})
                    WHERE $allow_all OR size(source.acl_subjects)=0 OR $subject IN source.acl_subjects
                  }
                  AND ($allow_all OR size(coalesce(r.acl_subjects, []))=0 OR $subject IN r.acl_subjects)
                  AND ($allow_all OR $authorized_sources IS NULL OR r.source_id IN $authorized_sources)
                RETURN startNode(r).public_id AS source_id, endNode(r).public_id AS target_id, r
                ORDER BY r.active DESC, type(r), r.key LIMIT $fetch
                """, key=key, **self._params(), history=include_history,
                reverse=reverse_only, reverse_type=reverse_relation_type, fetch=limit + 1))
            nodes: dict[str, Entity] = {}
            edges: list[Relationship] = []
            for row in rows[:limit]:
                source_id, target_id = row["source_id"], row["target_id"]
                source = nodes.get(source_id) or self._entity_in_tx(tx, source_id, history=include_history)
                target = nodes.get(target_id) or self._entity_in_tx(tx, target_id, history=include_history)
                if source is None or target is None:
                    continue
                nodes[source_id], nodes[target_id] = source, target
                edges.append(self._relationship(row["r"], source_id, target_id))
            return GraphPage(nodes=list(nodes.values()), relationships=edges,
                             truncated=len(rows) > limit)

        return self.store.read(work)

    def reverse_dependencies(self, entity_id: str, limit: int = 50) -> GraphPage:
        return self.neighborhood(entity_id, limit=limit, reverse_only=True)

    def blockers_of(self, entity_id: str, limit: int = 50, include_history: bool = False) -> GraphPage:
        """ND-12B: entities with a BLOCKS edge targeting ``entity_id`` -- i.e.
        work that must be addressed before ``entity_id`` can proceed. Same
        traversal engine and authorization guarantees as
        ``reverse_dependencies`` (a blocker whose own source record is not
        authorized for the caller is silently excluded by
        ``neighborhood``'s existing node-resolution check, never revealed)."""
        return self.neighborhood(entity_id, limit=limit, include_history=include_history,
                                 reverse_only=True, reverse_relation_type="BLOCKS")

    def relationship(self, relation_id: str) -> Relationship | None:
        def work(tx):
            row = tx.run("""
                MATCH (a:Entity {tenant_id:$tenant, workspace_id:$workspace})-[r]->
                      (b:Entity {tenant_id:$tenant, workspace_id:$workspace})
                WHERE r.key=$key AND r.active=true AND r.tenant_id=$tenant AND r.workspace_id=$workspace
                  AND (r.valid_from IS NULL OR r.valid_from <= $as_of)
                  AND (r.valid_to IS NULL OR r.valid_to > $as_of)
                  AND EXISTS {
                    MATCH (s:SourceRecord {key:r.record_key, tenant_id:$tenant, workspace_id:$workspace, active:true})
                    WHERE $allow_all OR size(s.acl_subjects)=0 OR $subject IN s.acl_subjects
                  }
                  AND ($allow_all OR size(coalesce(r.acl_subjects, []))=0 OR $subject IN r.acl_subjects)
                  AND ($allow_all OR $authorized_sources IS NULL OR r.source_id IN $authorized_sources)
                RETURN a.public_id AS source_id, b.public_id AS target_id, r
                """, key=relation_id, **self._params()).single()
            if row is None:
                return None
            if self._entity_in_tx(tx, row["source_id"]) is None or self._entity_in_tx(tx, row["target_id"]) is None:
                return None
            return self._relationship(row["r"], row["source_id"], row["target_id"])
        return self.store.read(work)

    def paths(self, source_id: str, target_id: str, max_depth: int = 3, limit: int = 10) -> dict[str, Any]:
        if not 1 <= max_depth <= 4 or not 1 <= limit <= 20:
            raise ValueError("Invalid path bounds")
        if self.get_entity(source_id) is None or self.get_entity(target_id) is None:
            return {"paths": [], "truncated": False, "coverage": "source_backed_only"}
        frontier = deque([(source_id, [source_id], [])])
        found: list[dict[str, Any]] = []
        visited = 0
        truncated = False
        deadline = time.monotonic() + 10
        while frontier and len(found) < limit and visited < 100 and time.monotonic() < deadline:
            current, ids, edges = frontier.popleft()
            visited += 1
            if len(edges) >= max_depth:
                continue
            graph = self.neighborhood(current, limit=50)
            truncated = truncated or graph.truncated
            for relation in graph.relationships:
                neighbor = relation.target_id if relation.source_id == current else relation.source_id
                if neighbor in ids:
                    continue
                next_ids, next_edges = ids + [neighbor], edges + [relation]
                if neighbor == target_id:
                    nodes = [self.get_entity(item) for item in next_ids]
                    if all(nodes):
                        found.append({"nodes": nodes, "relationships": next_edges})
                    if len(found) >= limit:
                        break
                elif len(next_edges) < max_depth and len(frontier) < 200:
                    frontier.append((neighbor, next_ids, next_edges))
                else:
                    truncated = True
        if frontier or visited >= 100 or time.monotonic() >= deadline:
            truncated = True
        return {"paths": found, "truncated": truncated, "coverage": "source_backed_only"}

    def evidence(self, entity_id: str, limit: int = 50) -> dict[str, Any]:
        if not self.can_evidence:
            raise PermissionError("Evidence permission required")
        key = scoped_key(self.tenant_id, self.workspace_id, entity_id)
        def work(tx):
            rows = list(tx.run("""
                MATCH (s:SourceRecord {tenant_id:$tenant, workspace_id:$workspace, active:true})
                      -[a:ASSERTS {active:true}]->(e:Entity {key:$key, tenant_id:$tenant, workspace_id:$workspace})
                WHERE ($allow_all OR size(s.acl_subjects)=0 OR $subject IN s.acl_subjects)
                  AND ($allow_all OR size(coalesce(a.acl_subjects, []))=0 OR $subject IN a.acl_subjects)
                  AND ($allow_all OR $authorized_sources IS NULL OR s.source_id IN $authorized_sources)
                  AND (a.valid_from IS NULL OR a.valid_from <= $as_of)
                  AND (a.valid_to IS NULL OR a.valid_to > $as_of)
                RETURN DISTINCT s ORDER BY s.source_id, s.record_id LIMIT $fetch
                """, key=key, **self._params(), fetch=limit + 1))
            return {"items": [Evidence(source_id=row["s"]["source_id"], record_id=row["s"]["record_id"],
                                       title=row["s"]["title"], uri=row["s"].get("uri"),
                                       observed_at=row["s"].get("observed_at")) for row in rows[:limit]],
                    "truncated": len(rows) > limit}
        return self.store.read(work)

    def sources(self, limit: int = 50) -> dict[str, Any]:
        if not self.can_evidence:
            raise PermissionError("Evidence permission required")
        def work(tx):
            rows = list(tx.run("""
                MATCH (s:SourceRecord {tenant_id:$tenant, workspace_id:$workspace, active:true})
                WHERE ($allow_all OR size(s.acl_subjects)=0 OR $subject IN s.acl_subjects)
                  AND ($allow_all OR $authorized_sources IS NULL OR s.source_id IN $authorized_sources)
                RETURN s.source_id AS id, count(s) AS records ORDER BY id LIMIT $fetch
                """, **self._params(), fetch=limit + 1))
            return {"items": [dict(row) for row in rows[:limit]], "truncated": len(rows) > limit}
        return self.store.read(work)

    def source_record(self, source_id: str, record_id: str) -> dict[str, Any] | None:
        if not self.can_evidence:
            raise PermissionError("Evidence permission required")
        key = scoped_key(self.tenant_id, self.workspace_id, source_id, record_id)
        def work(tx):
            row = tx.run("""
                MATCH (s:SourceRecord {key:$key, tenant_id:$tenant, workspace_id:$workspace, active:true})
                WHERE ($allow_all OR size(s.acl_subjects)=0 OR $subject IN s.acl_subjects)
                  AND ($allow_all OR $authorized_sources IS NULL OR s.source_id IN $authorized_sources)
                RETURN s
                """, key=key, **self._params()).single()
            if row is None:
                return None
            s = row["s"]
            return {"source_id": s["source_id"], "record_id": s["record_id"],
                    "title": s["title"], "uri": s.get("uri"),
                    "revision": s.get("revision"), "observed_at": s.get("observed_at")}
        return self.store.read(work)
