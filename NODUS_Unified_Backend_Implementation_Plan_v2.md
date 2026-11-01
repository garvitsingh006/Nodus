# NODUS — Unified Backend Implementation Plan v2.0

**Revised:** 31 October 2026  
**Change from v1:** Stage 07 is the fictional company definition and source dataset; Stage 08 is the preservation/patch stage for historical Stages 01–06.5; remaining backend stages run 09–16. Stage 16 is a chosen release gate, **not a forced upper limit**. Split a stage if engineering validation demands it.  
**Single forward implementation document:** Relationship graph + its effects are one implementation backlog in this file; this is **not** one monolithic Python source file.  
**Frontend remains deferred:** backend endpoints/events are built now; browser integration and browser acceptance happen after the backend gate.

## 0. Product and non-negotiable architecture

One fixed fictional company with multiple projects. User enters token → backend derives role/permissions/project → user specifies objective → user chooses only authorized sources → bounded investigator finds existing and new evidence → one policy-filtered graph view grows → user receives cited, uncertainty-aware report → verified relationships are retained for future authorized reuse. The UI is one progressively revealed page, but implementation of that page is a separate phase.

- **One cumulative Neo4j relationship graph**, not separate relationship and effects graphs, nor a second independently authoritative permission graph. Permissions reside in authoritative PostgreSQL policy/grant records; graph assertions carry provenance, source scope, temporal state and visibility metadata. With Neo4j Community, the trusted backend must enforce authorization before traversal and after evidence hydration. No browser/model raw unrestricted Cypher/SQL.
- **Stage 7 fixture source records are not the answer key.** Populate source-native records whose referential IDs, code references, timestamps and change histories form discoverable links; no scripted agent outputs, no prewritten findings, no directly seeded final discovery graph.
- **Controlled-demo token `4` is exact**, allowed only in an isolated non-sensitive environment with explicit server-side demo configuration. Admin/Developer/Intern scopes are server-resolved; users never select a role to gain permissions. Project membership, resource grants, source-level ACL and user-selected source IDs intersect, not union.
- **Agent ≠ permission authority or evidence authority.** Existing Gemini SDK investigator, Neo4j tools and signed/versioned skills are reused. A model may propose relationships; deterministic validators decide permitted factual writes. Hypotheses, observed activity, authoritative facts and counterfactuals must be kept separate.
- Start with genuinely useful deterministic relationship effects (PR change impact) before requiring parallel-agent infrastructure. Provider routing may use multiple Gemini models; only add other providers if policy, benchmarks and reliability justify it. Google ADK, Temporal, MongoDB Atlas, TigerData, Meko, MCP and A2A are optional; do not make MVP depend on them without a demonstrated need.
- Keep all original relationship requirement IDs **R01–R38** and effect IDs **E01–E24** in the unified backlog below. “Later” means deliberately deferred, not secretly completed or deleted. Do not infer a further unseen set of R IDs or create fictitious requirement coverage.
- Do not produce implementation audit/cleanup-report artifacts. Codex should implement small scoped tickets, run real integration/E2E checks and provide only a concise completion summary. Destructive cleanup of old company/demo code requires separate explicit approved targets.

## 1. Renumbered stages at a glance

| Stage | Name | Acceptance headline |
|---|---|---|
| **07** | Fictional Company Definition & Interconnected Source Dataset | One internally coherent company and reproducible source-native records; no precomputed findings |
| **08** | Historical 01–06.5 Completion & Compatibility Patches | Previously built tools, skills and Gemini investigator work against Stage 7 records; incomplete tasks explicitly closed or moved |
| **09** | Permission Identity, Project/Resource Authorization & Access Requests | Admin/Developer/Intern gates survive direct API and agent-tool abuse, revoke/expiry and narrow approval |
| **10** | Authorized Connectors, Source Catalog & Evidence Lifecycle | Actual source → evidence → Neo4j → read-time-scoped path and retraction across multiple source types |
| **11** | Objective-Driven Incremental Relationship Discovery | Objective + user-selected sources yields newly grounded, reusable graph paths without exhaustive whole-company crawl |
| **12** | Unified Relationship Intelligence & Effects | PR change-impact returns evidence-linked, typed consequences and explicit unknowns; additional effects only when supported |
| **13** | Multi-Model, Multi-Agent Investigation | Scoped planning/discovery/impact/verification/report tasks; independent check, budgets and controlled model routing |
| **14** | Durable Runs, Pauses, Handoffs & Recovery | Authorized runs resume after access approval, interruption, context exhaustion and retry without duplicate effects |
| **15** | Backend Streaming, Report & Frontend Contracts | A client can render same-page graph growth and cited reports entirely from stable authorized APIs/events |
| **16** | Full-Workflow Acceptance, Security, Reliability & Deployable Backend | Real DB + connectors + Gemini where available + hostile permission tests; backend deployable and frontend-ready |

