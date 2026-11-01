# Neo4j rebuild: architecture and end-to-end audit

**Date:** 30 October 2026  
**Scope:** Current repository implementation against `01-FalkorDB-Removal-and-Neo4j-Rebuild.md`. No application source was changed during this audit. All live records were created in disposable Neo4j containers and volumes, which were removed afterward. The existing `.env`, user data, and project volumes were not changed.

## 1. Executive Summary

**Overall status: REJECTED.** Neo4j Community starts from the pinned image, the Compose service reached `healthy`, FastAPI started with explicitly supplied temporary credentials, schema constraints were created, and source ingestion produced real entities and relationships. Data persisted through a restart of the disposable standalone container. Direct entity lookup, traversal, provenance, replay, update, record retraction, and tested server-side tenant/workspace isolation worked in the cases described below.

The complete source-to-graph-to-UI workflow **does not work**: `GET /api/v1/workspaces/{workspace}/search` returns HTTP 500 for both empty and populated graphs. Chromium displayed a JSON parse error when the user searched. A delayed real source-list response from workspace B also appeared after switching to an empty workspace. A graph-only principal received source record identifiers in relationship results despite lacking `READ_EVIDENCE`. Retraction left entity properties from a retracted source claim visible. These are observed defects, not inferred from passing builds.

The backend can start with environment variables supplied explicitly. The current local `.env` did not supply `NEO4J_PASSWORD` to Compose, so the default local command did not start without a temporary value; no real credential was altered. The backend reads process environment directly and does not load a `.env` file itself. The implementation is **not ready for the next development phase** until the search, workspace-response, evidence-permission, and retraction defects are fixed and verified.

## 2. Implementation Coverage

| Task | Status | Evidence |
|---|---|---|
| M01 Clean Neo4j target | PARTIAL | Active `backend/app/main.py` uses Neo4j, but retained graph/API modules still import the old client (for example `backend/app/graph/client.py:7`, `backend/app/api_v1/security.py:12`). |
| M02 Local deployment replacement | PASS | `docker-compose.yml:2-23` declares only Neo4j; isolated Compose `up -d` started and became healthy. |
| M03 Remove old clients/packages | PARTIAL | `requirements.txt` declares `neo4j` and no old client; old client code and imports remain in source. |
| M04 Delete legacy runtime | FAIL | Old graph queries, services, API facades, and ingestion modules remain, although the new `main.py` does not import them. |
| M05 Remove demo/seeding assumptions | PARTIAL | Fresh test graph was empty and no new startup seeding occurred. Retained frontend and backend legacy files remain; no full removal was established. |
| M06 Retire old API/frontend dependencies | PARTIAL | The active UI entry point uses the new API, while old frontend pages/API clients remain in the tree and the declared E2E script targets deleted verification files. |
| M07 Remove unused packages/configuration/artifacts | PARTIAL | Active manifest and Compose were updated; obsolete source/test artifacts remain. |
| M08 Pin Neo4j Community | PASS | `docker-compose.yml:3-23`: `neo4j:5.26.32-community`, named volume, environment auth, health check. Disposable Compose service reached `healthy`. |
| M09 Driver and lifecycle | PARTIAL | `backend/app/graph/driver.py:15-41` uses managed sessions and startup/shutdown; live startup and 503 outage behavior passed. No per-query execution timeout was configured. |
| M10 Schema bootstrap | PASS | Two uniqueness constraints and three indexes use `IF NOT EXISTS` (`driver.py:7-12`); repeated backend starts succeeded and `SHOW CONSTRAINTS` returned both named constraints. |
| M11 Graph persistence | PARTIAL | Live typed ingestion, lookup, traversal, provenance, and replay worked. Search failed; retraction did not restore surviving source properties. |
| M12 Tenant/source authorization | PARTIAL | Tested API tenant/workspace and restricted-entity checks passed; a graph-only principal received relationship source record IDs, and the UI rendered stale workspace data. |
| M13 Deterministic ingestion | PARTIAL | Normalized records replayed without duplicate nodes/edges, but a retracted claim's entity properties remained visible. There is no active source adapter → normalization → identity-resolution workflow in the new runtime. |
| M14 Graph query endpoints | FAIL | Search endpoint returns 500. Other tested read endpoints returned typed data with bounds. |
| M15 Real-graph UI | FAIL | Browser loaded, authenticated, and switched workspaces; search failed, so the normal path to entity details and evidence was unavailable. |
| M16 Isolated fixtures in repository tests | NOT IMPLEMENTED | No new Neo4j fixture/test suite was found. This audit used disposable external test scripts and databases only. |
| M17 Source → graph → API → UI | FAIL | Source → Neo4j → several API reads passed; API search and browser search failed. |
| M18 Isolation/access verification | PARTIAL | Live direct lookup, evidence, workspace, tenant, and write-permission checks passed; stale UI response and provenance permission checks failed. |
| M19 Bounded operations | PARTIAL | High-degree response limits and truncation passed; `MATCH p=(start)-[*1..4]-(finish)` expands before `LIMIT` and has no query execution timeout. |
| M20 Final removal/clean start | FAIL | Retained old client imports and failing old test/E2E commands remain; clean application startup requires explicit environment setup. |

