# NODUS — Frontend Requirements Plan

**Version:** 1.0 — 31 October 2026  
**Status:** Frontend design requirements established; implementation deferred  
**Next focus:** New backend architecture

## 1. Product intent

NODUS is an objective-driven, permission-aware relationship intelligence system for **one company**. A user provides an investigation objective, selects authorized sources, and starts an agent investigation. Evidence-backed relationships appear incrementally in a graph. A report follows underneath, and useful relationships are retained for future authorized investigations.

The frontend is **one progressively revealed page**, not a multi-tab dashboard.

## 2. Non-negotiable product decisions

1. One fixed demo company. No company/tenant switcher.
2. No sign-in, sign-out, registration, or role-selection dropdown.
3. A token is entered on the opening screen. The backend resolves the role, project and permissions. Users never select their role manually.
4. The **only admin token is exactly 4**. This is a controlled-demo rule, not production-safe authentication.
5. Developer and intern tokens grant project-specific access; interns can see only explicitly permitted subsets. Admins see all company projects.
6. Developers and interns may request additional project/resource access; the admin approves or denies requests.
7. The user enters an objective **before** selecting sources.
8. Source selection appears inside that objective's workflow, never as a separate source-management or ingestion tab.
9. The graph starts empty and grows only from actual agent investigation events.
10. The report is displayed **directly below** the relationship graph.
11. Evidence-backed relationships are retained and reused only where the user's permissions allow.

## 3. Page state and reveal sequence

| State | Visible UI | Trigger |
| --- | --- | --- |
| S0 — Locked | Centered NODUS logo, thin horizontal line, token field | Open application |
| S1 — Authorized | Same page, fixed company/project/role context in header, objective field below | Token accepted |
| S2 — Sources | Objective and authorized source selector below it | Objective confirmed |
| S3 — Investigating | Agent activity strip and expanding relationship graph | Start investigation |
| S4 — Reporting | Graph above, progressive evidence-backed report below | Findings arrive |
| S5 — Finished/partial | Final/partial report, evidence, limitations and retained relationship status | Investigation ends |
| Error/blocked | Concise inline explanation and recovery action | Invalid token, denied source, tool failure, or insufficient evidence |

There is **no route or full-page change** between these states. The next section fades into place vertically. Keep transitions subtle (approximately 200–350 milliseconds), and honor reduced-motion preferences.

## 4. Frontend requirements

### FE-01 — Initial token gate

- Show NODUS, a short divider, and an input labeled Access token.
- No other application content is visible until the token is validated.
- Invalid keys receive a brief error without leaking company or project data.
- On success, the token input may collapse into an Access granted indicator with a small Change token action.
- Changing the token is instant and requires no sign-out. It must discard previously rendered protected data and detach any investigation belonging to the previous identity.
- Never show quick-select Admin / Developer / Intern controls.

### FE-02 — Header context

- Fade in the same page after token resolution.
- Show company name, currently authorized project, and a non-interactive role badge.
- Admin: show Review access requests.
- Developer/intern: show Request additional access.
- Project switching is available only among projects already granted to the user.
- The company name remains fixed.

### FE-03 — Objective entry

- Show one prominent, free-text question/objective field and a Continue action.
- Allow arbitrary real-world problems: failed systems, supply-chain exposure, unexpected changes, fragile dependencies, weak links and similar investigations.
- No prewritten scenarios, simulation menus, hard-coded objectives or fictional findings.
- Keep a concise objective summary visible while agents work.

### FE-04 — Authorized source selection

- Reveal the selector beneath the objective only after the objective is provided.
- Show authorized source handles for the current principal and project: for example permitted GitHub repositories, Jira projects, databases and internal documents.
- The user picks relevant sources. Selected source IDs become the permitted investigation scope.
- Never expose unauthorized source names or confidential metadata even in disabled controls or client-side payloads.
- If the user has no relevant source access, offer the access-request flow.
- There is no separate Sources tab, onboarding wizard, or visible ingestion pipeline.
- **Technical clarification:** Selecting a source is a UI interaction; the backend still requires a way to fetch and interpret permitted data. That mechanism stays behind the interface.

### FE-05 — Live agents and relationship graph

- On Start investigation, reveal a compact agent-activity area followed by an initially empty graph.
- As agents discover evidence-backed entities and relationships, add nodes and edges incrementally.
- Show the distinction between hypothesis, supported relationship, contradiction and verified relationship.
- Clicking a node/edge reveals a meaningful explanation and accessible evidence references.
- Smooth pan/zoom, stable layout, path focus and readable labels are required.
- Do not animate fictional progress or pre-populate a company-wide graph.
- Progress events must correspond to real agent operations, not scripted demo scenarios.

### FE-06 — Report below graph

- Show the report immediately beneath the relationship graph, not in a new tab.
- Sections: answer/summary; root causes and dependency chain; affected systems; cited evidence; recommendations; unknowns and uncertainties.
- Display preliminary findings as preliminary. Mark the report final or partial when the run ends.
- If evidence is insufficient, explicitly state that no supported conclusion was established.
- Report claims must link back to evidence the user is authorized to view.

### FE-07 — Access request workflow