## 2. Detailed stage implementations and exits

### STAGE 07 — Define and create the fictional company and its sources

**Goal:** Design one believable organization as a *set of source systems*, not a finished knowledge graph. This stage precedes agent/graph changes so every later stage has the same stable acceptance fixture.

**Deliverables:**
1. Define company identity, project boundaries (minimum A1 Payments and B2 Orders), shared cross-project dependencies, employees/teams/roles and an exact source/resource ID catalog.
2. Define an access matrix: isolated Admin (`4`), A1 Developer, A1 Intern, B2 Developer and optionally B2 Intern; include public/shared, project-restricted and document-level restricted resources. This is policy **specification/fixture data**; live enforcement begins in Stage 09.
3. Create reproducible *source-native* records for GitHub-like repos/PRs/commits/reviews; issue tracker work items/blockers; architecture docs/ADRs/runbooks; and database schema/foreign-key metadata. Prefer repository-backed fixtures or a local connector test service; real hosted source accounts are optional.
4. Include deliberately discoverable references across systems: a PR references a task; a task references a decision; a code API client calls a documented endpoint in another project; an issue is blocked by another issue. Include ambiguous identities, stale references, contradictory claims, change histories, missing evidence, and restricted records.
5. Prepare at least two data revisions: initial records and a revision with an updated/removed dependency or grant so later tests can verify retraction/invalidation. Keep all expected test assertions **outside production code**, as test-only fixtures.

**Acceptance:** Deterministic fixture validation proves source-native IDs, record references, time order, project ownership, sample restrictions and replayability. Opening the fixture directory does **not** reveal a prefilled Neo4j findings graph. Do not call fixture data genuine connected company observations.

**Boundary:** Stage 07 creates the *data and company definition*; Stage 09 implements the permission engine, Stage 10 connects source adapters, and Stage 11 makes relationships emerge from an objective.

### STAGE 08 — Patch and finish historical Stages 01–06.5

**Goal:** Preserve the existing implementation and change only the surfaces needed by the unified architecture. Not a rewrite, a legacy-data purge or another report.

**Required checks, in execution order:**
1. **01 foundation:** FastAPI ↔ Neo4j ↔ PostgreSQL startup, configuration, shared IDs, migrations, stable error formats and explicit isolated demo flag.
2. **02 graph/evidence:** ontology typing and edge direction, canonical identity collisions, evidence provenance/ACL, graph revision, temporal state and conflict/retraction behavior.
3. **03 connectors:** already-implemented JSON/GitHub adapters, source ID stability, real import/reimport and whether deletion/modified records are correctly represented. Unfinished source integrations move to Stage 10, rather than reimplementing everything now.
4. **04 tools:** verify the existing `graph.neighborhood`, `graph.find_paths`, `graph.reverse_dependencies`, `graph.relationship_lookup`, `graph.revision`, `evidence.lookup`, `evidence.conflicts`, `evidence.verify`, `ontology.validate_relationship`, and `source.coverage` interfaces and direct-call bounds.
5. **05 skills & gateway:** verify registered versioned skills, trusted loading, `verify-evidence`, `graph-trace`, `investigate-conflict`, Gemini SDK gateway, quota/cost policies and structured outputs. **GH-006C was previously reported COMPLETE** (three skills and tool workflows); confirm exact status in repo instead of rebuilding it. If the user has newer evidence showing an unfinished GH-006C subtask, complete only that gap.
6. **06–06.5 investigator:** check Gemini SDK real tool-calling from question to evidence-cited output, invalid tool args, no-evidence/blocked result, no unauthorized tool access, context/token ceilings and tracing. Distinguish **GH-007A** (first investigator), **GH-007B** (harness/budget/tracing/errors) and **GH-007C** (realistic live/security/E2E acceptance). The latter may still be outstanding: do not falsely label Stage 06 accepted without it.
7. **New compatibility contract:** introduce or adapt a server-controlled investigation context carrying actor/project, policy revision, selected source IDs, graph revision, authorized tools/skills and budgets. For now, use a deny-by-default test context until Stage 09 provides persisted identities/grants. Require `ClaimCandidate`/`RelationshipCandidate` with evidence IDs; never let the LLM write canonical asserted edges directly.