## 3. Test Execution Results

**Counts:** The final isolated live procedures contained **44 distinct assertions: 38 passed, 6 failed**. The existing frontend Vitest suite reported **45 passing tests in six files**, but these are largely old-component tests and do not prove the new explorer. Across those assertion sets: **89 executed, 83 passed, 6 failed**. Separately, two repository test commands failed before completing their suites: backend pytest had three collection errors after the temporary Neo4j driver was supplied, and `npm run test:e2e` referenced a missing script. Typecheck and build passed. No live test is counted as passed because of a mock response.

The live API procedure ran a temporary Uvicorn process at `127.0.0.1:61234` against a disposable Neo4j volume. It used real HTTP requests, the official Python driver for database counts, and temporary bearer principals. The stress procedure used a separate temporary backend process at `61235`. The browser procedure used Vite at `5174`, the temporary backend, Chromium through Playwright, and real backend responses; its delay interception changed timing only. The outage procedure used the isolated Compose service and backend process at `61236`.

| Procedure / test | Expected | Observed | Result |
|---|---|---|---|
| API: backend start | Uvicorn and schema initialize | Process remained running | PASS |
| API: health | HTTP 200 when Neo4j ready | 200 | PASS |
| API: unauthenticated workspace list | 401 | 401 | PASS |
| API: authorized workspace list | Three configured workspaces | Three returned | PASS |
| API: empty search | 200 with empty items | 500; driver argument error | **FAIL** |
| API: unauthorized workspace | 404 | 404 | PASS |
| API: seven-entity record ingestion | 200; seven entities/seven edges | 200, counts reported seven/seven | PASS |
| API: populated search | One employee result | 500; no JSON response | **FAIL** |
| API: direct entity lookup | Employee 101 | 200, expected ID/name | PASS |
| API: neighborhood | Three connected edges | 200, three edges | PASS |
| API: evidence | Source record reference | 200, record-1 | PASS |
| API: reverse dependency | One incoming `DEPENDS_ON` | 200, one edge | PASS |
| API: bounded path | At least one path | 200, path returned | PASS |
| API: unchanged replay | One logical record | 200; direct database count stayed seven nodes/seven entity edges | PASS |
| API: source update | New employee name | 200, revised name returned | PASS |
| API: removed edge history | Inactive historical edge | 200, inactive edge returned | PASS |
| API: overlapping IDs in workspaces | Independent records | Both workspaces returned their own names | PASS |
| API: overlapping IDs in tenants | Independent records | Tenant B returned its own name | PASS |
| API: restricted entity to other subject | 404 | 404 | PASS |
| API: read-only principal write | 403 | 403 | PASS |
| API: invalid relationship type | 422 | 422 | PASS |
| API: missing entity | 404 | 404 | PASS |
| API: record retraction | Entity unavailable after sole source removed | DELETE 200; lookup 404 | PASS |
| API: retraction scope | Other workspace record survives | 200 | PASS |
| Stress: 106-node/106-edge ingestion | Accepted within input bounds | 200 | PASS |
| Stress: neighborhood limit 20 | 20 edges and `truncated=true` | 200, 20, true | PASS |
| Stress: reverse dependency limit 20 | 20 edges and truncation | 200, 20, true | PASS |
| Stress: cycle path | Bounded response | 200, two paths | PASS |
| Stress: depth 8 | 422 | 422 | PASS |
| Stress: neighborhood limit 1000 | 422 | 422 | PASS |
| Stress: edge to absent record endpoint | 422 | 422 | PASS |
| Stress: graph-only provenance | No source record ID without evidence permission | Relationship included `record_id` | **FAIL** |
| Stress: graph-only evidence endpoint | 403 | 403 | PASS |
| Stress: retract newer of two entity claims | Surviving first name visible | Second, retracted name remained | **FAIL** |
| Browser: app load | Heading visible | Visible in Chromium | PASS |
| Browser: connect | Session rendered | `auditor-a` rendered | PASS |
| Browser: workspace list | Three options | Three options | PASS |
| Browser: workspace selection | Work B selected | Selected | PASS |
| Browser: search | Entity card appears | `Unexpected token 'I', "Internal S"... is not valid JSON` | **FAIL** |
| Browser: failed search honesty | No fake result | No result fabricated | PASS |
| Browser: delayed old workspace response | Empty workspace stays empty | Work B source list appeared under empty workspace | **FAIL** |
| Outage: backend on isolated Compose service | Health 200 | 200 | PASS |
| Outage: health after audit DB stop | 503 | 503, `Neo4j unavailable` | PASS |
| Outage: graph read after audit DB stop | 503 | 503, `Graph database unavailable` | PASS |

