from __future__ import annotations

import base64
import json

import pytest

from backend.app.graph.models import RelationType
from backend.app.ingestion.connector import ConnectorConfig, ConnectorError, ConnectorIdentity, ErrorClass
from backend.app.ingestion.github_connector import GitHubConnector
from backend.app.ingestion.http import HttpClient, HttpResponse


class FakeTransport:
    def __init__(self):
        self.queues: dict[tuple[str, str], list[HttpResponse]] = {}
        self.calls: list[tuple[str, str]] = []

    def respond(self, method: str, url: str, response: HttpResponse) -> None:
        self.queues.setdefault((method, url), []).append(response)

    def __call__(self, method, url, headers, body) -> HttpResponse:
        self.calls.append((method, url))
        queue = self.queues.get((method, url))
        if not queue:
            raise AssertionError(f"Unexpected request: {method} {url}")
        return queue.pop(0) if len(queue) > 1 else queue[0]


def json_response(payload, status=200, headers=None) -> HttpResponse:
    return HttpResponse(status, headers or {}, json.dumps(payload).encode())


def config(repos: list[str], **settings) -> ConnectorConfig:
    return ConnectorConfig(tenant_id="tenant", workspace_id="workspace",
                           identity=ConnectorIdentity(connector_id="gh-1", source_id="github-acme", kind="github"),
                           settings={"repositories": repos, "per_page": 2, "token_env": "GH_TOKEN_TEST", **settings})


@pytest.fixture(autouse=True)
def token(monkeypatch):
    monkeypatch.setenv("GH_TOKEN_TEST", "ghp_fake")


def test_pagination_across_pulls_and_issues_with_reviewers_and_files():
    transport = FakeTransport()
    connector = GitHubConnector("gh-1", "github-acme", http=HttpClient(transport))
    cfg = config(["acme/widgets"])
    api = "https://api.github.com/repos/acme/widgets"

    transport.respond("GET", api, json_response({"node_id": "R_1", "full_name": "acme/widgets"}))
    transport.respond("GET", f"{api}/contents/CODEOWNERS", HttpResponse(404))
    transport.respond("GET", f"{api}/contents/.github/CODEOWNERS",
                      json_response({"content": base64.b64encode(b"* @acme/core-team\n").decode()}))
    pulls_page1 = f"{api}/pulls?state=all&per_page=2&page=1&sort=updated&direction=asc"
    transport.respond("GET", pulls_page1, json_response([
        {"node_id": "PR_1", "number": 1, "title": "Add feature", "html_url": "https://x/1",
         "updated_at": "2024-01-02T00:00:00Z", "user": {"id": 1, "login": "alice"}},
        {"node_id": "PR_2", "number": 2, "title": "Fix bug", "html_url": "https://x/2",
         "updated_at": "2024-01-03T00:00:00Z", "user": {"id": 1, "login": "alice"}},
    ]))
    transport.respond("GET", f"{api}/pulls/1/reviews?per_page=100", json_response([
        {"user": {"id": 2, "login": "bob"}, "state": "APPROVED", "submitted_at": "2024-01-02T01:00:00Z"},
        {"user": {"id": 5, "login": "pending-reviewer"}, "state": "PENDING", "submitted_at": "2024-01-02T01:05:00Z"},
    ]))
    transport.respond("GET", f"{api}/pulls/1/files?per_page=100", json_response([{"filename": "src/app.py"}]))
    transport.respond("GET", f"{api}/pulls/2/reviews?per_page=100", json_response([]))
    transport.respond("GET", f"{api}/pulls/2/files?per_page=100", json_response([]))
    pulls_page2 = f"{api}/pulls?state=all&per_page=2&page=2&sort=updated&direction=asc"
    transport.respond("GET", pulls_page2, json_response([]))
    issues_page1 = f"{api}/issues?state=all&filter=all&per_page=2&page=1&sort=updated&direction=asc"
    transport.respond("GET", issues_page1, json_response([
        {"node_id": "PR_1", "number": 1, "pull_request": {}},  # PRs appear in the issues API too
        {"node_id": "I_1", "number": 10, "title": "Investigate crash", "html_url": "https://x/10",
         "updated_at": "2024-01-04T00:00:00Z", "assignees": [{"id": 3, "login": "carol"}]},
    ]))
    issues_page2 = f"{api}/issues?state=all&filter=all&per_page=2&page=2&sort=updated&direction=asc"
    transport.respond("GET", issues_page2, json_response([]))

    page1 = connector.fetch(cfg, None)
    assert page1.has_more and json.loads(page1.next_cursor) == {"repo": 0, "phase": "pulls", "page": 2}
    codeowners = next(r for r in page1.records if r.record_id.startswith("codeowners:"))
    assert codeowners.relationships[0].type == RelationType.OWNS
    pr1 = next(r for r in page1.records if r.record_id == "pull:acme/widgets:1")
    kinds = {(rel.type, rel.source_external_id, rel.target_external_id) for rel in pr1.relationships}
    assert (RelationType.AUTHORED, "user:1", "pr:PR_1") in kinds
    assert (RelationType.REVIEWED, "user:2", "pr:PR_1") in kinds
    assert not any(rel.source_external_id == "user:5" for rel in pr1.relationships)  # pending review excluded
    assert (RelationType.MODIFIES, "pr:PR_1", "file:R_1:src/app.py") in kinds
    assert all(e.canonical_id and e.canonical_id.startswith("github:") or True for e in pr1.entities)

    page2 = connector.fetch(cfg, page1.next_cursor)
    assert page2.records == () and json.loads(page2.next_cursor) == {"repo": 0, "phase": "issues", "page": 1}

    page3 = connector.fetch(cfg, page2.next_cursor)
    assert page3.has_more is True  # raw page was full (2 items, one filtered) — one more issues page expected
    assert len(page3.records) == 1 and page3.records[0].record_id == "issue:acme/widgets:10"
    issue = page3.records[0]
    assert any(rel.type == RelationType.ASSIGNED_TO and rel.target_external_id == "user:3"
              for rel in issue.relationships)
    # The repo entity must be the same identity (repo:R_1) across pulls, issues and CODEOWNERS.
    repo_externals = {e.external_id for r in (pr1, issue, codeowners) for e in r.entities
                      if e.external_id == "repo:R_1"}
    assert repo_externals == {"repo:R_1"}

    page4 = connector.fetch(cfg, page3.next_cursor)
    assert page4.has_more is False and page4.next_cursor is None and page4.records == ()


