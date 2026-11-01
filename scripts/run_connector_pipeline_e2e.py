"""Start isolated PostgreSQL + Neo4j + the real backend + the real frontend,
seed the graph through the REAL GitHub and Confluence connectors (Connector
-> SyncCoordinator -> PostgreSQL source state/outbox -> Neo4jProjector ->
Neo4j) using deterministic fixture HTTP responses instead of the direct-
ingest HTTP endpoint, then run a real-browser Playwright suite proving the
whole pipeline end to end: authenticated search/evidence inspection,
restricted-evidence isolation, repeated-ingestion idempotency, and a source
update propagating through to the browser.
"""
from __future__ import annotations

import json
import os
import secrets
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import psycopg
from neo4j import GraphDatabase

ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "frontend"
sys.path.insert(0, str(ROOT))


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def wait_http(url: str, expected: int = 200) -> None:
    for _ in range(60):
        try:
            if httpx.get(url, timeout=2).status_code == expected:
                return
        except httpx.HTTPError:
            pass
        time.sleep(1)
    raise RuntimeError(f"Service did not become ready: {url}")


class FakeTransport:
    """Deterministic provider-shaped HTTP double, identical in spirit to the
    one used by backend/tests/test_github_connector.py and friends."""

    def __init__(self):
        self.queues: dict[tuple[str, str], list] = {}

    def respond(self, method: str, url: str, response) -> None:
        self.queues.setdefault((method, url), []).append(response)

    def __call__(self, method, url, headers, body):
        queue = self.queues.get((method, url))
        if not queue:
            raise AssertionError(f"Unexpected request in connector pipeline E2E: {method} {url}")
        return queue.pop(0) if len(queue) > 1 else queue[0]


