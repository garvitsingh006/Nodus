# Agent instructions for this repository

This file directs agent work in NODUS, a single-company, objective-driven
relationship intelligence system. A user provides an investigation objective
and data access; NODUS discovers relevant relationships; agents investigate
those relationships for causes, dependencies, risks, and weak links; useful
discovered relationships are retained for future investigations. A planned
demo adds project-scoped developer/intern permissions, an admin token, and
access-extension requests — these are future product requirements, not
implemented features.

## Existing foundation (reusable, not to be redesigned)

- The Neo4j graph driver, repository, models, and ontology.
- PostgreSQL persistence infrastructure.
- Generic ingestion and reconciliation components, including the GitHub,
  Jira, and Confluence connectors.
- The agent investigator and execution infrastructure, and the Gemini/LLM
  gateway.
- Graph, evidence, ontology, and source tools, and the existing skills and
  their registries.
- The frontend application structure.

Build new work on top of these rather than replacing them. There is no
numbered implementation roadmap, milestone ID list, or stage-completion
checklist to maintain — track status in commit messages and code, not in
new planning documents.

## Repository conventions (current state, not aspiration)

- Single FastAPI app in `backend/app/main.py` — no `api/` router package yet;
  new HTTP routes go there until a split is explicitly planned.
- Typed cross-component contracts live in `backend/app/contracts.py`
  (`Identity`, `Checkpoint`, `Handoff`, `OutboxEvent`, `ProjectionWatermark`,
  `BaselineMetrics`, `StorageAdoptionCriteria`, and the `*Port` Protocols) and
  `backend/app/security.py` (`Permission`, `Principal`, `AuthorizationContext`,
  `ToolPolicy`). These are frozen Pydantic `Record` models with
  `model_validator`s enforcing invariants (fencing, expiry, scope-narrowing) —
  follow this pattern for new contracts rather than inventing a new style.
- Tests live under `backend/tests/`. `test_foundation_contracts.py` is pure
  unit tests (no I/O). `test_neo4j_graph.py` is marked `integration` in
  `pytest.ini` and spins up its own isolated Neo4j Docker container unless
  `GRAPH_HACKS_TEST_NEO4J_URI`/`GRAPH_HACKS_TEST_NEO4J_PASSWORD` are set.
- Dependencies: root `requirements.txt` only (no `backend/requirements.txt`,
  no `pyproject.toml`). Frontend deps are in `frontend/package.json`.
- `.env` is local-only (gitignored) and must define `NEO4J_PASSWORD` /
  `POSTGRES_PASSWORD` or `docker compose config` fails to parse — see
  `.env.example` for the full variable set.
- The frontend persists the bearer token in browser `localStorage`
  (`frontend/src/App.tsx`) — acceptable for the current dev explorer, but a
  security consideration to address before any production exposure.

## What not to do

- Do not reintroduce FalkorDB, the previous demo companies/sources, or the
  old numbered implementation files — they have been removed intentionally.
- Do not create audits, audit artifacts, progress logs, or retrospective
  reports as part of normal work. Describe what changed in the commit or PR,
  not in a new document.
