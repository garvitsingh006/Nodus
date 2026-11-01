"""Production Confluence Cloud connector using the REST API v2 (pages, spaces)
with a narrowly-scoped v1 call for the one operation v2 does not expose yet
(per-content read restrictions: ``GET /rest/api/content/{id}/restriction/byOperation/read``).

Same framework boundary as the GitHub/Jira connectors: this module only
fetches and normalizes; durability, dedup and graph projection all come from
``backend.app.ingestion.sync.SyncCoordinator``.

Permission model (see module docstring on ``_effective_acl``): a page's
restrictions are resolved by walking its ancestor chain, mapped through an
explicitly configured, admin-curated ``identity_map`` (never an automatic
external-ID-to-NODUS-identity conversion), and any restriction that cannot be
positively resolved fails closed rather than defaulting to open.
"""
from __future__ import annotations

import base64
import json
import os
import re
from datetime import datetime, timezone
from urllib.parse import urljoin, urlsplit

from backend.app.graph.models import EntityInput, EntityType, RelationInput, RelationType, SourceRecordInput
from backend.app.ingestion.connector import (
    ConnectorCapabilities, ConnectorConfig, ConnectorError, ConnectorIdentity, ErrorClass,
    FetchPage, SourceCoverage, permanent,
)
from backend.app.ingestion.http import HttpClient, urllib_transport

# A page whose restrictions cannot be positively resolved (ancestor walk
# incomplete, or the restriction API itself refused the connector's own
# credentials) is assigned this sentinel ACL: it matches no real Principal,
# so the page is visible only to an ADMIN/allow_all reader, never to an
# ordinary subject — fail closed, never fail open.
_UNRESOLVED_RESTRICTION_ACL = ("__confluence_permission_unresolved__",)

_JIRA_MACRO_KEY = re.compile(r'<ac:parameter ac:name="key">([A-Z][A-Z0-9]+-\d+)</ac:parameter>')


