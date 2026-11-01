from __future__ import annotations

import json

import pytest

from backend.app.graph.models import RelationType
from backend.app.ingestion.confluence_connector import ConfluenceConnector, _UNRESOLVED_RESTRICTION_ACL
from backend.app.ingestion.connector import ConnectorConfig, ConnectorError, ConnectorIdentity, ErrorClass
from backend.app.ingestion.http import HttpClient, HttpResponse
from backend.tests.test_github_connector import FakeTransport, json_response

BASE = "https://acme.atlassian.net"


def config(**settings) -> ConnectorConfig:
    return ConnectorConfig(
        tenant_id="tenant", workspace_id="workspace",
        identity=ConnectorIdentity(connector_id="conf-1", source_id="confluence-acme", kind="confluence"),
        settings={"base_url": BASE, "spaces": ["ENG"], "limit": 2,
                 "email_env": "CONF_EMAIL_TEST", "token_env": "CONF_TOKEN_TEST", **settings})


@pytest.fixture(autouse=True)
def credentials(monkeypatch):
    monkeypatch.setenv("CONF_EMAIL_TEST", "bot@acme.com")
    monkeypatch.setenv("CONF_TOKEN_TEST", "conf-token")


def page_item(page_id, title, *, parent_id=None, author_id="acc-1", body="", number=1):
    return {
        "id": page_id, "title": title, "parentId": parent_id,
        "version": {"number": number, "createdAt": "2024-01-05T00:00:00.000Z", "authorId": author_id},
        "body": {"storage": {"value": body}},
        "_links": {"webui": f"/spaces/ENG/pages/{page_id}"},
    }


def no_restriction():
    return json_response({"user": {"results": []}, "group": {"results": []}})


def restriction(users=(), groups=()):
    return json_response({"user": {"results": [{"accountId": u} for u in users]},
                          "group": {"results": [{"name": g} for g in groups]}})


def test_page_normalization_with_author_parent_and_jira_macro_reference():
    transport = FakeTransport()
    connector = ConfluenceConnector("conf-1", "confluence-acme", http=HttpClient(transport))
    cfg = config()
    transport.respond("GET", f"{BASE}/wiki/api/v2/spaces?keys=ENG&limit=1",
                      json_response({"results": [{"id": "111", "key": "ENG"}]}))
    body = ('<p>See <ac:structured-macro ac:name="jira">'
           '<ac:parameter ac:name="key">ENG-42</ac:parameter></ac:structured-macro></p>')
    transport.respond("GET", f"{BASE}/wiki/api/v2/pages?space-id=111&status=current&body-format=storage&limit=2",
                      json_response({"results": [page_item("501", "Design Doc", parent_id="400", body=body)]}))
    transport.respond("GET", f"{BASE}/wiki/rest/api/content/501/restriction/byOperation/read", no_restriction())
    transport.respond("GET", f"{BASE}/wiki/rest/api/content/400/restriction/byOperation/read", no_restriction())
    transport.respond("GET", f"{BASE}/wiki/api/v2/pages/400", json_response({"id": "400", "parentId": None}))

    page = connector.fetch(cfg, None)
    [record] = page.records
    assert record.acl_subjects == []
    by_type = {(r.type, r.source_external_id, r.target_external_id) for r in record.relationships}
    assert (RelationType.AUTHORED, "user:acc-1", "page:501") in by_type
    assert (RelationType.PART_OF, "page:501", "page:400") in by_type
    assert (RelationType.REFERENCES, "page:501", "jira-ref:ENG-42") in by_type
    reference = next(e for e in record.entities if e.external_id == "jira-ref:ENG-42")
    assert reference.canonical_id == "confluence:jira-reference:ENG-42"  # provisional, never the real Jira identity


def test_pagination_trusts_only_the_configured_origin():
    transport = FakeTransport()
    connector = ConfluenceConnector("conf-1", "confluence-acme", http=HttpClient(transport))
    cfg = config()
    transport.respond("GET", f"{BASE}/wiki/api/v2/spaces?keys=ENG&limit=1",
                      json_response({"results": [{"id": "111", "key": "ENG"}]}))
    transport.respond("GET", f"{BASE}/wiki/api/v2/pages?space-id=111&status=current&body-format=storage&limit=2",
                      json_response({"results": [page_item("1", "A"), page_item("2", "B")],
                                    "_links": {"next": "/wiki/api/v2/pages?cursor=next&limit=2"}}))
    for page_id in ("1", "2"):
        transport.respond("GET", f"{BASE}/wiki/rest/api/content/{page_id}/restriction/byOperation/read",
                          no_restriction())

    page = connector.fetch(cfg, None)
    assert page.has_more is True
    state = json.loads(page.next_cursor)
    assert state["next_url"] == f"{BASE}/wiki/api/v2/pages?cursor=next&limit=2"

    # A malicious/misconfigured absolute next-link pointing off-origin must be rejected.
    transport2 = FakeTransport()
    connector2 = ConfluenceConnector("conf-1", "confluence-acme", http=HttpClient(transport2))
    transport2.respond("GET", f"{BASE}/wiki/api/v2/spaces?keys=ENG&limit=1",
                       json_response({"results": [{"id": "111", "key": "ENG"}]}))
    transport2.respond("GET", f"{BASE}/wiki/api/v2/pages?space-id=111&status=current&body-format=storage&limit=2",
                       json_response({"results": [], "_links": {"next": "https://evil.example.com/steal"}}))
    with pytest.raises(ConnectorError) as excinfo:
        connector2.fetch(cfg, None)
    assert excinfo.value.classification == ErrorClass.PERMANENT


