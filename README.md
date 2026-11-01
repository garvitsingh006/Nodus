NODUS — Enterprise Dependency Intelligence

The problem and our solution, in one line: NODUS helps organizations prevent operational failures caused by fragmented knowledge and hidden dependencies by connecting enterprise information into a permission-aware knowledge graph, verifying change risks against evidence, and routing actionable remediation tasks to responsible teams.

An organization may document an API in one place, implement its consumers in another, discuss a migration in a ticket, and assign ownership in a service registry. Individually, those records look complete. Together, they can reveal a dependency that nobody considered before making a change.

NODUS is designed to make those relationships discoverable before a change causes an incident. It is an investigation and decision-support system, not a replacement for automated tests, monitoring, or engineering approval.

1. Example: What happens when the Authentication API changes?

Suppose a developer plans to remove the user_id field from the Authentication API response. The team that owns Authentication controls the API contract, but the Checkout service still reads user_id to associate a purchase with an authenticated user. Its owning team must also participate in a safe migration.

The relevant facts are spread across different records:

Record

What it tells us

Authentication API contract

The current response includes user_id.

Checkout source code

Checkout reads user_id from the Authentication API response.

Service/dependency catalog

Checkout depends on the Authentication API.

Change ticket or engineering decision

The team proposes removing user_id.

Ownership metadata

Identifies the teams responsible for each service.

Without NODUS

flowchart TD
    A["Proposal: remove user_id from Authentication API"]
    B["Authentication team updates its API"]
    C["API-local tests pass"]
    D["Review may miss the Checkout dependency"]
    E["Checkout still expects user_id"]
    F["Integration test or live request fails"]
    G["Teams manually trace code, tickets, and ownership"]
    A --> B --> C --> D --> E --> F --> G

The risk is not that teams have no tests or that every change will fail. Good contract and integration tests may catch the break. The problem is that local approval does not automatically provide cross-team context: a developer may not know which consumers, decisions, or teams need to be consulted.

With NODUS — intended end-to-end workflow

flowchart TD
    A["Ask: What breaks if Authentication API removes user_id?"]
    B["Choose accessible sources and investigation scope"]
    C["Enforce user and source permissions"]
    D["Search contracts, code, tickets, and service records"]
    E["Traverse graph: Authentication API → Checkout"]
    F["Check claim against the contract and Checkout code"]
    G{"Enough valid evidence?"}
    H["Explain verified risk and supporting evidence"]
    I["Mark unverified and state what is missing"]
    J["Resolve Authentication and Checkout owning teams"]
    K["Create evidence-linked remediation tasks"]
    L["Notify responsible teams via approved channel"]
    M["Track acceptance, remediation, and verification"]
    A --> B --> C --> D --> E --> F --> G
    G -->|Yes| H --> J --> K --> L --> M
    G -->|No| I

The important distinction: NODUS should not merely answer “Checkout might be affected.” It should show why: the declared dependency, the contract field being changed, and the consumer code that still uses that field. Then it should connect the risk to the teams able to resolve it and make the next action explicit. A missing or inaccessible source must not be presented as proof.

For this example, the intended handoff produces two distinct tasks, not one vague notification:

Responsible team

Example task

Completion evidence

Authentication API owning team

Preserve the existing user_id contract until consumers are migrated, or publish a compatible versioned API.

Updated API contract and compatibility-test result.

Checkout owning team

Remove its dependency on the old response field, update consumer code, and add a regression/contract test.

Reviewed code change and passing consumer test.

Each task should carry the affected service, risk summary, severity rationale, relevant evidence references, named owner or team, and a trackable status. A due date should come from policy or an authorized human—not be invented by the model. After the tasks are approved for routing, the system should notify the teams through a configured channel (such as an issue tracker or team messaging integration), record delivery, and track acknowledgment. A generated task is not the same as a successfully delivered notification.

The diagram above represents the complete product workflow. Graph search and Gemini-powered investigations have been exercised with the synthetic NEXUS environment. Strict claim-to-evidence verification and automatic owner resolution, task creation, and notification delivery have not yet been accepted as fully demonstrated end-to-end capabilities.

2. How the system works