**Acceptance:** A real running old-implementation stack processes Stage 7 source fixtures through the existing tools and Gemini investigator, with evidence references and bounded output; no unreviewed legacy code or behaviors are silently dropped. All unverified behavior is either repaired or made a numbered task for a later stage. Full scoped identity/token implementation belongs to Stage 09, not this patch stage.

### STAGE 09 — Project identity, permissions and request lifecycle

**Build:** Server-owned token validation and hashed Developer/Intern credentials, demo-only literal admin `4`, actor/project/resource/action grants, role defaults, per-record/document classification, permission revision, expiration/revocation, request–approve/narrow/deny UI-independent API, source connector restriction, selected-source intersection and policy-safe error messages. No plain token in logs. Restrict traversal, metadata/catalog, snippets, report citations, caches, tool execution and event stream.

**Acceptance:** Admin, A1 developer, A1 intern and B2 developer see correct differing authorized source sets; a pending request grants nothing, a narrowed approval unlocks only its exact resource/action/expiry, and revoke/expiry takes effect on new reads and queued/active tool results. Test direct HTTP and tool APIs, not merely UI hiding. Token `4` must fail when isolated-demo mode is disabled.

### STAGE 10 — Source adapters, real records, normalization and retention

**Build:** Permission-filtered source catalog and real read-only acquisition from Stage 7 sources; connector fetch/pagination/retries/checkpoints, source-native record envelope, normalized assertions, identity collision handling, relationship type checks, source authority/coverage, evidence snapshots and idempotent Neo4j writes. At least GitHub and one independent source type must work end-to-end; document or database source should be added as necessary for first meaningful cross-source effect. Source changes, removals, and access revocations must invalidate or hide affected assertions and projections.

**Acceptance:** Real connector/local test-service → assertion → policy-protected Neo4j → typed graph path → authorized source citation works without LLM. Modify/delete one source record and verify its derived path is stale/retracted; distinguish 'not found' from 'not covered' where permitted. Do not fake all data as pre-inserted graph nodes.

### STAGE 11 — Objective-first selected-source graph discovery

**Build:** Objective planner with bounded expansion; only after objective do selected authorized source IDs enter run scope. Query existing authorized graph facts first, identify evidence gaps, selectively fetch records, resolve references, propose typed relationships, validate provenance/authorization and commit only qualified facts. Persist minimal run events and discovered graph IDs. Expose basic incremental discovery events; detailed streaming contracts are finalized in Stage 15.

**Acceptance:** Two different unseen objectives produce different relevant graph expansions from same company. Unselected or forbidden sources never enter a tool prompt or resulting node/path. Repeat same objective reuses current permitted facts, a revised source updates them, and an unknown path returns explicit coverage limits. Empty investigation view does not imply empty retained graph.

### STAGE 12 — Unified relationship intelligence and effect engine

**Build:** One relationship engine that supports both validated relationship discovery and evidence-based consequences. First complete **PR change impact (E05)**: changed files/API → typed service dependencies → possible downstream consumers → ownership/tasks/tests where supported; correct traversal direction and source version. Then add directed blocker chains (E16), decision genealogy (E14), expert suggestions (E02), conflict resolution and incident hypotheses only where actual source coverage supports them. Implement provenance-sensitive ranking, stale finding invalidation and no invented causal confidence.