def test_restricted_page_maps_identities_and_fails_closed_when_unmapped():
    transport = FakeTransport()
    connector = ConfluenceConnector("conf-1", "confluence-acme",
                                    http=HttpClient(transport))
    cfg = config(identity_map={"acc-restricted": "alice-nodus"})
    transport.respond("GET", f"{BASE}/wiki/api/v2/spaces?keys=ENG&limit=1",
                      json_response({"results": [{"id": "111", "key": "ENG"}]}))
    transport.respond("GET", f"{BASE}/wiki/api/v2/pages?space-id=111&status=current&body-format=storage&limit=2",
                      json_response({"results": [page_item("10", "Mapped"), page_item("20", "Unmapped")]}))
    transport.respond("GET", f"{BASE}/wiki/rest/api/content/10/restriction/byOperation/read",
                      restriction(users=["acc-restricted"]))
    transport.respond("GET", f"{BASE}/wiki/rest/api/content/20/restriction/byOperation/read",
                      restriction(users=["acc-unknown"]))

    page = connector.fetch(cfg, None)
    mapped_record = next(r for r in page.records if r.record_id == "page:10")
    unmapped_record = next(r for r in page.records if r.record_id == "page:20")
    assert mapped_record.acl_subjects == ["alice-nodus"]
    assert unmapped_record.acl_subjects == list(_UNRESOLVED_RESTRICTION_ACL)  # fails closed, never public


def test_restriction_api_permission_failure_fails_closed():
    transport = FakeTransport()
    connector = ConfluenceConnector("conf-1", "confluence-acme", http=HttpClient(transport))
    cfg = config()
    transport.respond("GET", f"{BASE}/wiki/api/v2/spaces?keys=ENG&limit=1",
                      json_response({"results": [{"id": "111", "key": "ENG"}]}))
    transport.respond("GET", f"{BASE}/wiki/api/v2/pages?space-id=111&status=current&body-format=storage&limit=2",
                      json_response({"results": [page_item("30", "Secret-ish")]}))
    transport.respond("GET", f"{BASE}/wiki/rest/api/content/30/restriction/byOperation/read", HttpResponse(403))

    page = connector.fetch(cfg, None)
    [record] = page.records
    assert record.acl_subjects == list(_UNRESOLVED_RESTRICTION_ACL)


def test_missing_configuration_and_unauthorized_space_are_rejected():
    transport = FakeTransport()
    connector = ConfluenceConnector("conf-1", "confluence-acme", http=HttpClient(transport))
    bad = ConnectorConfig(tenant_id="t", workspace_id="w",
                          identity=ConnectorIdentity(connector_id="conf-1", source_id="confluence-acme",
                                                     kind="confluence"),
                          settings={})
    with pytest.raises(ConnectorError) as excinfo:
        connector.fetch(bad, None)
    assert excinfo.value.classification == ErrorClass.PERMANENT
    assert transport.calls == []

    cfg = config()
    transport.respond("GET", f"{BASE}/wiki/api/v2/spaces?keys=ENG&limit=1", json_response({"results": []}))
    with pytest.raises(ConnectorError) as excinfo:
        connector.fetch(cfg, None)
    assert excinfo.value.classification == ErrorClass.PERMANENT


def test_auth_and_rate_limit_classification():
    transport = FakeTransport()
    connector = ConfluenceConnector("conf-1", "confluence-acme", http=HttpClient(transport))
    cfg = config()
    transport.respond("GET", f"{BASE}/wiki/api/v2/spaces?keys=ENG&limit=1", HttpResponse(401))
    with pytest.raises(ConnectorError) as excinfo:
        connector.fetch(cfg, None)
    assert excinfo.value.classification == ErrorClass.PERMANENT

    transport2 = FakeTransport()
    connector2 = ConfluenceConnector("conf-1", "confluence-acme", http=HttpClient(transport2))
    transport2.respond("GET", f"{BASE}/wiki/api/v2/spaces?keys=ENG&limit=1", HttpResponse(429, {"Retry-After": "5"}))
    with pytest.raises(ConnectorError) as excinfo:
        connector2.fetch(cfg, None)
    assert excinfo.value.classification == ErrorClass.RETRYABLE