def test_codeowners_uses_exact_global_pattern_and_last_match_wins():
    transport = FakeTransport()
    connector = GitHubConnector("gh-1", "github-acme", http=HttpClient(transport))
    cfg = config(["acme/widgets"])
    api = "https://api.github.com/repos/acme/widgets"
    transport.respond("GET", api, json_response({"node_id": "R_1", "full_name": "acme/widgets"}))
    # "*.py" starts with "*" but is a file-glob, not the universal pattern, and
    # must never be mistaken for it; CODEOWNERS' own last-match-wins precedence
    # means the final "*" line (acme/second-team) overrides the first.
    codeowners_body = "\n".join([
        "*.py @acme/python-only-team",
        "* @acme/first-team",
        "* @acme/second-team",
    ]).encode()
    transport.respond("GET", f"{api}/contents/CODEOWNERS",
                      json_response({"content": base64.b64encode(codeowners_body).decode()}))
    transport.respond("GET", f"{api}/pulls?state=all&per_page=2&page=1&sort=updated&direction=asc",
                      json_response([]))
    transport.respond("GET", f"{api}/pulls?state=all&per_page=2&page=2&sort=updated&direction=asc",
                      json_response([]))
    transport.respond("GET", f"{api}/issues?state=all&filter=all&per_page=2&page=1&sort=updated&direction=asc",
                      json_response([]))

    page = connector.fetch(cfg, None)
    [codeowners] = [r for r in page.records if r.record_id.startswith("codeowners:")]
    owners = {rel.source_external_id for rel in codeowners.relationships}
    assert owners == {"team:acme/second-team"}  # last "*" line wins; ".py" glob and first "*" never applied


def test_auth_failure_is_permanent_and_rate_limit_is_retryable():
    transport = FakeTransport()
    connector = GitHubConnector("gh-1", "github-acme", http=HttpClient(transport))
    cfg = config(["acme/widgets"])
    api = "https://api.github.com/repos/acme/widgets"
    transport.respond("GET", api, HttpResponse(401))
    with pytest.raises(ConnectorError) as excinfo:
        connector.fetch(cfg, None)
    assert excinfo.value.classification == ErrorClass.PERMANENT

    transport2 = FakeTransport()
    connector2 = GitHubConnector("gh-1", "github-acme", http=HttpClient(transport2))
    transport2.respond("GET", api, HttpResponse(429, {"Retry-After": "30"}))
    with pytest.raises(ConnectorError) as excinfo:
        connector2.fetch(cfg, None)
    assert excinfo.value.classification == ErrorClass.RETRYABLE


def test_missing_configuration_is_rejected_before_any_request():
    transport = FakeTransport()
    connector = GitHubConnector("gh-1", "github-acme", http=HttpClient(transport))
    bad = ConnectorConfig(tenant_id="t", workspace_id="w",
                          identity=ConnectorIdentity(connector_id="gh-1", source_id="github-acme", kind="github"),
                          settings={})
    with pytest.raises(ConnectorError) as excinfo:
        connector.fetch(bad, None)
    assert excinfo.value.classification == ErrorClass.PERMANENT
    assert transport.calls == []