flowchart TD
    U["Investigator in React / TypeScript UI"]
    API["Python / FastAPI backend"]
    AUTH["Identity, role, and selected-source authorization"]
    COORD["Investigation coordinator and specialist stages"]
    SEARCH["Read-only source search and retrieval tools"]
    GRAPH["Neo4j enterprise relationship graph"]
    LLM["Google Gemini: guided investigation and synthesis"]
    VERIFY["Structured evidence checks — in progress"]
    OWNER["Ownership resolver: service → responsible team"]
    TASK["Task records and authorized notification adapters — planned"]
    STATE["PostgreSQL: run and application state"]
    REPORT["Impact report, graph view, and evidence references"]
    U --> API --> AUTH --> COORD
    COORD <--> SEARCH
    COORD <--> GRAPH
    COORD <--> LLM
    COORD --> VERIFY --> OWNER --> TASK
    VERIFY --> REPORT
    TASK --> REPORT
    COORD <--> STATE
    REPORT --> U

What each component does

Technology or component

Role in NODUS

React + TypeScript

Lets users choose an investigation objective and authorized sources, inspect the result, and explore related entities.

FastAPI (Python)

Exposes session and investigation APIs and coordinates backend operations.

Neo4j

Represents services, APIs, projects, people, records, and the relationships between them; supports dependency traversal.

PostgreSQL

Stores structured application and investigation state, including persisted lifecycle data.

Google Gemini

Interprets the question, helps select investigative steps, and synthesizes observations into a readable explanation. The model is not treated as an authority on its own.

Source-search and graph tools

Retrieve relevant authorized records and traverse connected entities. Retrieval and graph relationships supply the context that the LLM alone lacks.

Verification/evidence logic

Is intended to test each reported claim against source references, permissions, version/freshness, and contradictory evidence. This is the main current completion gap.

Ownership and task routing (planned)

Uses graph/service ownership records to identify accountable teams, creates evidence-linked remediation tasks, and sends authorized, deduplicated notifications through configured integrations.

Docker

Runs local supporting services for the demonstration environment.

Demo data versus production integrations: NODUS currently uses the fictional NEXUS Technologies organization and its structured synthetic source records to demonstrate realistic cross-service investigations. GitHub code, issue trackers, and documentation are important source categories in the design; a synthetic record describing such a source is not evidence that a live GitHub, Jira, or Confluence connector has been deployed.

What the agents actually do

Discovery: Find entities, contracts, incidents, decisions, and owners relevant to the question.

Impact analysis: Follow dependency paths and distinguish direct from indirect impact.

Verification: Check whether the evidence supports specific claims; record missing or conflicting support instead of guessing.

Ownership resolution: Follow the affected service's ownership record to identify responsible teams; flag missing or conflicting ownership for human review.

Task routing and notification: Convert supported risks into proposed, evidence-linked tasks; after authorization, assign and notify the correct teams and track delivery and acknowledgment.

Reporting: Explain the impact in plain language, showing the dependency path, evidence, responsible teams, and follow-up status.

The coordinator can use model-guided investigation and bounded tools. Authorization remains a backend responsibility, not an instruction that the model is trusted to follow. A successful HTTP response or a fluent summary is not, by itself, proof that investigation findings are verified.

3. Core capabilities

A. Search: finding knowledge across silos

Conventional search often returns separate documents. NODUS combines source retrieval with graph navigation so that a query about the Authentication API can lead to its consumers, responsible teams, decisions, and code references.

The user specifies an objective and chooses sources they are allowed to access.

Retrieval locates candidate records; the graph makes relationships such as DEPENDS_ON, CALLS, and OWNED_BY useful for tracing impact.

The investigation follows relevant links instead of dumping the entire enterprise graph.

Permissions must be applied on source retrieval and subsequent tool calls; a relationship never grants access to restricted source content.

Example query: Which services could break if the Authentication API removes user_id, and who owns the affected consumer?

Current state: Source selection, graph-backed investigation, and Gemini-generated summaries have been demonstrated. Strict enforcement of the user-selected source list needs an explicit test: a report mentioning an unselected source should trigger inspection of the underlying tool calls.

B. Verification: separating a plausible answer from a supported finding

An LLM can produce a coherent impact story without proving it. NODUS's verification layer is designed to treat the model's output as candidate claims.

For the Authentication API example, a verification check should establish that:

The proposed contract change actually removes user_id.

The identified Checkout consumer reads the field in the relevant version of code.

The relationship and code references belong to the authorized, selected investigation scope.

The evidence is current enough for the stated conclusion, with contradictions surfaced.

