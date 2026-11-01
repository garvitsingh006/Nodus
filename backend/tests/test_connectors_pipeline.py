"""Real end-to-end verification: GitHubConnector, JiraConnector and
ConfluenceConnector, three independent production connector implementations,
driven through the same SyncCoordinator into real PostgreSQL (state/outbox)
and real Neo4j (graph). No mocked database for these transactional/graph
claims.
"""
from __future__ import annotations

import json
import os
import uuid

import pytest

from backend.app.graph.models import RelationType
from backend.app.ingestion.confluence_connector import ConfluenceConnector
from backend.app.ingestion.connector import ConnectorConfig, ConnectorError, ConnectorIdentity
from backend.app.ingestion.github_connector import GitHubConnector
from backend.app.ingestion.http import HttpClient, HttpResponse
from backend.app.ingestion.jira_connector import JiraConnector
from backend.app.ingestion.sync import SyncCoordinator
from backend.tests.test_confluence_connector import no_restriction, page_item, restriction
from backend.tests.test_github_connector import FakeTransport, json_response
from backend.tests.test_ingestion_sync import graph, isolated_neo4j, isolated_postgres, store  # noqa: F401
from backend.tests.test_jira_connector import SEARCH_URL, issue

CONF_BASE = "https://acme.atlassian.net"


def _name() -> str:
    return uuid.uuid4().hex


def test_github_and_jira_both_ingest_through_the_same_coordinator(store, graph):
    tenant, workspace = graph.tenant_id, graph.workspace_id
    coordinator = SyncCoordinator(store, graph)

    gh_transport = FakeTransport()
    github = GitHubConnector("gh-pipeline", "github-acme", http=HttpClient(gh_transport))
    gh_cfg = ConnectorConfig(tenant_id=tenant, workspace_id=workspace,
                             identity=ConnectorIdentity(connector_id="gh-pipeline", source_id="github-acme",
                                                        kind="github"),
                             settings={"repositories": ["acme/widgets"], "per_page": 25, "allow_public": True})
    api = "https://api.github.com/repos/acme/widgets"
    gh_transport.respond("GET", api, json_response({"node_id": "R_1", "full_name": "acme/widgets"}))
    gh_transport.respond("GET", f"{api}/contents/CODEOWNERS", json_response({}, status=404))
    gh_transport.respond("GET", f"{api}/contents/.github/CODEOWNERS", json_response({}, status=404))
    gh_transport.respond("GET", f"{api}/contents/docs/CODEOWNERS", json_response({}, status=404))
    gh_transport.respond("GET", f"{api}/pulls?state=all&per_page=25&page=1&sort=updated&direction=asc",
                         json_response([{"node_id": "PR_1", "number": 1, "title": "Add feature",
                                        "html_url": "https://x/1", "updated_at": "2024-01-02T00:00:00Z",
                                        "user": {"id": 1, "login": "alice"}}]))
    gh_transport.respond("GET", f"{api}/pulls/1/reviews?per_page=100", json_response([]))
    gh_transport.respond("GET", f"{api}/pulls/1/files?per_page=100", json_response([{"filename": "src/app.py"}]))
    gh_transport.respond("GET", f"{api}/issues?state=all&filter=all&per_page=25&page=1&sort=updated&direction=asc",
                         json_response([]))

    gh_result = coordinator.run(github, gh_cfg)
    assert gh_result["status"] == "SUCCEEDED" and gh_result["records_changed"] == 1  # one pull request record
    pr_entity = graph.get_entity("github:pr:PR_1")
    assert pr_entity is not None and pr_entity.type.value == "PULL_REQUEST"
    neighborhood = graph.neighborhood("github:pr:PR_1")
    assert any(rel.type == RelationType.AUTHORED for rel in neighborhood.relationships)
    assert any(rel.type == RelationType.MODIFIES and rel.target_id.endswith("src/app.py")
              for rel in neighborhood.relationships)

    jira_transport = FakeTransport()
    jira = JiraConnector("jira-pipeline", "jira-acme", http=HttpClient(jira_transport),)
    jira_cfg = ConnectorConfig(tenant_id=tenant, workspace_id=workspace,
                               identity=ConnectorIdentity(connector_id="jira-pipeline", source_id="jira-acme",
                                                          kind="jira"),
                               settings={"base_url": "https://acme.atlassian.net", "projects": ["ENG"],
                                        "max_results": 50, "email_env": "GH004B1_JIRA_EMAIL",
                                        "token_env": "GH004B1_JIRA_TOKEN"})
    import os
    os.environ["GH004B1_JIRA_EMAIL"] = "bot@acme.com"
    os.environ["GH004B1_JIRA_TOKEN"] = "token"
    jira_transport.respond("POST", SEARCH_URL, json_response({
        "issues": [issue("ENG-1", "1001", assignee={"accountId": "acc-1", "displayName": "Dana"})],
        "isLast": True}))

    jira_result = coordinator.run(jira, jira_cfg)
    assert jira_result["status"] == "SUCCEEDED" and jira_result["records_changed"] == 1
    issue_entity = graph.get_entity("jira:issue:1001")
    assert issue_entity is not None and issue_entity.type.value == "TASK"

    # Both connectors' facts coexist in one tenant/workspace graph, ingested
    # through the exact same coordinator/outbox pipeline, with zero
    # GitHub/Jira-specific logic anywhere in GraphRepository or ontology.
    assert graph.get_entity("github:pr:PR_1") is not None
    assert graph.get_entity("jira:issue:1001") is not None


