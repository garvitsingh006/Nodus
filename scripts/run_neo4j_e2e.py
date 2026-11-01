"""Start isolated Neo4j, backend and Vite, then run real-browser Neo4j E2E tests."""

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
from pathlib import Path

import httpx
from neo4j import GraphDatabase


ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "frontend"


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


def main() -> int:
    suffix = uuid.uuid4().hex[:12]
    container = f"gh-neo4j-e2e-{suffix}"
    volume = f"gh-neo4j-e2e-data-{suffix}"
    password = secrets.token_urlsafe(24)
    token = secrets.token_urlsafe(24)
    workspace_a, workspace_b = f"workspace-{suffix}-a", f"workspace-{suffix}-b"
    employee = f"person-{suffix}"
    employee_name = f"Employee {suffix}"
    bolt, api, vite = free_port(), free_port(), free_port()
    environment = os.environ.copy()
    environment.update(
        NEO4J_URI=f"bolt://127.0.0.1:{bolt}", NEO4J_USER="neo4j",
        NEO4J_PASSWORD=password, NEO4J_DATABASE="neo4j",
        GRAPH_HACKS_API_TOKEN=token,
        GRAPH_HACKS_API_PRINCIPALS_JSON=json.dumps({
            token: {"subject": "e2e-user", "tenant_id": f"tenant-{suffix}",
                    "workspaces": [workspace_a, workspace_b], "source_ids": ["local"],
                    "permissions": ["READ_GRAPH", "READ_EVIDENCE", "MANAGE_INGESTION"]}
        }),
        VITE_PROXY_TARGET=f"http://127.0.0.1:{api}",
        VITE_DEV_PORT=str(vite),
        GH_E2E_TOKEN=token,
        GH_E2E_WORKSPACE_A=workspace_a,
        GH_E2E_WORKSPACE_B=workspace_b,
        GH_E2E_EMPLOYEE_NAME=employee_name,
        GH_E2E_EMPLOYEE_ID=f"local::{employee}",
        GH_E2E_BASE_URL=f"http://127.0.0.1:{vite}",
    )
    processes: list[subprocess.Popen] = []
    backend_log = None
    frontend_log = None
    with tempfile.TemporaryDirectory(prefix="graphhacks-e2e-") as temporary:
        temp = Path(temporary)
        try:
            subprocess.run(["docker", "run", "-d", "--name", container,
                            "-p", f"127.0.0.1:{bolt}:7687",
                            "-e", f"NEO4J_AUTH=neo4j/{password}",
                            "-v", f"{volume}:/data", "neo4j:5.26.32-community"],
                           check=True, stdout=subprocess.DEVNULL)
            driver = GraphDatabase.driver(environment["NEO4J_URI"], auth=("neo4j", password))
            for _ in range(60):
                try:
                    driver.verify_connectivity()
                    break
                except Exception:
                    time.sleep(1)
            else:
                raise RuntimeError("Isolated Neo4j did not become ready")
            driver.close()
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
            document = {"source_id": "local", "records": [{
                "id": f"record-{suffix}", "title": "Local source document",
                "entities": [
                    {"id": employee, "kind": "PERSON", "name": employee_name},
                    {"id": f"team-{suffix}", "kind": "TEAM", "name": f"Team {suffix}"},
                    {"id": f"repo-{suffix}", "kind": "REPOSITORY", "name": f"Repository {suffix}"},
                    {"id": f"pr-{suffix}", "kind": "PULL_REQUEST", "name": f"PR {suffix}"},
                    {"id": f"file-{suffix}", "kind": "FILE", "name": f"File {suffix}"},
                    {"id": f"task-{suffix}", "kind": "TASK", "name": f"Task {suffix}"},
                    {"id": f"doc-{suffix}", "kind": "DOCUMENT", "name": f"Document {suffix}"}
                ],
                "links": [
                    {"from": employee, "to": f"team-{suffix}", "kind": "BELONGS_TO"},
                    {"from": employee, "to": f"pr-{suffix}", "kind": "AUTHORED"},
                    {"from": f"pr-{suffix}", "to": f"repo-{suffix}", "kind": "MODIFIES"},
                    {"from": f"pr-{suffix}", "to": f"file-{suffix}", "kind": "MODIFIES"},
                    {"from": f"task-{suffix}", "to": employee, "kind": "ASSIGNED_TO"},
                    {"from": f"doc-{suffix}", "to": f"task-{suffix}", "kind": "REFERENCES"}
                ]
            }]}
            document_path = temp / "source.json"
            document_path.write_text(json.dumps(document), encoding="utf-8")
            subprocess.run([sys.executable, str(ROOT / "scripts" / "import_json.py"),
                            str(document_path), "--workspace", workspace_a,
                            "--api-url", f"http://127.0.0.1:{api}"],
                           cwd=ROOT, env=environment, check=True)
            completed = subprocess.run(
                [node, str(FRONTEND / "node_modules" / "playwright" / "cli.js"),
                 "test", "e2e/neo4j.spec.ts"], cwd=FRONTEND, env=environment)
            return completed.returncode
        finally:
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
            subprocess.run(["docker", "rm", "-f", container], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            subprocess.run(["docker", "volume", "rm", volume], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


if __name__ == "__main__":
    raise SystemExit(main())