def _ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class ConfluenceConnector:
    """Cursor is a JSON object ``{"space": <allowlist index>, "next_url": <a
    pre-validated absolute URL, or null to start that space>}``."""

    def __init__(self, connector_id: str, source_id: str, *, http: HttpClient | None = None):
        self._identity = ConnectorIdentity(connector_id=connector_id, source_id=source_id, kind="confluence")
        self.http = http or HttpClient(urllib_transport)
        self._restriction_cache: dict[str, tuple[frozenset[str], str | None]] = {}

    def identity(self) -> ConnectorIdentity:
        return self._identity

    def capabilities(self) -> ConnectorCapabilities:
        # Confluence's v2 pages listing has no deletion/tombstone feed, so a
        # page's absence from a listing is never treated as a retraction.
        return ConnectorCapabilities(supports_incremental=True, supports_deletion_detection=False, paginated=True)

    def _headers(self, config: ConnectorConfig) -> dict[str, str]:
        email = os.getenv(config.settings.get("email_env", ""), "")
        token = os.getenv(config.settings.get("token_env", ""), "")
        if not email or not token:
            raise permanent("Confluence connector requires 'email_env' and 'token_env' to resolve to real credentials")
        basic = base64.b64encode(f"{email}:{token}".encode()).decode()
        return {"Authorization": f"Basic {basic}", "Accept": "application/json"}

    def _trusted_url(self, base_url: str, link: str) -> str:
        """Resolves a provider-supplied pagination link and rejects it unless
        its origin matches the tenant's own configured Confluence site —
        this is the only thing standing between a provider response and an
        SSRF-style redirect to an arbitrary host."""
        resolved = urljoin(base_url, link)
        base_origin = (urlsplit(base_url).scheme, urlsplit(base_url).netloc)
        resolved_origin = (urlsplit(resolved).scheme, urlsplit(resolved).netloc)
        if resolved_origin != base_origin:
            raise permanent(f"Refusing to follow a pagination link to an untrusted origin: {resolved}")
        return resolved

    def _resolve_space_id(self, base_url: str, space_key: str, headers: dict[str, str]) -> str:
        url = f"{base_url}/wiki/api/v2/spaces?keys={space_key}&limit=1"
        data = self.http.request("GET", url, headers=headers).json()
        results = data.get("results") or []
        if not results:
            raise permanent(f"Confluence space {space_key!r} not found or not authorized for this connector")
        return str(results[0]["id"])

    def _effective_acl(self, config: ConnectorConfig, page_item: dict, headers: dict[str, str],
                       base_url: str) -> list[str]:
        identity_map: dict[str, str] = config.settings.get("identity_map", {})
        page_id = str(page_item["id"])
        restricted_ids: set[str] = set()
        current_id: str | None = page_id
        seen: set[str] = set()
        for _ in range(20):
            if current_id is None:
                break
            if current_id in seen:
                return list(_UNRESOLVED_RESTRICTION_ACL)  # a cycle is not a resolvable hierarchy
            seen.add(current_id)
            cached = self._restriction_cache.get(current_id)
            if cached is None:
                try:
                    restriction_url = f"{base_url}/wiki/rest/api/content/{current_id}/restriction/byOperation/read"
                    restriction = self.http.request("GET", restriction_url, headers=headers).json()
                except ConnectorError as exc:
                    if exc.classification == ErrorClass.PERMANENT:
                        return list(_UNRESOLVED_RESTRICTION_ACL)
                    raise
                ids = {u["accountId"] for u in restriction.get("user", {}).get("results", [])}
                ids |= {g["name"] for g in restriction.get("group", {}).get("results", [])}
                if current_id == page_id:
                    parent_id = page_item.get("parentId")
                else:
                    try:
                        parent_item = self.http.request(
                            "GET", f"{base_url}/wiki/api/v2/pages/{current_id}", headers=headers).json()
                    except ConnectorError as exc:
                        if exc.classification == ErrorClass.PERMANENT:
                            return list(_UNRESOLVED_RESTRICTION_ACL)
                        raise
                    parent_id = parent_item.get("parentId")
                self._restriction_cache[current_id] = (frozenset(ids), parent_id)
                cached = self._restriction_cache[current_id]
            ids, parent_id = cached
            restricted_ids |= ids
            current_id = parent_id
        else:
            return list(_UNRESOLVED_RESTRICTION_ACL)  # ancestor chain too deep to resolve within budget

        if not restricted_ids:
            return []
        # A non-empty restriction set whose members are all unmapped must
        # NEVER resolve to an empty ACL list: an empty list means "open" to
        # every reader elsewhere in this codebase (zero acl_subjects == no
        # restriction), which would silently flip a restricted page to
        # public. Fail closed instead whenever nothing maps.
        mapped = sorted({identity_map[subject] for subject in restricted_ids if subject in identity_map})
        return mapped or list(_UNRESOLVED_RESTRICTION_ACL)

    def _normalize_page(self, config: ConnectorConfig, item: dict, headers: dict[str, str],
                        base_url: str) -> SourceRecordInput:
        page_id = str(item["id"])
        page_external = f"page:{page_id}"
        title = str(item["title"])
        version = item.get("version") or {}
        updated_at = _ts(version.get("createdAt") or item.get("createdAt"))
        author_id = version.get("authorId") or item.get("authorId")
        entities = [EntityInput(external_id=page_external, canonical_id=f"confluence:page:{page_id}",
                                type=EntityType.DOCUMENT, name=title)]
        relations: list[RelationInput] = []

        if author_id:
            author_external = f"user:{author_id}"
            entities.append(EntityInput(external_id=author_external, canonical_id=f"confluence:user:{author_id}",
                                        type=EntityType.PERSON, name=author_id))
            relations.append(RelationInput(source_external_id=author_external, target_external_id=page_external,
                                           type=RelationType.AUTHORED, observed_at=updated_at))

        parent_id = item.get("parentId")
        if parent_id:
            parent_external = f"page:{parent_id}"
            entities.append(EntityInput(external_id=parent_external, canonical_id=f"confluence:page:{parent_id}",
                                        type=EntityType.DOCUMENT, name=f"Confluence page {parent_id}"))
            relations.append(RelationInput(source_external_id=page_external, target_external_id=parent_external,
                                           type=RelationType.PART_OF, observed_at=updated_at))

        # Only a structured Jira-issue macro reference is parsed (never free
        # text): this is a provisional, connector-scoped identity for the
        # referenced key, not a merge with the real Jira-ingested entity —
        # that would require an actual cross-source key-to-ID resolution,
        # which this connector does not perform (see R07/cross-source note).
        body = ((item.get("body") or {}).get("storage") or {}).get("value", "") or ""
        for jira_key in sorted(set(_JIRA_MACRO_KEY.findall(body))):
            reference_external = f"jira-ref:{jira_key}"
            entities.append(EntityInput(external_id=reference_external,
                                        canonical_id=f"confluence:jira-reference:{jira_key}",
                                        type=EntityType.TASK, name=jira_key))
            relations.append(RelationInput(source_external_id=page_external, target_external_id=reference_external,
                                           type=RelationType.REFERENCES, observed_at=updated_at))

        acl = self._effective_acl(config, item, headers, base_url)
        unique_entities = list({entity.external_id: entity for entity in entities}.values())
        web_path = (item.get("_links") or {}).get("webui", "")
        return SourceRecordInput(
            source_id=config.identity.source_id, record_id=f"page:{page_id}", title=title,
            uri=f"{base_url.rstrip('/')}/wiki{web_path}" if web_path else None,
            revision=str(version.get("number", "")), observed_at=updated_at,
            acl_subjects=acl, entities=unique_entities, relationships=relations)

    def fetch(self, config: ConnectorConfig, cursor: str | None) -> FetchPage:
        spaces = config.settings.get("spaces")
        if not spaces:
            raise permanent("Confluence connector requires an explicit 'spaces' (key) allowlist in settings")
        base_url = config.settings.get("base_url")
        if not base_url:
            raise permanent("Confluence connector requires 'base_url' (the tenant's Confluence Cloud site)")
        per_page = min(int(config.settings.get("limit", 25)), 100)
        headers = self._headers(config)
        state = json.loads(cursor) if cursor else {"space": 0, "next_url": None}
        if state["space"] >= len(spaces):
            return FetchPage(coverage=SourceCoverage.COMPLETE, has_more=False)
        space_key = spaces[state["space"]]
        if state["next_url"] is None:
            space_id = self._resolve_space_id(base_url, space_key, headers)
            url = (f"{base_url}/wiki/api/v2/pages?space-id={space_id}&status=current"
                   f"&body-format=storage&limit={per_page}")
        else:
            url = state["next_url"]
        response = self.http.request("GET", url, headers=headers)
        data = response.json()
        records = tuple(self._normalize_page(config, item, headers, base_url) for item in data.get("results", []))
        next_link = (data.get("_links") or {}).get("next")
        if next_link:
            next_state = {"space": state["space"], "next_url": self._trusted_url(base_url, next_link)}
        else:
            next_state = {"space": state["space"] + 1, "next_url": None}
        done = next_state["space"] >= len(spaces) and next_state["next_url"] is None
        return FetchPage(records=records, next_cursor=None if done else json.dumps(next_state),
                         has_more=not done, coverage=SourceCoverage.PARTIAL)
