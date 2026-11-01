"""Production Jira Cloud connector using the enhanced JQL search endpoint
(``POST /rest/api/3/search/jql``, ``nextPageToken``-based pagination — the
endpoint that replaced the deprecated offset-based ``/search``).

Same framework boundary as the GitHub connector: this module only knows how
to authenticate, query and normalize; durability, dedup and graph projection
all come from ``backend.app.ingestion.sync.SyncCoordinator``.
"""
from __future__ import annotations

import base64
import json
import os
from datetime import datetime, timezone

from backend.app.graph.models import EntityInput, EntityType, RelationInput, RelationType, SourceRecordInput
from backend.app.ingestion.connector import (
    ConnectorCapabilities, ConnectorConfig, ConnectorIdentity, FetchPage, SourceCoverage, permanent,
)
from backend.app.ingestion.http import HttpClient, urllib_transport

FIELDS = ("summary", "issuetype", "status", "assignee", "reporter", "creator", "project",
         "created", "updated", "issuelinks")


def _ts(value: str) -> datetime:
    return datetime.fromisoformat(value)


class JiraConnector:
    """Cursor is a JSON object ``{"since": <iso timestamp or null>,
    "next_page_token": <token or null>}``. ``since`` is fixed for the
    duration of one paginated JQL traversal and only advances to "now" once
    that traversal reports ``isLast``, so a fresh run never misses an issue
    updated while paging through the previous one."""

    def __init__(self, connector_id: str, source_id: str, *, http: HttpClient | None = None):
        self._identity = ConnectorIdentity(connector_id=connector_id, source_id=source_id, kind="jira")
        self.http = http or HttpClient(urllib_transport)

    def identity(self) -> ConnectorIdentity:
        return self._identity

    def capabilities(self) -> ConnectorCapabilities:
        # Jira's search results are eventually consistent and offer no
        # authoritative tombstone feed for issue deletion in this endpoint,
        # so deletion is never inferred from an issue's absence.
        return ConnectorCapabilities(supports_incremental=True, supports_deletion_detection=False, paginated=True)

    def _headers(self, config: ConnectorConfig) -> dict[str, str]:
        email = os.getenv(config.settings.get("email_env", ""), "")
        token = os.getenv(config.settings.get("token_env", ""), "")
        if not email or not token:
            raise permanent("Jira connector requires 'email_env' and 'token_env' to resolve to real credentials")
        basic = base64.b64encode(f"{email}:{token}".encode()).decode()
        return {"Authorization": f"Basic {basic}", "Content-Type": "application/json", "Accept": "application/json"}

    def _entity(self, kind: EntityType, external: str, canonical: str, name: str) -> EntityInput:
        return EntityInput(external_id=external, canonical_id=canonical, type=kind, name=name)

    def _normalize_issue(self, config: ConnectorConfig, item: dict) -> SourceRecordInput:
        key, issue_id = item["key"], str(item["id"])
        fields = item["fields"]
        issue_external = f"issue:{issue_id}"
        updated_at = _ts(fields["updated"])
        base_url = config.settings["base_url"].rstrip("/")
        entities = [self._entity(EntityType.TASK, issue_external, f"jira:issue:{issue_id}",
                                 f"{key}: {fields['summary']}")]
        relations: list[RelationInput] = []

        project = fields.get("project")
        if project:
            project_external = f"project:{project['key']}"
            entities.append(self._entity(EntityType.SYSTEM, project_external,
                                         f"jira:project:{project['key']}", project.get("name", project["key"])))
            relations.append(RelationInput(source_external_id=issue_external, target_external_id=project_external,
                                           type=RelationType.PART_OF, observed_at=updated_at))

        # Jira's "reporter" can be administratively reassigned after creation,
        # so it is never treated as authorship on its own (R05: an activity
        # signal must not be silently promoted into a stronger claim). Only
        # the source's own "creator" field — who actually made the issue —
        # is asserted as AUTHORED; reporter gets the weaker, distinct REPORTED.
        creator = fields.get("creator")
        if creator:
            creator_external = f"user:{creator['accountId']}"
            entities.append(self._entity(EntityType.PERSON, creator_external,
                                         f"jira:user:{creator['accountId']}", creator.get("displayName", "")))
            relations.append(RelationInput(source_external_id=creator_external, target_external_id=issue_external,
                                           type=RelationType.AUTHORED, observed_at=_ts(fields["created"])))

        reporter = fields.get("reporter")
        if reporter:
            reporter_external = f"user:{reporter['accountId']}"
            entities.append(self._entity(EntityType.PERSON, reporter_external,
                                         f"jira:user:{reporter['accountId']}", reporter.get("displayName", "")))
            relations.append(RelationInput(source_external_id=reporter_external, target_external_id=issue_external,
                                           type=RelationType.REPORTED, observed_at=_ts(fields["created"])))

        assignee = fields.get("assignee")
        if assignee:
            assignee_external = f"user:{assignee['accountId']}"
            entities.append(self._entity(EntityType.PERSON, assignee_external,
                                         f"jira:user:{assignee['accountId']}", assignee.get("displayName", "")))
            relations.append(RelationInput(source_external_id=issue_external, target_external_id=assignee_external,
                                           type=RelationType.ASSIGNED_TO, observed_at=updated_at))

        for link in fields.get("issuelinks") or []:
            link_type = link["type"]
            if "outwardIssue" in link:
                other = link["outwardIssue"]
                other_external = f"issue:{other['id']}"
                phrase = link_type.get("outward", "")
                src, dst = issue_external, other_external
            elif "inwardIssue" in link:
                other = link["inwardIssue"]
                other_external = f"issue:{other['id']}"
                phrase = link_type.get("outward", "")
                src, dst = other_external, issue_external  # the inward issue is the one performing "outward"
            else:
                continue
            entities.append(self._entity(EntityType.TASK, other_external, f"jira:issue:{other['id']}",
                                         other.get("fields", {}).get("summary", other.get("key", ""))))
            relation_type = RelationType.BLOCKS if phrase.lower() == "blocks" else RelationType.REFERENCES
            relations.append(RelationInput(source_external_id=src, target_external_id=dst, type=relation_type))

        unique_entities = list({entity.external_id: entity for entity in entities}.values())
        return SourceRecordInput(
            source_id=config.identity.source_id, record_id=f"issue:{key}",
            title=f"{key}: {fields['summary']}", uri=f"{base_url}/browse/{key}",
            revision=fields["updated"], observed_at=updated_at, entities=unique_entities, relationships=relations)

    def fetch(self, config: ConnectorConfig, cursor: str | None) -> FetchPage:
        projects = config.settings.get("projects")
        if not projects:
            raise permanent("Jira connector requires an explicit 'projects' allowlist in settings")
        base_url = config.settings.get("base_url")
        if not base_url:
            raise permanent("Jira connector requires 'base_url' (the tenant's Jira Cloud site)")
        per_page = min(int(config.settings.get("max_results", 50)), 100)
        headers = self._headers(config)
        state = json.loads(cursor) if cursor else {"since": None, "next_page_token": None}
        jql = f"project in ({','.join(projects)})"
        if state["since"]:
            jql += f' AND updated >= "{state["since"]}"'
        jql += " ORDER BY updated ASC"
        body: dict = {"jql": jql, "maxResults": per_page, "fields": list(FIELDS)}
        if state["next_page_token"]:
            body["nextPageToken"] = state["next_page_token"]
        response = self.http.request("POST", f"{base_url.rstrip('/')}/rest/api/3/search/jql",
                                     headers=headers, body=json.dumps(body).encode())
        data = response.json()
        records = tuple(self._normalize_issue(config, item) for item in data.get("issues", []))
        is_last = bool(data.get("isLast", True))
        if is_last:
            next_cursor = json.dumps({"since": datetime.now(timezone.utc).isoformat(), "next_page_token": None})
            has_more = False
        else:
            next_cursor = json.dumps({"since": state["since"], "next_page_token": data.get("nextPageToken")})
            has_more = True
        # A JQL time-window search is never claimed as a complete enumeration —
        # clock skew/eventual consistency at the boundary is handled by the
        # coordinator's own content-hash idempotency, not by this connector.
        return FetchPage(records=records, next_cursor=next_cursor, has_more=has_more, coverage=SourceCoverage.PARTIAL)