**Acceptance:** Given a new PR from the company source, system returns evidence-backed potentially affected assets, explicit uncertainty and correct absent-data states. Editing code dependency or resolving the blocker changes the finding after resync. A repository-only code contribution does not magically assert formal service ownership.

### STAGE 13 — Permission-aware multi-model and coordinated investigative work

**Build:** Use existing Gemini SDK investigator as foundation. Implement bounded task plans with role-specific responsibilities: planner/coordinator, discovery investigator, impact analyst, independent verifier, resolution/report synthesizer. These may be configurations/skills within a pool, **not** separate always-on services. Use model gateway for tested routing by task type, allowed content, cost/latency and quality; second provider optional. Agents use deterministic tools and versioned trusted skills; validators are independent of agent confidence. Enforce global and task budgets; stop no-progress loops and surface partial findings.

**Acceptance:** Two investigators can process independently scoped tasks without seeing each other's restricted evidence; a verifier checks another worker's findings and refuses unsupported promotion; budget/model outage produces accurate partial output. Capability routing passes regressions when selected model changes.

### STAGE 14 — Run durability, checkpoints, access pauses and recovery

**Build:** PostgreSQL-persisted investigation lifecycle with idempotent work items, step completion records, safe retries, run ownership/fencing, checkpoint/handoff references, context rollover with source/evidence revalidation, and access-required pauses resumed after grant approval. Permission changes reauthorize resumed and currently running steps. Use Temporal only if tested failure/recovery requirements justify extra infrastructure; never run competing retry orchestrators.

**Acceptance:** Crash a worker during a source/tool step, recover without duplicate evidence promotion or final report, and preserve partial validated results. Denied grant leaves run blocked/partial; approved narrow grant resumes only scoped work; expired grant blocks it again. Handoff retains evidence IDs and policy revision, not a privileged free-text summary.

### STAGE 15 — Graph stream, report APIs and frontend handoff contracts

**Build:** Stable token-context, project, authorized-source, investigation start/status, access request, graph node/edge/delta, evidence details, report partial/final and run revisit endpoints. Server-sent events or equivalent ordered stream with monotonic run sequence, replay cursor, revision markers, `partial/blocked/unknown` statuses, filtered evidence citations and no confidential cached event reuse. This is **backend-only**; frontend rendering is subsequent work.

**Acceptance:** An HTTP/SSE test client drives token → objective → source selection → start → graph increments → report → revisit, and receives exactly the actor's permitted graph/report information. Switching token/permission revision invalidates old client authorization. Graph and report reconcile after stream reconnect.

### STAGE 16 — Full-system E2E, security, scale, deployable backend

**Build:** Full-workflow tests using real Neo4j and PostgreSQL, realistic source adapters, at least one live Gemini request when configured (mark live-unverified otherwise), adversarial token/project/source/prompt/graph permission tests, source disappearance/revision, model outage, worker crash and access-grant expiry. Benchmark bounded traversal and tool/model budgets; produce deployment configuration with demo-mode disabled by default. Scope legacy cleanup separately and require explicit targets/permission.

**Acceptance:** Automated backend acceptance covers both successful and denied/unresolved investigations with no leaks through counts, snippets, paths, agent text, cached events or reports. Deterministic graph/effects work without any model. The backend is deployable and its API/event contracts are stable enough to begin one-page frontend integration. **Do not call browser E2E complete until the frontend exists.**

## 3. Dependency order, not just numeric order

```text
07 Company source records and project/role specification
  ↓
08 Existing engine and Gemini investigator compatibility
  ↓
09 Auth identity, grants, requests (required before user-directed retrieval)
  ↓
10 Authorized source ingestion, normalization, revisions
  ↓
11 Objective-selected evidence discovery and incremental graph
  ↓
12 Deterministic relationship effects and source-backed findings
  ↓
13 Agent collaboration / task-aware model routing
  ↓
14 Durable runs / access-blocked resume / context handoffs
  ↓
15 Frontend-consumable graph and report interfaces
  ↓
16 Security/performance/integration acceptance and backend deployment
  ↓
FRONTEND INTEGRATION (separate phase, not falsely counted as backend acceptance)
```