Additional executed gates and observations:

| Command or procedure | Expected | Actual | Result |
|---|---|---|---|
| `docker compose config --quiet` with current environment | Parse configured service | Failed because `NEO4J_PASSWORD` had no value; no credential was changed | BLOCKED by local configuration |
| Same command with a temporary process-only password | Parse | Exit 0 | PASS |
| Isolated Compose `up -d` with override container name/project/ports | Start Neo4j | Started and reached `running healthy` | PASS |
| Disposable standalone `docker restart` then Cypher count | Persist data | Ten audit entities remained; mapped random host port changed | PASS |
| `SHOW CONSTRAINTS` | Named uniqueness constraints | `entity_scope_key`, `source_scope_key` | PASS |
| `python -c "Settings.from_env()"` without exported password | Configuration should be available for documented direct startup | Pydantic error: `neo4j_password String should have at least 1 character` | FAIL for unconfigured direct startup |
| `npm run typecheck` | Exit 0 | Exit 0 | PASS |
| `npm run build -- --outDir <temporary directory>` | Production bundle | Exit 0, 31 modules transformed | PASS |
| `npm test` | Existing unit suite | 45 passed in six files; tests target retained older modules | PASS, limited relevance |
| `python -m pytest backend/tests -q --disable-warnings --maxfail=5` with temporary driver available | Suite collection | Three collection errors: missing `pglast`, missing `redis`, missing removed `work_planning_service` | FAIL at collection |
| `npm run test:e2e` | Declared E2E command | Missing `scripts/verify_phase12_ui.py` | FAIL before tests |

The first unconfigured backend suite attempt also failed on missing `neo4j`; the driver was then installed into a temporary directory and the suite rerun. A first browser harness attempt used a stale ephemeral Bolt host port after container restart; it was corrected and is not counted as an application test. The initial API harness stopped on the search 500; it was made failure-tolerant and rerun. These setup attempts were not silently counted as passes.

## 4. Architecture Findings

**Database infrastructure.** The pinned Community container, named volume, auth, Bolt endpoint, and Compose health check worked in the disposable project. Compose interpolation requires a nonempty password. The actual project service and any pre-existing project volume were intentionally not started or changed. Neo4j Community remains one database with application-scoped tenant/workspace separation.

**Backend and persistence.** `Neo4jStore` owns one driver, uses managed read/write transactions, and closes at shutdown. Two node-key uniqueness constraints and three indexes initialized idempotently. Inputs use enum-allowlisted relationship types; the only dynamic relationship token comes from `RelationType`. The bounded path depth is a validated integer. Query values otherwise use Cypher parameters. There is no endpoint for arbitrary Cypher. Search passes a Python keyword named `query` to `tx.run`, which already has a `query` parameter; this causes the observed 500. Entity properties are stored on the shared entity node and overwritten by the most recent record, so retracting a later record cannot restore earlier values.