- Developer/intern requests a specific resource or project with a justification.
- The request stays pending until an admin approves or denies it.
- Admin reviews requests from an inline panel or drawer on the same page.
- Approval gives only the requested resource/project scope, not a higher role.
- The UI refreshes effective permissions when the backend confirms a grant; pending requests do not unlock protected sources.

### FE-08 — Retained relationship memory

- Confirmed useful relationships survive the original investigation.
- A later objective may reuse relevant saved relationships and their supporting evidence.
- The current user's source/project permissions still restrict retrieval and display.
- Stale or contradicted relationships must be visibly identified; old conclusions do not override newer evidence.
- There is no standalone Knowledge Graph tab; relevant knowledge appears in the active investigation.

## 5. Access rules

| Identity | Project access | Resource access | Additional action |
| --- | --- | --- | --- |
| Admin, token 4 | All company projects | All company resources | Approve/deny requests |
| Developer, example token sofsonsofsvnos-A1 | Project A1 and approved additions | Resources allowed for that project | Request more |
| Intern, project A1 token | Project A1 and approved additions | Explicitly permitted A1 subset | Request more |

**Important:** The frontend may only render server-authorized context. The backend must enforce permissions for source reads, agent tool calls, graph updates, evidence retrieval and reports. A frontend-hidden button is never an authorization boundary. Exact admin key 4 is for a private, controlled demonstration only.

## 6. Visual direction

- Dark charcoal/slate background with restrained lime-green highlights.
- Minimal initial gate: centered logo, small line, token input.
- After access: compact top header and centered vertical work area.
- Section reveals are calm, polished and quick; the graph receives the greatest amount of space.
- Graph should grow without constantly rearranging existing nodes.
- No sidebar, role picker, company picker, scenarios, simulations, Demo Lab, source dashboard or separate report route.
- Desktop-first for the judging presentation; responsive and keyboard-accessible.

## 7. Backend capabilities required by this UI

The following are **logical contracts, not committed API endpoints or stack choices**.

| Backend capability | Frontend dependency |
| --- | --- |
| Token and scope resolver | Token validity, role badge, authorized projects and capabilities |
| Scoped resource catalog | Display-safe source handles for one user/project |
| Access-request service | Submit, status, admin decisions and grant updates |
| Investigation lifecycle | Create from objective plus selected authorized sources; query, stop and resume |
| Agent activity stream | Ordered, real progress events for the active investigation |
| Relationship stream | Stable node/edge IDs; status, evidence links, additions, updates and retractions |
| Evidence service | Access-checked evidence details for relationships and report claims |
| Report service | Structured provisional/final findings, causes, impacts and uncertainties |
| Relationship memory | Durable evidence-backed relations with project/resource access constraints |

### Mandatory architecture constraints

- Authorize **before** agents read sources; enforce again on every tool call.
- Scope new investigation to its objective and selected sources, plus only relevant permitted saved relationships.
- Stream graph changes as events tied to a single investigation ID.
- Preserve source citations and relationship status (hypothesis vs verified).
- Ensure access-revocation or token-switch handling cannot leak old-role data.
- Admit uncertainty and incomplete results; never invent conclusions.

## 8. Deferred frontend implementation tasks

| Order | Task | Required backend contract |
| --- | --- | --- |
| UI-01 | Token gate and in-place access reveal | Token resolver |
| UI-02 | Fixed company/project context and safe token switching | Principal scope |
| UI-03 | Objective input and permitted source selection | Source catalog |
| UI-04 | Resource access requests and admin decisions | Grants/requests |
| UI-05 | Agent progress and growing evidence graph | Investigation and event stream |
| UI-06 | Evidence inspection and report beneath graph | Evidence and report services |
| UI-07 | Reusing retained relationships, responsive motion and UI polish | Relationship memory |

### Completion conditions for future UI work

- Before token acceptance, only the branded access gate is visible.
- Token 4 always means admin; no role-selection dropdown exists.
- Developer A1 cannot see Project B2 data unless an admin explicitly grants appropriate access.
- Objective is submitted before resources are chosen.
- Graph begins empty and is updated only through real investigation events.
- Report is below the graph and preserves evidence references and uncertainty.
- Token changes remove protected previous-role content immediately.
- The flow remains a single progressive page.

## 9. Non-goals

No frontend implementation during the present architecture phase. No previous Company Brain, simulations, canned scenarios, Demo Lab, multi-company switching, standalone ingestion UI, fake agent runs, or old product documentation. No development audit reports.

## 10. Next task: backend architecture

Design these capability boundaries in dependency order:

1. Principal/token resolution, project permissions and access grants.
2. Company source registry and on-demand data adapters.
3. Investigation state and agent orchestration.
4. Evidence-backed entity and relationship discovery.
5. Durable permission-aware relationship memory.
6. Streaming investigation events and report synthesis.

Decisions to resolve in the backend architecture phase: trust boundaries, databases/graph storage, source adapters, agent/tool responsibilities, agent handoff method, graph/evidence model, streaming transport, and permission-grant duration.

**Handoff:** This document fixes the desired frontend behavior. Proceed to backend architecture before implementing the UI. Do not revive the legacy frontend or old workflows.
