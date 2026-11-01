from __future__ import annotations

import json

import pytest

from backend.app.graph.models import RelationType
from backend.app.ingestion.connector import ConnectorConfig, ConnectorError, ConnectorIdentity, ErrorClass
from backend.app.ingestion.http import HttpClient, HttpResponse
from backend.app.ingestion.jira_connector import JiraConnector
from backend.tests.test_github_connector import FakeTransport, json_response


def config(**settings) -> ConnectorConfig:
    return ConnectorConfig(
        tenant_id="tenant", workspace_id="workspace",
        identity=ConnectorIdentity(connector_id="jira-1", source_id="jira-acme", kind="jira"),
        settings={"base_url": "https://acme.atlassian.net", "projects": ["ENG"], "max_results": 2,
                 "email_env": "JIRA_EMAIL_TEST", "token_env": "JIRA_TOKEN_TEST", **settings})


@pytest.fixture(autouse=True)
def credentials(monkeypatch):
    monkeypatch.setenv("JIRA_EMAIL_TEST", "bot@acme.com")
    monkeypatch.setenv("JIRA_TOKEN_TEST", "jira-token")


SEARCH_URL = "https://acme.atlassian.net/rest/api/3/search/jql"


def issue(key, issue_id, *, assignee=None, reporter=None, creator=None, links=None):
    return {
        "key": key, "id": issue_id,
        "fields": {
            "summary": f"Summary for {key}", "updated": "2024-02-01T00:00:00+00:00",
            "created": "2024-01-01T00:00:00+00:00",
            "project": {"key": "ENG", "name": "Engineering"},
            "assignee": assignee, "reporter": reporter, "creator": creator, "issuelinks": links or [],
        },
    }


def test_nextpagetoken_pagination_fixes_the_jql_window(monkeypatch):
    transport = FakeTransport()
    connector = JiraConnector("jira-1", "jira-acme", http=HttpClient(transport))
    cfg = config()

    transport.respond("POST", SEARCH_URL, json_response({
        "issues": [issue("ENG-1", "1001"), issue("ENG-2", "1002")],
        "isLast": False, "nextPageToken": "token-2"}))
    transport.respond("POST", SEARCH_URL, json_response({
        "issues": [issue("ENG-3", "1003")], "isLast": True}))

    first = connector.fetch(cfg, None)
    assert first.has_more is True
    state1 = json.loads(first.next_cursor)
    assert state1["next_page_token"] == "token-2" and state1["since"] is None

    second = connector.fetch(cfg, first.next_cursor)
    assert second.has_more is False
    state2 = json.loads(second.next_cursor)
    assert state2["next_page_token"] is None and state2["since"]  # advanced to "now" only once isLast


def test_blocks_direction_is_correct_for_outward_and_inward_links():
    transport = FakeTransport()
    connector = JiraConnector("jira-1", "jira-acme", http=HttpClient(transport))
    cfg = config()
    outward_blocks = {"type": {"name": "Blocks", "inward": "is blocked by", "outward": "blocks"},
                      "outwardIssue": {"id": "2001", "key": "ENG-20", "fields": {"summary": "Downstream"}}}
    inward_blocks = {"type": {"name": "Blocks", "inward": "is blocked by", "outward": "blocks"},
                     "inwardIssue": {"id": "2002", "key": "ENG-21", "fields": {"summary": "Upstream blocker"}}}
    relates = {"type": {"name": "Relates", "inward": "relates to", "outward": "relates to"},
              "outwardIssue": {"id": "2003", "key": "ENG-22", "fields": {"summary": "Related"}}}
    transport.respond("POST", SEARCH_URL, json_response({
        "issues": [issue("ENG-1", "1001", links=[outward_blocks, inward_blocks, relates])], "isLast": True}))

    page = connector.fetch(cfg, None)
    [record] = page.records
    by_type = {(rel.type, rel.source_external_id, rel.target_external_id) for rel in record.relationships}
    assert (RelationType.BLOCKS, "issue:1001", "issue:2001") in by_type  # ENG-1 blocks ENG-20 (outward)
    assert (RelationType.BLOCKS, "issue:2002", "issue:1001") in by_type  # ENG-21 blocks ENG-1 (inward)
    assert (RelationType.REFERENCES, "issue:1001", "issue:2003") in by_type  # non-blocking link


def test_assignee_reporter_and_project_relationships():
    transport = FakeTransport()
    connector = JiraConnector("jira-1", "jira-acme", http=HttpClient(transport))
    cfg = config()
    transport.respond("POST", SEARCH_URL, json_response({
        "issues": [issue("ENG-1", "1001",
                         assignee={"accountId": "acc-1", "displayName": "Dana"},
                         reporter={"accountId": "acc-2", "displayName": "Ravi"},
                         creator={"accountId": "acc-3", "displayName": "Sam"})],
        "isLast": True}))
    [record] = connector.fetch(cfg, None).records
    by_type = {(rel.type, rel.source_external_id, rel.target_external_id) for rel in record.relationships}
    assert (RelationType.ASSIGNED_TO, "issue:1001", "user:acc-1") in by_type
    assert (RelationType.PART_OF, "issue:1001", "project:ENG") in by_type
    # The reporter (can be administratively reassigned) must never be asserted
    # as authorship on its own — only the source's own "creator" field is.
    assert (RelationType.REPORTED, "user:acc-2", "issue:1001") in by_type
    assert (RelationType.AUTHORED, "user:acc-3", "issue:1001") in by_type
    assert not any(rel[0] == RelationType.AUTHORED and rel[1] == "user:acc-2" for rel in by_type)


def test_reporter_without_a_separate_creator_is_never_treated_as_authored():
    transport = FakeTransport()
    connector = JiraConnector("jira-1", "jira-acme", http=HttpClient(transport))
    cfg = config()
    transport.respond("POST", SEARCH_URL, json_response({
        "issues": [issue("ENG-2", "1002", reporter={"accountId": "acc-9", "displayName": "Lee"})],
        "isLast": True}))
    [record] = connector.fetch(cfg, None).records
    by_type = {(rel.type, rel.source_external_id, rel.target_external_id) for rel in record.relationships}
    assert (RelationType.REPORTED, "user:acc-9", "issue:1002") in by_type
    assert not any(rel[0] == RelationType.AUTHORED for rel in by_type)


def test_auth_and_rate_limit_classification():
    transport = FakeTransport()
    connector = JiraConnector("jira-1", "jira-acme", http=HttpClient(transport))
    cfg = config()
    transport.respond("POST", SEARCH_URL, HttpResponse(401))
    with pytest.raises(ConnectorError) as excinfo:
        connector.fetch(cfg, None)
    assert excinfo.value.classification == ErrorClass.PERMANENT

    transport2 = FakeTransport()
    connector2 = JiraConnector("jira-1", "jira-acme", http=HttpClient(transport2))
    transport2.respond("POST", SEARCH_URL, HttpResponse(429, {"Retry-After": "10"}))
    with pytest.raises(ConnectorError) as excinfo:
        connector2.fetch(cfg, None)
    assert excinfo.value.classification == ErrorClass.RETRYABLE


def test_missing_configuration_is_rejected():
    transport = FakeTransport()
    connector = JiraConnector("jira-1", "jira-acme", http=HttpClient(transport))
    bad = ConnectorConfig(tenant_id="t", workspace_id="w",
                          identity=ConnectorIdentity(connector_id="jira-1", source_id="jira-acme", kind="jira"),
                          settings={})
    with pytest.raises(ConnectorError) as excinfo:
        connector.fetch(bad, None)
    assert excinfo.value.classification == ErrorClass.PERMANENT
    assert transport.calls == []