**APIs and ingestion.** A bearer principal supplies tenant, workspace allowlist, permissions, and allowed source IDs. The live API accepted normalized records and wrote source provenance atomically. It does not invoke the retained connector/normalization/resolution modules; the active path starts with already normalized request bodies. No automatic synthetic data appeared on startup. Graph responses are typed at the Pydantic model level. The frontend uses the new routes, but search failure blocks normal navigation to details.

**Frontend and deployment.** `frontend/src/App.tsx` clears local state when workspace changes, but in-flight fetch promises remain active and can repopulate state with data from the old selection. It parses every response as JSON before checking `response.ok`; a plain-text 500 becomes a syntax error. `frontend/tsconfig.app.json:21` compiles only the new entry files, leaving retained old source outside the typecheck. The declared E2E script points to removed files. The README's direct Uvicorn command depends on exported environment variables; `backend/app/config.py:13-20` does not read `.env`.

## 5. End-to-End Workflow Results

The audit posted one normalized `audit-source/record-1` through the **actual ingestion HTTP endpoint**. It represented an employee, team, repository, pull request, code file, task, and document with seven directed typed relationships. The response was 200. Direct Neo4j counts showed seven entity nodes and seven entity-to-entity relationships after a second identical post, demonstrating stable identity and no duplicate logical facts in that replay. API entity lookup, neighborhood, reverse dependency, bounded path, evidence, and history returned expected real data. Updating the record changed the employee name and made a removed edge historical; deleting the only record made that employee unavailable. Separate workspace and tenant records with overlapping IDs remained independent.

The active API begins at **normalized input**. A live external connector → normalization → identity-resolution chain was not present in the new runtime and therefore was not verified. API search returned 500 on both an empty and a populated workspace. Chromium loaded the app, connected and selected workspaces, but searching showed a JSON parse error. Thus entity details, traversal, and evidence were **not reachable through the normal browser search workflow**; passing backend calls do not substitute for this UI failure.

## 6. Security Findings

- **Server-side scope:** Tested direct lookup, overlap, restricted entity, source write authorization, and forbidden workspace requests behaved as expected. Scope keys include tenant and workspace. No application cache was found in the new runtime, so a cross-tenant cache collision was not observed.
- **Evidence permission:** `READ_EVIDENCE` correctly protected the evidence endpoint, but `READ_GRAPH` alone exposed `source_id_ref` and `record_id` on relationship responses (`backend/app/graph/models.py:71-81`, `repository.py:23-29`). This is an observed authorization gap for source metadata.
- **Workspace presentation:** A delayed real response for work B populated the source list after the browser switched to empty work C. Both workspaces were authorized to the same test principal; this is a demonstrated workspace-context mix-up, not a demonstrated cross-tenant server leak.
- **Cypher safety:** Enum relationship tokens and validated depth are the only interpolated Cypher fragments. Record identifiers and text are parameterized. Invalid relation type and excessive depth were rejected with 422.
- **Secrets:** Production credentials were not read or printed. The repository supplies an empty example password; Compose requires an environment value. The audit used disposable credentials only. The UI stores the bearer token in `localStorage` (`frontend/src/App.tsx:10,51-52`), which should be reviewed against the intended browser threat model; this audit did not demonstrate token compromise.
- **Reliability:** Database outage returned 503 on health and graph lookup, with no fake success. Query execution has connection and retry limits but no explicit per-query timeout; path expansion can consume work before the final result limit.

## 7. Failures and Defects

### NEO-01 — HIGH — Search API returns 500

- **Component:** `backend/app/graph/repository.py:141-151`.
- **Reproduce:** Start the backend against the disposable database and call `GET /api/v1/workspaces/work-a/search?q=Employee` with an authorized bearer token, including on an empty workspace.
- **Expected:** 200 with a typed, possibly empty, paginated result.
- **Actual/evidence:** HTTP 500. Server traceback: `TypeError: TransactionBase.run() got multiple values for argument 'query'`. The browser displayed a JSON parse error during search.
- **Cause (confirmed):** The call supplies `query=query`, colliding with the driver's first `run(query, ...)` argument.
- **Correction:** Rename the Cypher parameter and supplied keyword, then verify empty/populated search and pagination through HTTP and Chromium.