def test_repeated_sync_is_idempotent_and_tenant_isolated(store, graph):
    tenant, workspace = graph.tenant_id, graph.workspace_id
    other_tenant, other_workspace = _name(), _name()
    coordinator = SyncCoordinator(store, graph)

    transport = FakeTransport()
    connector = GitHubConnector("gh-idem", "github-acme", http=HttpClient(transport))
    cfg = ConnectorConfig(tenant_id=tenant, workspace_id=workspace,
                          identity=ConnectorIdentity(connector_id="gh-idem", source_id="github-acme", kind="github"),
                          settings={"repositories": ["acme/widgets"], "per_page": 25, "allow_public": True})
    api = "https://api.github.com/repos/acme/widgets"
    transport.respond("GET", api, json_response({"node_id": "R_2", "full_name": "acme/widgets"}))
    transport.respond("GET", f"{api}/contents/CODEOWNERS", json_response({}, status=404))
    transport.respond("GET", f"{api}/contents/.github/CODEOWNERS", json_response({}, status=404))
    transport.respond("GET", f"{api}/contents/docs/CODEOWNERS", json_response({}, status=404))
    transport.respond("GET", f"{api}/pulls?state=all&per_page=25&page=1&sort=updated&direction=asc",
                      json_response([{"node_id": "PR_9", "number": 9, "title": "Stable PR",
                                     "html_url": "https://x/9", "updated_at": "2024-03-01T00:00:00Z",
                                     "user": {"id": 1, "login": "alice"}}]))
    transport.respond("GET", f"{api}/pulls/9/reviews?per_page=100", json_response([]))
    transport.respond("GET", f"{api}/pulls/9/files?per_page=100", json_response([]))
    transport.respond("GET", f"{api}/issues?state=all&filter=all&per_page=25&page=1&sort=updated&direction=asc",
                      json_response([]))

    first = coordinator.run(connector, cfg)
    assert first["records_changed"] == 1
    outbox_count_first = store.read(
        "SELECT count(*) AS n FROM outbox_events WHERE connector_id='gh-idem'")[0]["n"]

    second = coordinator.run(connector, cfg)
    assert second["records_changed"] == 0  # identical content, no new facts, no duplicate outbox rows
    outbox_count_second = store.read(
        "SELECT count(*) AS n FROM outbox_events WHERE connector_id='gh-idem'")[0]["n"]
    assert outbox_count_second == outbox_count_first

    # A second, unrelated tenant/workspace sharing nothing must never see this data.
    isolated_repo = graph.__class__(graph.store, other_tenant, other_workspace, "sync-coordinator", allow_all=True)
    assert isolated_repo.get_entity("github:pr:PR_9") is None
    assert graph.get_entity("github:pr:PR_9") is not None  # same canonical id IS visible in the right tenant