**Thin slices early:** stage 09 starts minimal authorization used by stage 10; stage 11 may need simple events/checkpoints that Stage 14/15 harden later. Integration tests should run at each boundary, not only at stage 16. Stage 07 source fixture design should be stable enough to test every later vertical slice, but it can evolve through versioned data revisions.

## 4. First Codex tickets under the new numbering

| Ticket | Purpose | Minimum check |
|---|---|---|
| **ND-07A** | Define company manifest, projects/roles, resources and scope-matrix fixtures; do not implement graph conclusions | Validate IDs/roles/classifications and fixed-company assumption |
| **ND-07B** | Create source-native records with cross-system references, conflicting/stale/restricted cases and two revisions | Fixture linter proves consistency; no precomputed finding graph |
| **ND-08A** | Map actual Stages 01–06.5 interfaces and tasks to new requirements; run targeted live DB tests and any available model tests | Concise work summary only; no audit artifacts or destructive changes |
| **ND-08B** | Apply actual compatibility patches to existing graph/tools/skills/investigator, especially missing GH-007B/C checks | End-to-end Gemini-tool evidence output plus strict deny-by-default context |
| **ND-09A** | Implement demo-only token exchange and per-project/resource scope | Negative HTTP, tool, graph and evidence read tests |
| **ND-09B** | Access request, narrow admin grant, revoke/expire and policy revision | Restricted read pending/denied; allowed only after actual grant |

**Important naming:** GH-006C is *not* assumed outstanding. According to earlier project updates it was completed. The task ledger and actual code determine if any missing 006C subtask remains; GH-007C (realistic Stage 06 acceptance) should be checked independently. Do not delete, recreate, or downgrade working skills simply because of a naming mismatch.

## 5. Unified relationship-and-effects backlog (retained requirement IDs)

The prior **R01–R38** and **E01–E24** are retained **inside one unified backlog**, not as separate relationship-construction and relationship-effects workstreams. A row is complete only when its concrete source-backed behavior works under real authorization and revision changes; a checked box or passing unit suite alone is not sufficient. Stage references show the **new owner**, not a claim of previous completion. `Later` is deliberate prioritization rather than deletion, especially where supporting HR, IAM, CI, vendor or lineage data is unavailable. Original task IDs remain available to prevent requirement loss.