### NEO-02 — HIGH — Stale workspace response appears after selection changes

- **Component:** `frontend/src/App.tsx:81-87`; similar unguarded updates in `search` and `selectEntity` at lines 56-78.
- **Reproduce:** In Chromium, delay the real work-B `/sources` response, select work B, then select empty work C before it arrives.
- **Expected:** Work C remains empty.
- **Actual/evidence:** The Sources section under work C displayed `audit-source · 1 records` from work B.
- **Cause (confirmed for sources):** The effect clears state but does not abort or scope the pending response before `setSources`.
- **Correction:** Use abort controllers or request-generation keys tied to principal, tenant, and workspace for every fetch; discard responses that no longer match active selection.

### NEO-03 — HIGH — Graph-only permission exposes source record identifiers

- **Component:** `backend/app/graph/models.py:71-81`, `backend/app/graph/repository.py:23-29,156-182`, `backend/app/main.py:149-168`.
- **Reproduce:** Give a principal `READ_GRAPH` without `READ_EVIDENCE`; request a neighborhood containing an accessible relationship.
- **Expected:** Source record identifiers are withheld unless evidence access is granted, or the permission contract explicitly authorizes them.
- **Actual/evidence:** Neighborhood 200 included `record_id` and `source_id_ref`; the same principal received 403 from `/evidence`.
- **Probable cause (inferred):** Relationship DTOs always include provenance identifiers and are returned under `READ_GRAPH`.
- **Correction:** Define the intended metadata permission boundary and redact or authorize provenance fields consistently in graph, history, path, and reverse-dependency responses.

### NEO-04 — HIGH — Retraction leaves properties from a retracted claim

- **Component:** `backend/app/graph/repository.py:75-80,107-123`.
- **Reproduce:** POST records A and B with the same source-scoped entity ID but names `First name` and `Second name`; DELETE B; GET the entity.
- **Expected:** The surviving A fact determines the displayed name, or conflicting properties are explicitly source-qualified/unknown.
- **Actual/evidence:** GET returned 200 with `Second name` after B was retracted.
- **Cause (confirmed):** Every upsert overwrites entity properties on one node, while retraction only deactivates the source assertion and relationships.
- **Correction:** Store source-qualified property assertions and derive current entity properties from active authorized claims with deterministic precedence; verify update, conflict, and retraction cases.

### NEO-05 — MEDIUM — Local environment setup does not match backend loading

- **Component:** `backend/app/config.py:13-20`, `.env.example:1-12`, `README.md` setup instructions, `docker-compose.yml:6-7`.
- **Reproduce:** Run `docker compose config --quiet` in the current environment; then call `Settings.from_env()` without exported Neo4j variables.
- **Expected:** Documented local steps provide the backend and Compose a usable configured password.
- **Actual/evidence:** Compose reported required `NEO4J_PASSWORD` missing; `Settings.from_env()` raised Pydantic validation for empty `neo4j_password`. Supplying a temporary process environment made Compose and backend work.
- **Probable cause (inferred):** Compose reads `.env` for interpolation, while direct Uvicorn reads only process environment and receives no `--env-file`.
- **Correction:** Document explicit environment export or use a supported env-file startup command; keep credentials out of source.

### NEO-06 — MEDIUM — Old verification entry points and code remain

- **Component:** `frontend/package.json:12`, `frontend/tsconfig.app.json:21`, retained `backend/app/graph/client.py:7,21`, `backend/app/api_v1/security.py:12`, and old backend tests.
- **Reproduce:** Run `npm run test:e2e`; run backend pytest with the Neo4j driver available; inspect retained imports.
- **Expected:** New runtime and test commands target the Neo4j implementation.
- **Actual/evidence:** E2E script failed on missing `scripts/verify_phase12_ui.py`; pytest had three collection errors from missing `pglast`, `redis`, and a removed service. Retained source still imports the old graph client. The active new `main.py` does not import it, so these are residual code/test artifacts rather than a demonstrated active-process dependency.
- **Correction:** Replace the declared E2E command and old fixtures with new isolated Neo4j tests; remove obsolete source only after confirming no other current feature imports it.

### NEO-07 — MEDIUM — Path expansion lacks execution budget

