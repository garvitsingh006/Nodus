"""Production GitHub connector: authorized repository pull requests, issues
and best-effort CODEOWNERS, through the official GitHub REST API.

Scope deliberately excludes raw commit history (would require an unbounded
per-commit files call); PR-level changed files already cover the "code
change touches file" relationship this stage needs. Identity, config,
pagination and persistence all flow through the existing
``backend.app.ingestion.connector``/``sync``/``postgres_store`` framework —
nothing GitHub-specific exists in the graph core or ontology beyond the
generic entity/relation types already defined there.
"""
from __future__ import annotations

import json
import os
from datetime import datetime

from backend.app.graph.models import EntityInput, EntityType, RelationInput, RelationType, SourceRecordInput
from backend.app.ingestion.connector import (
    Connector, ConnectorCapabilities, ConnectorConfig, ConnectorError, ConnectorIdentity, ErrorClass,
    FetchPage, SourceCoverage, permanent,
)
from backend.app.ingestion.http import HttpClient, urllib_transport

GITHUB_API = "https://api.github.com"


def _ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class GitHubConnector:
    """Cursor is a JSON object ``{"repo": <allowlist index>, "phase":
    "pulls"|"issues", "page": <1-based>}``. One page == one bounded HTTP call
    per repo/phase; CODEOWNERS is fetched once per repo on its first page."""

    def __init__(self, connector_id: str, source_id: str, *, http: HttpClient | None = None):
        self._identity = ConnectorIdentity(connector_id=connector_id, source_id=source_id, kind="github")
        self.http = http or HttpClient(urllib_transport)

    def identity(self) -> ConnectorIdentity:
        return self._identity

    def capabilities(self) -> ConnectorCapabilities:
        # GitHub has no reliable "list of everything deleted" feed for PRs/issues
        # (a closed/deleted item simply stops matching future list calls), so
        # deletion detection is never claimed here; absence is never retraction.
        return ConnectorCapabilities(supports_incremental=True, supports_deletion_detection=False, paginated=True)

    def _headers(self, config: ConnectorConfig) -> dict[str, str]:
        headers = {"Accept": "application/vnd.github+json", "User-Agent": "NODUS",
                   "X-GitHub-Api-Version": "2022-11-28"}
        token_env = config.settings.get("token_env")
        token = os.getenv(token_env) if token_env else None
        if token:
            headers["Authorization"] = f"Bearer {token}"
        elif not config.settings.get("allow_public"):
            raise permanent("GitHub connector requires a configured 'token_env' or explicit 'allow_public: true'")
        return headers

    def _repo_entity(self, repo_node_id: str, full_name: str) -> tuple[str, EntityInput]:
        external = f"repo:{repo_node_id}"
        return external, EntityInput(external_id=external, canonical_id=f"github:repo:{repo_node_id}",
                                      type=EntityType.REPOSITORY, name=full_name)

    def _person_entity(self, user: dict) -> tuple[str, EntityInput]:
        external = f"user:{user['id']}"
        return external, EntityInput(external_id=external, canonical_id=f"github:user:{user['id']}",
                                      type=EntityType.PERSON, name=str(user.get("login") or user["id"]))

    def _repo_meta(self, owner: str, repo: str, headers: dict[str, str]) -> tuple[str, str]:
        """Fetched once per page so pulls, issues and CODEOWNERS all key the
        same repository entity off the same stable ``node_id`` — the pulls
        list response nests its own ``base.repo.node_id``, but the issues
        list response does not, so this cannot be derived per-item."""
        item = self.http.request("GET", f"{GITHUB_API}/repos/{owner}/{repo}", headers=headers).json()
        return str(item["node_id"]), item["full_name"]

    def _normalize_pull(self, config: ConnectorConfig, owner: str, repo: str, item: dict,
                        headers: dict[str, str], repo_node: str, repo_full_name: str) -> SourceRecordInput:
        node_id, number = str(item["node_id"]), int(item["number"])
        pr_external = f"pr:{node_id}"
        updated_at = _ts(item["updated_at"])
        repo_external, repo_entity = self._repo_entity(repo_node, repo_full_name)
        entities = [
            EntityInput(external_id=pr_external, canonical_id=f"github:pr:{node_id}",
                       type=EntityType.PULL_REQUEST, name=str(item["title"])),
            repo_entity,
        ]
        relations = [RelationInput(source_external_id=pr_external, target_external_id=repo_external,
                                   type=RelationType.MODIFIES, observed_at=updated_at)]
        author = item.get("user")
        if author:
            author_external, author_entity = self._person_entity(author)
            entities.append(author_entity)
            relations.append(RelationInput(source_external_id=author_external, target_external_id=pr_external,
                                           type=RelationType.AUTHORED, observed_at=updated_at))
        reviews_url = f"{GITHUB_API}/repos/{owner}/{repo}/pulls/{number}/reviews?per_page=100"
        for review in self.http.request("GET", reviews_url, headers=headers).json():
            reviewer = review.get("user")
            if not reviewer or review.get("state") in (None, "PENDING", "DISMISSED"):
                continue
            reviewer_external, reviewer_entity = self._person_entity(reviewer)
            entities.append(reviewer_entity)
            relations.append(RelationInput(
                source_external_id=reviewer_external, target_external_id=pr_external,
                type=RelationType.REVIEWED, observed_at=_ts(review["submitted_at"])))
        files_url = f"{GITHUB_API}/repos/{owner}/{repo}/pulls/{number}/files?per_page=100"
        for changed in self.http.request("GET", files_url, headers=headers).json():
            file_external = f"file:{repo_node}:{changed['filename']}"
            entities.append(EntityInput(external_id=file_external,
                                        canonical_id=f"github:file:{repo_node}:{changed['filename']}",
                                        type=EntityType.FILE, name=changed["filename"]))
            relations.append(RelationInput(source_external_id=pr_external, target_external_id=file_external,
                                           type=RelationType.MODIFIES, observed_at=updated_at))
        unique_entities = list({entity.external_id: entity for entity in entities}.values())
        unique_relations = list({(r.source_external_id, r.target_external_id, r.type): r for r in relations}.values())
        return SourceRecordInput(
            source_id=config.identity.source_id, record_id=f"pull:{owner}/{repo}:{number}",
            title=str(item["title"]), uri=str(item["html_url"]), revision=str(item["updated_at"]),
            observed_at=updated_at, entities=unique_entities, relationships=unique_relations)

    def _normalize_issue(self, config: ConnectorConfig, owner: str, repo: str, item: dict,
                         repo_node: str, repo_full_name: str) -> SourceRecordInput:
        node_id = str(item["node_id"])
        issue_external = f"issue:{node_id}"
        updated_at = _ts(item["updated_at"])
        repo_external, repo_entity = self._repo_entity(repo_node, repo_full_name)
        entities = [
            EntityInput(external_id=issue_external, canonical_id=f"github:issue:{node_id}",
                       type=EntityType.TASK, name=str(item["title"])),
            repo_entity,
        ]
        relations = [RelationInput(source_external_id=issue_external, target_external_id=repo_external,
                                   type=RelationType.PART_OF, observed_at=updated_at)]
        for assignee in item.get("assignees") or []:
            assignee_external, assignee_entity = self._person_entity(assignee)
            entities.append(assignee_entity)
            relations.append(RelationInput(source_external_id=issue_external, target_external_id=assignee_external,
                                           type=RelationType.ASSIGNED_TO, observed_at=updated_at))
        unique_entities = list({entity.external_id: entity for entity in entities}.values())
        return SourceRecordInput(
            source_id=config.identity.source_id, record_id=f"issue:{owner}/{repo}:{item['number']}",
            title=str(item["title"]), uri=str(item["html_url"]), revision=str(item["updated_at"]),
            observed_at=updated_at, entities=unique_entities, relationships=relations)

    def _codeowners(self, config: ConnectorConfig, owner: str, repo: str, headers: dict[str, str],
                    repo_node: str, repo_full_name: str) -> SourceRecordInput | None:
        """Best-effort, global-pattern-only CODEOWNERS parse: only the exact
        top-level ``*`` pattern is honored (never a file-glob that merely
        starts with ``*``, e.g. ``*.py``), and CODEOWNERS' own last-match-wins
        precedence is applied when the file declares ``*`` more than once.
        Path/file-pattern-specific ownership is not resolved in this stage —
        a partial read is reported as such by simply not asserting ownership
        for anything but the one global pattern."""
        for path in ("CODEOWNERS", ".github/CODEOWNERS", "docs/CODEOWNERS"):
            url = f"{GITHUB_API}/repos/{owner}/{repo}/contents/{path}"
            try:
                response = self.http.request("GET", url, headers=headers)
            except ConnectorError as exc:
                if exc.classification == ErrorClass.RETRYABLE:
                    raise
                continue  # not found at this path (or inaccessible) — try the next, or give up quietly
            import base64
            content = base64.b64decode(response.json()["content"]).decode("utf-8", errors="replace")
            repo_external, repo_entity = self._repo_entity(repo_node, repo_full_name)
            entities, relations = [repo_entity], []
            global_owners: list[str] | None = None
            for line in content.splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                pattern, *owners = line.split()
                if pattern == "*":
                    global_owners = owners  # CODEOWNERS precedence: the last "*" line wins, not cumulative
            for handle in global_owners or ():
                handle = handle.lstrip("@")
                if "/" in handle:
                    org, team = handle.split("/", 1)
                    owner_external = f"team:{org}/{team}"
                    entities.append(EntityInput(external_id=owner_external,
                                                canonical_id=f"github:team:{org}/{team}",
                                                type=EntityType.TEAM, name=team))
                else:
                    owner_external = f"login:{handle}"
                    entities.append(EntityInput(external_id=owner_external,
                                                canonical_id=f"github:login:{handle}",
                                                type=EntityType.PERSON, name=handle))
                relations.append(RelationInput(source_external_id=owner_external,
                                               target_external_id=repo_external, type=RelationType.OWNS))
            if not relations:
                return None
            return SourceRecordInput(source_id=config.identity.source_id, record_id=f"codeowners:{owner}/{repo}",
                                     title=f"CODEOWNERS for {owner}/{repo}", entities=entities, relationships=relations)
        return None

    def fetch(self, config: ConnectorConfig, cursor: str | None) -> FetchPage:
        repos = config.settings.get("repositories")
        if not repos:
            raise permanent("GitHub connector requires an explicit 'repositories' allowlist in settings")
        per_page = min(int(config.settings.get("per_page", 25)), 100)
        headers = self._headers(config)
        state = json.loads(cursor) if cursor else {"repo": 0, "phase": "pulls", "page": 1}
        if state["repo"] >= len(repos):
            return FetchPage(coverage=SourceCoverage.COMPLETE, has_more=False)
        owner, repo = repos[state["repo"]].split("/", 1)
        repo_node, repo_full_name = self._repo_meta(owner, repo, headers)
        records: list[SourceRecordInput] = []
        if state["phase"] == "pulls" and state["page"] == 1:
            owners = self._codeowners(config, owner, repo, headers, repo_node, repo_full_name)
            if owners:
                records.append(owners)
        if state["phase"] == "pulls":
            url = (f"{GITHUB_API}/repos/{owner}/{repo}/pulls?state=all&per_page={per_page}"
                   f"&page={state['page']}&sort=updated&direction=asc")
            items = self.http.request("GET", url, headers=headers).json()
            records.extend(self._normalize_pull(config, owner, repo, item, headers, repo_node, repo_full_name)
                          for item in items)
            if len(items) == per_page:
                next_state = {"repo": state["repo"], "phase": "pulls", "page": state["page"] + 1}
            else:
                next_state = {"repo": state["repo"], "phase": "issues", "page": 1}
        else:
            url = (f"{GITHUB_API}/repos/{owner}/{repo}/issues?state=all&filter=all&per_page={per_page}"
                   f"&page={state['page']}&sort=updated&direction=asc")
            raw = self.http.request("GET", url, headers=headers).json()
            non_pr_issues = [item for item in raw if "pull_request" not in item]
            records.extend(self._normalize_issue(config, owner, repo, item, repo_node, repo_full_name)
                          for item in non_pr_issues)
            if len(raw) == per_page:
                next_state = {"repo": state["repo"], "phase": "issues", "page": state["page"] + 1}
            else:
                next_state = {"repo": state["repo"] + 1, "phase": "pulls", "page": 1}
        done = next_state["repo"] >= len(repos)
        return FetchPage(records=tuple(records), next_cursor=None if done else json.dumps(next_state),
                         has_more=not done, coverage=SourceCoverage.PARTIAL)