def main() -> int:
    from backend.app.config import Settings
    from backend.app.graph.driver import Neo4jStore
    from backend.app.graph.repository import GraphRepository
    from backend.app.ingestion.confluence_connector import ConfluenceConnector
    from backend.app.ingestion.connector import ConnectorConfig, ConnectorIdentity
    from backend.app.ingestion.github_connector import GitHubConnector
    from backend.app.ingestion.http import HttpClient, HttpResponse
    from backend.app.ingestion.postgres_store import PostgresStore
    from backend.app.ingestion.sync import SyncCoordinator

    def json_response(payload, status=200, headers=None):
        return HttpResponse(status, headers or {}, json.dumps(payload).encode())

    suffix = uuid.uuid4().hex[:12]
    neo4j_container, neo4j_volume = f"gh-neo4j-cpe2e-{suffix}", f"gh-neo4j-cpe2e-data-{suffix}"
    neo4j_password = secrets.token_urlsafe(24)
    pg_container = f"gh-postgres-cpe2e-{suffix}"
    pg_password = secrets.token_urlsafe(24)
    token, restricted_token = secrets.token_urlsafe(24), secrets.token_urlsafe(24)
    tenant, workspace = f"tenant-{suffix}", f"workspace-{suffix}"
    bolt, api, vite, pg_port = free_port(), free_port(), free_port(), free_port()

    pr_title = f"Connector PR {suffix}"
    # Deliberately NOT a substring/superstring of pr_title: search is
    # substring-based, so "renamed" must share no text with "pr_title" or a
    # query for the old title would still match the new one trivially.
    updated_pr_title = f"Renamed Feature {suffix}"
    public_page_title = f"Public Runbook {suffix}"
    restricted_page_title = f"Restricted Design {suffix}"

    environment = os.environ.copy()
    environment.update(
        NEO4J_URI=f"bolt://127.0.0.1:{bolt}", NEO4J_USER="neo4j", NEO4J_PASSWORD=neo4j_password,
        NEO4J_DATABASE="neo4j",
        GRAPH_HACKS_API_PRINCIPALS_JSON=json.dumps({
            token: {"subject": "jane", "tenant_id": tenant, "workspaces": [workspace],
                    "permissions": ["READ_GRAPH", "READ_EVIDENCE"]},
            restricted_token: {"subject": "outsider", "tenant_id": tenant, "workspaces": [workspace],
                               "permissions": ["READ_GRAPH", "READ_EVIDENCE"]},
        }),
        VITE_PROXY_TARGET=f"http://127.0.0.1:{api}", VITE_DEV_PORT=str(vite),
        GH_E2E_BASE_URL=f"http://127.0.0.1:{vite}",
        CPE2E_TOKEN=token, CPE2E_RESTRICTED_TOKEN=restricted_token, CPE2E_WORKSPACE=workspace,
        CPE2E_PR_TITLE=pr_title, CPE2E_UPDATED_PR_TITLE=updated_pr_title,
        CPE2E_PUBLIC_PAGE_TITLE=public_page_title, CPE2E_RESTRICTED_PAGE_TITLE=restricted_page_title,
        CPE2E_GH_TOKEN="ghp_fake", CPE2E_CONF_EMAIL="bot@acme.com", CPE2E_CONF_TOKEN="token",
    )
    os.environ.update(CPE2E_GH_TOKEN="ghp_fake", CPE2E_CONF_EMAIL="bot@acme.com", CPE2E_CONF_TOKEN="token")

    processes: list[subprocess.Popen] = []
    neo4j_store = None
    backend_log = None
    frontend_log = None
    with tempfile.TemporaryDirectory(prefix="graphhacks-cpe2e-") as temporary:
        temp = Path(temporary)
        try:
            subprocess.run(["docker", "run", "-d", "--name", neo4j_container,
                            "-p", f"127.0.0.1:{bolt}:7687",
                            "-e", f"NEO4J_AUTH=neo4j/{neo4j_password}",
                            "-v", f"{neo4j_volume}:/data", "neo4j:5.26.32-community"],
                           check=True, stdout=subprocess.DEVNULL)
            driver = GraphDatabase.driver(environment["NEO4J_URI"], auth=("neo4j", neo4j_password))
            for _ in range(60):
                try:
                    driver.verify_connectivity()
                    break
                except Exception:
                    time.sleep(1)
            else:
                raise RuntimeError("Isolated Neo4j did not become ready")
            driver.close()

            subprocess.run(["docker", "run", "-d", "--name", pg_container,
                            "-p", f"127.0.0.1:{pg_port}:5432",
                            "-e", f"POSTGRES_PASSWORD={pg_password}",
                            "postgres:16.10-alpine"], check=True, stdout=subprocess.DEVNULL)
            pg_dsn = f"postgresql://postgres:{pg_password}@127.0.0.1:{pg_port}/postgres"
            for _ in range(60):
                try:
                    with psycopg.connect(pg_dsn, connect_timeout=2):
                        break
                except Exception:
                    time.sleep(1)
            else:
                raise RuntimeError("Isolated PostgreSQL did not become ready")

            backend_log = (temp / "backend.log").open("w")
            frontend_log = (temp / "frontend.log").open("w")
            processes.append(subprocess.Popen(
                [sys.executable, "-m", "uvicorn", "backend.app.main:app",
                 "--host", "127.0.0.1", "--port", str(api)],
                cwd=ROOT, env=environment, stdout=backend_log, stderr=subprocess.STDOUT))
            node = os.getenv("GRAPH_HACKS_NODE") or "node"
            processes.append(subprocess.Popen(
                [node, str(FRONTEND / "node_modules" / "vite" / "bin" / "vite.js"),
                 "--host", "127.0.0.1", "--port", str(vite)],
                cwd=FRONTEND, env=environment, stdout=frontend_log, stderr=subprocess.STDOUT))
            wait_http(f"http://127.0.0.1:{api}/health")
            wait_http(f"http://127.0.0.1:{vite}/")

            # --- Seed through the REAL connectors; never the direct-ingest endpoint. ---
            neo4j_store = Neo4jStore(Settings(neo4j_uri=environment["NEO4J_URI"], neo4j_user="neo4j",
                                              neo4j_password=neo4j_password))
            neo4j_store.start()
            pg_store = PostgresStore(pg_dsn)
            pg_store.start()
            graph = GraphRepository(neo4j_store, tenant, workspace, "connector-e2e", allow_all=True)
            coordinator = SyncCoordinator(pg_store, graph)

            gh_api = "https://api.github.com/repos/acme/widgets"
            gh_transport = FakeTransport()
            github_cfg = ConnectorConfig(
                tenant_id=tenant, workspace_id=workspace,
                identity=ConnectorIdentity(connector_id="gh-cpe2e", source_id="github-e2e", kind="github"),
                settings={"repositories": ["acme/widgets"], "per_page": 25, "token_env": "CPE2E_GH_TOKEN"})

            # Static, content-invariant fixtures: registered once and safely
            # reused indefinitely (FakeTransport repeats the last queued
            # response once its queue drains to exactly one entry).
            gh_transport.respond("GET", gh_api, json_response({"node_id": "R_CPE2E", "full_name": "acme/widgets"}))
            gh_transport.respond("GET", f"{gh_api}/contents/CODEOWNERS", HttpResponse(404))
            gh_transport.respond("GET", f"{gh_api}/contents/.github/CODEOWNERS", HttpResponse(404))
            gh_transport.respond("GET", f"{gh_api}/contents/docs/CODEOWNERS", HttpResponse(404))

            def queue_github_pull_round(title: str, updated_at: str) -> None:
                # Per-round, content-varying fixtures: ALL rounds are queued
                # up front below, before the first coordinator.run() call —
                # interleaving registration with calls here would make
                # FakeTransport serve a stale round's response one call late.
                # ``updated_at`` is passed in (not captured fresh here) so a
                # byte-identical repeat round really is byte-identical —
                # content fingerprinting hashes this timestamp too.
                gh_transport.respond(
                    "GET", f"{gh_api}/pulls?state=all&per_page=25&page=1&sort=updated&direction=asc",
                    json_response([{"node_id": "PR_CPE2E", "number": 1, "title": title, "html_url": "https://x/1",
                                   "updated_at": updated_at, "user": {"id": 1, "login": "alice"}}]))
                gh_transport.respond("GET", f"{gh_api}/pulls/1/reviews?per_page=100", json_response([]))
                gh_transport.respond("GET", f"{gh_api}/pulls/1/files?per_page=100",
                                     json_response([{"filename": "src/app.py"}]))
                gh_transport.respond(
                    "GET", f"{gh_api}/issues?state=all&filter=all&per_page=25&page=1&sort=updated&direction=asc",
                    json_response([]))

            github_connector = GitHubConnector("gh-cpe2e", "github-e2e", http=HttpClient(gh_transport))

            created_at = datetime.now(timezone.utc).isoformat()
            updated_at = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
            queue_github_pull_round(pr_title, created_at)          # round 1: first ingest
            queue_github_pull_round(pr_title, created_at)          # round 2: byte-identical repeat
            queue_github_pull_round(updated_pr_title, updated_at)  # round 3: a real source update

            first = coordinator.run(github_connector, github_cfg)
            assert first["records_changed"] == 1, f"expected 1 new record, got {first}"

            # Repeated ingestion of byte-identical content must create no new facts.
            repeat = coordinator.run(github_connector, github_cfg)
            assert repeat["records_changed"] == 0, f"repeated sync must be a no-op, got {repeat}"
            revision_after_repeat = graph.graph_revision()

            # A source update (changed title) must propagate as a real revision change.
            updated = coordinator.run(github_connector, github_cfg)
            assert updated["records_changed"] == 1, f"expected the title change to register, got {updated}"
            assert graph.graph_revision() > revision_after_repeat, "graph revision must advance on a real update"

            conf_base = "https://acme.atlassian.net"
            conf_transport = FakeTransport()
            confluence_cfg = ConnectorConfig(
                tenant_id=tenant, workspace_id=workspace,
                identity=ConnectorIdentity(connector_id="conf-cpe2e", source_id="confluence-e2e", kind="confluence"),
                settings={"base_url": conf_base, "spaces": ["ENG"], "limit": 10,
                         "email_env": "CPE2E_CONF_EMAIL", "token_env": "CPE2E_CONF_TOKEN",
                         "identity_map": {"acc-jane": "jane"}})

            def page_item(page_id: str, title: str) -> dict:
                return {"id": page_id, "title": title, "parentId": None,
                       "version": {"number": 1, "createdAt": datetime.now(timezone.utc).isoformat(),
                                  "authorId": "acc-jane"},
                       "body": {"storage": {"value": ""}},
                       "_links": {"webui": f"/spaces/ENG/pages/{page_id}"}}

            conf_transport.respond("GET", f"{conf_base}/wiki/api/v2/spaces?keys=ENG&limit=1",
                                   json_response({"results": [{"id": "111", "key": "ENG"}]}))
            conf_transport.respond(
                "GET", f"{conf_base}/wiki/api/v2/pages?space-id=111&status=current&body-format=storage&limit=10",
                json_response({"results": [page_item("1", public_page_title),
                                           page_item("2", restricted_page_title)]}))
            conf_transport.respond("GET", f"{conf_base}/wiki/rest/api/content/1/restriction/byOperation/read",
                                   json_response({"user": {"results": []}, "group": {"results": []}}))
            conf_transport.respond("GET", f"{conf_base}/wiki/rest/api/content/2/restriction/byOperation/read",
                                   json_response({"user": {"results": [{"accountId": "acc-jane"}]},
                                                 "group": {"results": []}}))
            confluence_connector = ConfluenceConnector("conf-cpe2e", "confluence-e2e", http=HttpClient(conf_transport))
            conf_result = coordinator.run(confluence_connector, confluence_cfg)
            assert conf_result["records_changed"] == 2, f"expected both pages to be new, got {conf_result}"

            neo4j_store.close()
            neo4j_store = None
            print("Seeded via real GitHub + Confluence connectors through SyncCoordinator/PostgreSQL/"
                 "Neo4jProjector — the direct-ingest HTTP endpoint was never used.")

            completed = subprocess.run(
                [node, str(FRONTEND / "node_modules" / "playwright" / "cli.js"),
                 "test", "e2e/connector_pipeline.spec.ts"], cwd=FRONTEND, env=environment)
            return completed.returncode
        finally:
            if neo4j_store is not None:
                neo4j_store.close()
            for process in reversed(processes):
                process.terminate()
                try:
                    process.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    process.kill()
            if backend_log is not None:
                backend_log.close()
            if frontend_log is not None:
                frontend_log.close()
            subprocess.run(["docker", "rm", "-f", neo4j_container], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            subprocess.run(["docker", "volume", "rm", neo4j_volume], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            subprocess.run(["docker", "rm", "-f", pg_container], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


if __name__ == "__main__":
    raise SystemExit(main())