- **Component:** `backend/app/graph/repository.py:185-203`, `backend/app/graph/driver.py:18-24`.
- **Reproduce:** The audit confirmed a 106-edge graph returns bounded output; a larger adversarial graph was not run. Code places `LIMIT` after variable-length path matching and sets no transaction/query execution timeout.
- **Expected:** Bounded database work as well as bounded response size.
- **Actual/evidence:** Response size and depth limits passed; an execution budget was not configured. Resource-exhaustion impact is **inferred**, not observed.
- **Correction:** Add query timeouts and workload-sized expansion constraints, then validate on isolated high-degree/cyclic graphs.

### NEO-08 — MEDIUM — New runtime lacks connector-to-normalization workflow

- **Component:** `backend/app/main.py:111-121`, retained `backend/app/ingestion/pipeline.py:7`.
- **Reproduce:** Inspect active route registration and post a normalized record through the API.
- **Expected:** Phase-3 full source → normalization → identity-resolution → graph workflow through active application interfaces.
- **Actual/evidence:** The new route accepts already normalized records and writes them successfully. No active new connector/normalizer/identity resolver is called by the new `main.py`; the retained pipeline depends on the old client.
- **Correction:** Add a new authorized connector/normalization slice when that full workflow is in scope; preserve the working normalized-record API contract.

### NEO-09 — LOW — Plain-text API errors surface as JSON syntax errors

- **Component:** `frontend/src/App.tsx:27-35`.
- **Reproduce:** Trigger the observed search HTTP 500 in Chromium.
- **Expected:** A status-aware server error message.
- **Actual/evidence:** The alert displayed `Unexpected token 'I', "Internal S"... is not valid JSON`.
- **Cause (confirmed):** `response.json()` runs before `response.ok` is checked and assumes every error body is JSON.
- **Correction:** Check status/content type, parse safely, and show a stable actionable error.

## 8. Blocked or Untested Areas

- A complete browser graph exploration could not proceed because search returned 500. Direct API detail, traversal, and evidence calls passed but do not prove their UI paths.
- Search pagination could not be exercised because all search calls failed. No claim of correct pagination is made.
- The current local environment had no usable `NEO4J_PASSWORD`; the real project Compose service and any existing project database were not started. Isolated Compose startup and volume behavior were checked instead.
- The backend repository suite did not execute test bodies because collection failed on obsolete imports and missing packages. The command stopped after the three reported collection errors; none were treated as passing.
- The declared frontend E2E command could not launch its first script because that file is absent. A separate real Chromium procedure was executed instead.
- A full external connector → normalization → identity-resolution path was not present in the active new runtime; no external source or production credentials were accessed.
- Concurrent write races, very large graph query cost, and token-compromise scenarios were not exercised. Related risks are marked inferred above.

## 9. Prioritized Fix List

1. **Security and workspace context:** Fix NEO-02 by canceling/scoping all frontend responses to active principal/tenant/workspace. Fix NEO-03 by enforcing the provenance metadata permission contract. Re-run adversarial workspace/tenant/evidence browser and API tests.
2. **Core API:** Fix NEO-01 so search works on empty and populated graphs; verify pagination and correct error response shape.
3. **Graph correctness:** Fix NEO-04 with source-qualified property facts and deterministic active-value selection; test overlapping claims, updates, and retractions.
4. **Startup/deployment:** Resolve NEO-05 so documented local startup supplies the backend credentials safely. Re-check clean Compose startup and backend startup.
5. **Ingestion scope:** Implement NEO-08 when connector ingestion is required; keep normalized-record authorization and idempotency.
6. **Test and cleanup:** Replace obsolete tests and scripts and remove retained old client modules once current imports are resolved (NEO-06). Add isolated two-tenant Neo4j fixtures.
7. **Reliability/UI:** Add path execution budgets (NEO-07) and status-aware frontend error parsing (NEO-09); measure on isolated cyclic/high-degree datasets.

## 10. Final Acceptance Decision

**REJECTED.** Core search fails with HTTP 500, preventing source-to-graph-to-UI completion. The browser can display stale data from a previously selected workspace, relationship responses disclose source record identifiers to a principal without evidence permission, and source retraction leaves stale entity properties. The backend and database foundation demonstrates useful working behavior, but those observed failures prevent acceptance under M12, M14, M15, M17, and M20.
