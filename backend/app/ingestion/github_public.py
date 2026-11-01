from __future__ import annotations

import json
import os
import re
from datetime import datetime
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from pydantic import BaseModel, Field

from backend.app.graph.models import EntityInput, EntityType, RelationInput, RelationType, SourceRecordInput


_SEGMENT = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")


class GitHubImport(BaseModel):
    owner: str = Field(min_length=1, max_length=100)
    repository: str = Field(min_length=1, max_length=100)
    source_id: str = Field(min_length=1, max_length=200)
    max_pull_requests: int = Field(default=10, ge=1, le=20)


def fetch_public_pull_requests(request: GitHubImport) -> list[dict]:
    if not _SEGMENT.fullmatch(request.owner) or not _SEGMENT.fullmatch(request.repository):
        raise ValueError("Invalid GitHub repository name")
    url = (f"https://api.github.com/repos/{request.owner}/{request.repository}/pulls"
           f"?state=all&per_page={request.max_pull_requests}")
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "NODUS"}
    token = os.getenv("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        with urlopen(Request(url, headers=headers), timeout=15) as response:
            payload = json.load(response)
    except (HTTPError, URLError, TimeoutError) as exc:
        raise RuntimeError("GitHub source unavailable") from exc
    if not isinstance(payload, list):
        raise RuntimeError("GitHub returned an unexpected response")
    return payload


def normalize_pull_request(source_id: str, item: dict) -> SourceRecordInput:
    node_id = str(item["node_id"])
    number = int(item["number"])
    repo = item["base"]["repo"]
    repo_node = str(repo["node_id"])
    author = item.get("user") or {}
    author_id = author.get("id")
    updated_at = datetime.fromisoformat(str(item["updated_at"]).replace("Z", "+00:00"))
    pr_external = f"pr:{node_id}"
    repo_external = f"repo:{repo_node}"
    entities = [
        EntityInput(external_id=pr_external, canonical_id=f"github:pr:{node_id}",
                    type=EntityType.PULL_REQUEST, name=str(item["title"])),
        EntityInput(external_id=repo_external, canonical_id=f"github:repo:{repo_node}",
                    type=EntityType.REPOSITORY, name=str(repo["full_name"])),
    ]
    relations = [RelationInput(source_external_id=pr_external, target_external_id=repo_external,
                               type=RelationType.MODIFIES, observed_at=updated_at)]
    if author_id is not None:
        author_external = f"user:{author_id}"
        entities.append(EntityInput(external_id=author_external,
                                    canonical_id=f"github:user:{author_id}",
                                    type=EntityType.PERSON,
                                    name=str(author.get("login") or author_id)))
        relations.append(RelationInput(source_external_id=author_external,
                                       target_external_id=pr_external,
                                       type=RelationType.AUTHORED, observed_at=updated_at))
    return SourceRecordInput(
        source_id=source_id, record_id=f"pull:{node_id}",
        title=str(item["title"]), uri=str(item["html_url"]),
        revision=str(item["updated_at"]), observed_at=updated_at,
        entities=entities, relationships=relations,
    )