| Original ID | Stage / sequence | Unified relationship intelligence behavior and done check |
|---|---|---|
| **R01** | 08, 10 | Typed company ontology; accept only entity types justified by a connected source; validate required fields. |
| **R02** | 08, 10 | Stable canonical identities and aliases; cross-source matches require identifiers/evidence, never names alone. |
| **R03** | 08, 10 | Directed typed-edge registry; reject invalid source/target entity combinations. |
| **R04** | 08, 10 | Evidence-backed assertions; each fact/edge can be traced to an accessible source record and revision. |
| **R05** | 08, 12 | Store asserted/observed/inferred/hypothetical semantics distinctly; hypothetical never enters facts. |
| **R06** | 08, 10 | Temporal validity and supersession; former/current relationships remain distinguishable. |
| **R07** | 08, 12 | Conflict/authority service; two contradictory owners survive side-by-side until resolved. |
| **R08** | 09 | Actor/project/source/field-level graph authorization; forbidden paths/snippets/caches never leak. |
| **R09** | 08–11 | Typed entity, paths, reverse dependencies and evidence APIs with bounds and read-time authorization. |
| **R10** | 10 | Idempotent source upserts/retractions; deleting a source invalidates related facts and effects. |
| **R11** | 10 | Source-authority and coverage inventory; unknown availability is not reported as no relationship. |
| **R12** | 10 | Reusable permission-aware connector sync contract, checkpoints, retries, IDs, pagination and retractions. |
| **R13** | 10 | Live authorized GitHub PR/review/file/commit relationships with source-linked evidence. |
| **R14** | 10 | Jira-equivalent issue, assignee and directed blocker records when needed by first supported effect. |
| **R15** | 10 | Documents/ADRs/runbooks with page-level ACL and decision references when available. |
| **R16** | Later | Approved-scope discussion references; conversation text never becomes approval by inference. |
| **R17** | 12, if connected | Authoritative service catalog/owners distinct from contributor activity. |
| **R18** | 12, if connected | CI, build, deploy and commit lineage; distinguish proposed from deployed changes. |
| **R19** | 10–12, if selected | Database table/column/FK schema metadata; do not confuse FK topology with runtime data flow. |
| **R20** | Later | Verified data-pipeline/column lineage; outputs have traceable transformation evidence. |
| **R21** | 12 extension | Operational observations/incidents with exact times and observed runtime dependency evidence. |
| **R22** | Later | Least-sensitive authorized HR/directory lifecycle data for reliable personnel effects. |
| **R23** | Later | Source IAM/grant ingestion for comparing real policies; demo grant authority already Stage 07. |
| **R24** | Later | Verified product/customer/vendor/contract connections for business-impact effects. |
| **R25** | 11–12 | Real PR/commit/file ↔ work item ↔ actor links; unresolved ambiguous references stay unresolved. |
| **R26** | 11–12 | Work/decision/requirement/blocker connections with correct edge direction and provenance. |
| **R27** | 12 extension | Evidence-supported expertise and formal responsibility signals kept distinct. |
| **R28** | 12 | Declared/static/observed/inferred service/API/deploy paths labeled by certainty and version. |
| **R29** | Later | Data impact paths only for verified transformation/consumption lineage, not bare FKs. |
| **R30** | 12, if connected | Decision, rationale, participants and supersession timeline with source authority. |
| **R31** | 12 extension | Incident timeline and competing hypotheses; chronology alone cannot establish causation. |
| **R32** | Later | Verified policy-to-grant mismatch findings using authoritative policy/IAM records. |
| **R33** | 11, 15 | Focused authorized relationship explorer backed by incremental event and evidence APIs. |
| **R34** | 12 | Bounded typed dependency/impact algorithms; explain traversed propagation rules and truncation. |
| **R35** | Later | Counterfactual changes in isolated overlay; factual Neo4j graph remains untouched. |
| **R36** | 10, 12 | On source revision, invalidate dependent findings and graph projections deterministically. |
| **R37** | 08–16 | Full connector→Neo4j→scoped query→finding tests incl. collisions, forbidden paths, deletion. |
| **R38** | 09–16 | Bounds for fanout, depth, latency, volume, retries and model spend; truthful truncation. |
| **E01** | Later | Departure knowledge handoff from actual ownership, tasks, code, docs and successors. |
| **E02** | 12 early | Expert finder cites contributions/reviews/incidents; cannot invent formal ownership. |
| **E03** | Later | Single-person knowledge coverage risk changes when real backup evidence is added. |
| **E04** | Later | Role-specific onboarding connects permitted roles to evidence-backed relevant reading. |
| **E05** | 12 FIRST | Real PR change-impact chain through changed code and verified service dependencies; unknown is explicit. |
| **E06** | Later | Migration/column impact distinguishes structural from proven consumer paths. |
| **E07** | Later | API removal/deprecation affects only documented or observed consumers with version context. |
| **E08** | Later | Test-gap hints update with actual CI/test linkage, not invented missing tests. |
| **E09** | Later | Advisory → installed package version → deployed build/service exposure; not proof of exploit. |
| **E10** | 12 extension | Incident evidence produces competing qualified hypotheses and confidence limits. |
| **E11** | Later | Recurring incident pattern links change when incident/remediation data changes. |
| **E12** | Later | Stale runbook references point to exact changed/retired targets for human review. |
| **E13** | Later | Owner/escalation resolver uses current authoritative team and on-call data. |
| **E14** | 12 early | Decision genealogy reconstructs approved rationale/supersession from real docs/PRs. |
| **E15** | Later | Conflicting decisions checked within same version/time/scope; avoid false contradictions. |
| **E16** | 12 early | Directed cross-team blocker chains update when upstream issue status changes. |
| **E17** | Later | Org restructure preview via isolated read-only ownership/approval overlay. |
| **E18** | Later | Sensitive data propagation uses verified lineage, never foreign-key-only leakage claims. |
| **E19** | Later | Source-authoritative policy/access mismatch detector; separate from Stage 07 policy engine. |
| **E20** | Later | Offboarding closure distinguishes verified revocations from suggested steps. |
| **E21** | Later | Service→product/customer *potential* impact with entitlement and observed-use distinction. |
| **E22** | Later | Vendor/API shutdown exposure follows actual consumers and migration deadlines. |
| **E23** | 11–15 CORE | Objective→authorized graph→explainable finding/unknown via same-page APIs; no script launcher. |
| **E24** | 08–16 CORE | Realistic whole-flow acceptance, tenant/project isolation, edited sources and honest empty states. |