Possible outcomes are supported, contradicted, or insufficient evidence. These are evidentiary judgments, not guarantees that a production incident will or will not happen. Contract tests and integration tests are still necessary to confirm runtime compatibility.

Current state: Verification-related stages and tests exist, but the demonstrated investigation output has included empty structured findings and evidence_refs. Independent, claim-level verification is therefore not yet an accepted end-to-end capability.

C. Evidence: making findings inspectable

Every significant claim should connect to the specific records that justify it:

Claim

Required evidence

Why it matters

Authentication API exposes user_id

Versioned API specification or response contract

Establishes what changes.

Checkout depends on user_id

Consumer code or contract test plus dependency path

Establishes an actual breaking dependency, not merely a related service.

The proposed change is planned

Authorized change ticket or decision record

Distinguishes a proposal from an implemented change.

A team owns the remediation

Current service ownership record

Identifies who should assess or change the consumer.

A useful evidence record includes a source ID, relevant location (file/line or document section), source version or timestamp, the claim it supports, and its verification status. References must remain inspectable only by users authorized to view them.

The report should keep observed facts, inferences, and recommendations visually and semantically separate. A summary that names a source without a resolvable evidence reference is not a verified finding.

Current state: Turning search results into enforceable, inspectable claim → evidence mappings is active development. Continuous evidence freshness checks and persistent re-verification when sources change are future capabilities; they should not be presented as already running automatically.

D. Ownership and action: closing the investigation loop

A verified risk is useful only if someone can act on it. NODUS is intended to resolve ownership through service/team/project records, propose tasks for the relevant teams, and notify them with enough context to start work without repeating the entire investigation.

For the Authentication API change:

Identify the Authentication API owner responsible for safe compatibility or versioning.

Identify the Checkout consumer owner responsible for the dependent code and tests.

Create separate tasks with explicit expected changes and the supporting claim → evidence links.

Enforce assignment and notification permissions, avoid duplicate tasks for the same risk, and require review when ownership is ambiguous.

Deliver approved notifications through configured integrations and record whether each was sent, acknowledged, completed, or blocked.

Re-check the relevant contract and consumer evidence before marking the risk resolved.

This is evidence-based remediation coordination, not autonomous modification of source code or unrestricted messaging. No team should receive sensitive source excerpts it is not authorized to view.

Current state: Owner lookup, task assignment, notification delivery, and subsequent closure verification form the next workflow extension. They must be integrated and tested before the README can claim automatic team notification is working.

4. Why NODUS is feasible

NODUS does not need to recreate a company's engineering systems or train a new foundation model.

The inputs already exist. Companies already produce API contracts, code, tickets, architecture records, ownership tables, and decision histories. The first task is to ingest and normalize selected sources, not invent new records.

The foundation is available. FastAPI handles orchestration, Neo4j handles connected dependencies, PostgreSQL handles state, and Gemini helps interpret and explain relevant findings. These are separable, testable components.

The demo is bounded. A synthetic NEXUS organization provides known services, sources, employees, and expected dependency paths. That makes evaluation repeatable and avoids needing confidential customer integrations for the prototype.

Permissions and verification can be deterministic. The AI can propose hypotheses, while ordinary code checks source scope, reference existence, contract differences, graph paths, and the status of evidence. The model need not be trusted to certify itself.

Ownership and task routing are incremental. Once a supported risk is linked to a service, NODUS can use existing ownership metadata to propose tasks. A narrow authorized integration can create those tasks and send notifications, with deduplication and delivery-status logging.

Success is measurable. Seed a known breaking API change; test whether the system finds the consumer, cites the contract and code, respects source permissions, routes tasks to the correct teams, and records notification outcomes. Also test missing owners, duplicate risks, unauthorized recipients, and delivery failures.

Automation remains controlled. NODUS can propose and route remediation tasks after authorization. Automatically editing code, blocking deployments, or synchronizing all enterprise tools continuously would require additional safeguards and is not necessary for a credible first version.

5. What NODUS is — and is not

NODUS is: A permission-aware investigation and remediation-coordination layer that connects organizational context, traces hidden dependencies, aims to make operational-risk claims auditable, and routes supported risks to accountable teams.

NODUS is not: A guarantee that no outage will occur, a replacement for CI/contract tests, or a claim that a general-purpose coding agent cannot search the same sources. Its intended differentiation is the shared dependency model, controlled source scope, structured evidence, accountable task routing, and persistent verification lifecycle—several of these end-to-end capabilities are still being built and validated.