def test_confluence_ingests_with_acl_enforcement_and_interrupted_pagination_recovery(store, graph):
    tenant, workspace = graph.tenant_id, graph.workspace_id
    coordinator = SyncCoordinator(store, graph)
    os.environ["GH004B2_CONF_EMAIL"] = "bot@acme.com"
    os.environ["GH004B2_CONF_TOKEN"] = "token"
    cfg = ConnectorConfig(
        tenant_id=tenant, workspace_id=workspace,
        identity=ConnectorIdentity(connector_id="conf-pipeline", source_id="confluence-acme", kind="confluence"),
        settings={"base_url": CONF_BASE, "spaces": ["ENG"], "limit": 2,
                 "email_env": "GH004B2_CONF_EMAIL", "token_env": "GH004B2_CONF_TOKEN",
                 "identity_map": {"acc-restricted": "alice-nodus"}})

    # Page 1: a public page and a restricted one (mapped identity).
    transport = FakeTransport()
    connector = ConfluenceConnector("conf-pipeline", "confluence-acme", http=HttpClient(transport))
    transport.respond("GET", f"{CONF_BASE}/wiki/api/v2/spaces?keys=ENG&limit=1",
                      json_response({"results": [{"id": "111", "key": "ENG"}]}))
    transport.respond("GET", f"{CONF_BASE}/wiki/api/v2/pages?space-id=111&status=current&body-format=storage&limit=2",
                      json_response({"results": [page_item("1", "Public Runbook"),
                                                 page_item("2", "Restricted Design")],
                                    "_links": {"next": "/wiki/api/v2/pages?cursor=p2&limit=2"}}))
    transport.respond("GET", f"{CONF_BASE}/wiki/rest/api/content/1/restriction/byOperation/read", no_restriction())
    transport.respond("GET", f"{CONF_BASE}/wiki/rest/api/content/2/restriction/byOperation/read",
                      restriction(users=["acc-restricted"]))
    # Page 2 fetch fails (simulated outage) before any checkpoint for it is saved;
    # the retry's success response is queued now too (interleaving queue
    # registration with calls on the same URL breaks FakeTransport's ordering).
    transport.respond("GET", f"{CONF_BASE}/wiki/api/v2/pages?cursor=p2&limit=2", HttpResponse(503))
    transport.respond("GET", f"{CONF_BASE}/wiki/api/v2/pages?cursor=p2&limit=2",
                      json_response({"results": [page_item("3", "Second Page")]}))
    transport.respond("GET", f"{CONF_BASE}/wiki/rest/api/content/3/restriction/byOperation/read", no_restriction())

    with pytest.raises(ConnectorError):
        coordinator.run(connector, cfg)
    assert graph.get_entity("confluence:page:1") is not None  # page 1 already committed before the failure
    assert graph.get_entity("confluence:page:3") is None  # page 2's content never reached

    # Retry: only the failed page is refetched (the checkpoint resumed past page 1).
    retry = coordinator.run(connector, cfg)
    assert retry["status"] == "SUCCEEDED" and retry["records_seen"] == 1  # page 1 never refetched
    assert graph.get_entity("confluence:page:3") is not None

    # ACL enforcement: a non-admin reader sees the public and mapped-restricted
    # pages only according to acl_subjects; the unmapped-restriction page was
    # never created in this scenario, so verify directly against a reader
    # lacking the mapped identity and one holding it.
    outsider = graph.__class__(graph.store, tenant, workspace, "outsider", allow_all=False, can_evidence=True)
    insider = graph.__class__(graph.store, tenant, workspace, "alice-nodus", allow_all=False, can_evidence=True)
    assert outsider.get_entity("confluence:page:2") is None  # restricted to alice-nodus only
    assert insider.get_entity("confluence:page:2") is not None
    assert outsider.get_entity("confluence:page:1") is not None  # unrestricted page stays visible to everyone