**Universal relationship-effect contract:** `real objective or record → selected authorized evidence → correct typed path → deterministic effect / hypothesis → independently validated evidence → affected resources + follow-up + explicit unknowns → revision-aware retention/invalidation`. **One engine, one graph, one output model.** No effect exists merely because two nodes are connected; propagation and causal meaning must be justified per edge type.

## 6. Mandatory full-workflow acceptance matrix

| Scenario | Must happen | Must not happen |
|---|---|---|
| A1 Developer investigates change in allowed payment repo | Cited relevant nodes and impact path | B2 private metadata, paths or source contents leak |
| A1 Intern selects subset of allowed sources | Restricted graph/report from chosen subset | Developer-only or unselected sources influence answer |
| B2 Developer investigates same objective | Different authorization-safe result | A1 private source names or counts appear through cached data |
| Admin token `4` with isolated-demo flag disabled | Token denied | Any production/public admin escalation |
| Permission request pending→approved narrowly→revoked | Read fails, succeeds only within approved scope, then fails | Pending request grants any permission or revoked evidence remains visible |
| One source PR/ADR/issue removed in later revision | Affected derived edges/findings stale/retract and report updates | Ghost dependencies stay current |
| Contradictory sources and missing data | Both claims and unknowns preserved, no unsupported root cause | LLM assigns sole authority or equates correlation with causality |
| Model rate-limit or unavailable | Deterministic graph functionality remains and run ends partial | Invented results, unsafe fallback or endless retry |
| Worker killed during step | Retry resumes from checkpoint and idempotency fence | Duplicate evidence/side effect/final report |
| Stream reconnect and token switch | Sequence/resume follows new permission and current run | Previous actor's graph, report or citation repaints page |

**Acceptance standard:** full connector→source normalization→Neo4j→scoped tool→analysis→report tests dominate. Unit suites supplement but do not replace real integration, adversarial failure and restart coverage. Dataset fixtures are isolated test records, not live customer evidence. Report measured metrics only; do not invent accuracy/latency figures.

## 7. Scope limits and release conditions

- Single-company, multi-project application; no multi-company SaaS management and no role-picker screen.
- Selected-source IDs are hard constraints and all readable graph results use current source permission; permission graph concepts are authorization data, not inference of entitlement.
- Add source domains or relationship effects only when records exist; missing coverage is an honest output, not an excuse for fake nodes.
- Preserve existing work from Stages 01–06.5 and signed/versioned skills, never hardcode Asteria's 24 scenarios or canned problem outputs.
- External system mutations, automatic remediations, full IAM/HR/vendor ingestion and enterprise-compliance claims are outside Stage 16 MVP.
- If live external credentials are missing, finish deterministic local-fixture verification and explicitly mark live connector/model tests unverified rather than passing them by assertion.
- No comprehensive audit-report files. This document is the single requested implementation roadmap; Codex implementation deliverables are running code/tests and concise final comments.

**Backend exit (Stage 16):** A user-specific token and selected source IDs can drive an unfamiliar objective to an authorized, incremental, evidence-backed graph and an accurate final/partial report; a changed/revoked source changes the answer, and the backend survives interruptions and denies forbidden access. Frontend work can then implement the agreed progressive page without re-architecting the backend.
