# NODUS

NODUS stores source-backed entities and directed relationships in Neo4j Community Edition. Its FastAPI service requires an explicit authenticated tenant and workspace for graph access. A fresh database contains no entities until authorized source records are posted.

NODUS is moving toward a single-company, objective-driven relationship investigation system: a user states an investigation objective, NODUS discovers relevant relationships, and agents investigate causes, dependencies, risks, and weak links. That direction is not yet implemented — the sections below describe what exists today.

## Configuration

Copy `.env.example` to `.env`, set `NEO4J_PASSWORD`, and set `GRAPH_HACKS_API_PRINCIPALS_JSON` to a JSON object mapping opaque bearer tokens to principals. Each principal needs `subject`, `tenant_id`, `workspaces`, and `permissions`. Writers also need `source_ids`, listing the sources they can update. Use `READ_GRAPH` for graph queries, `READ_EVIDENCE` for evidence and sources, and `MANAGE_INGESTION` for writes. `ADMIN` grants all permissions within a principal's listed workspaces. Keep the real `.env` private.

Install backend dependencies with `python -m pip install -r requirements.txt`. Start the database with `docker compose up -d neo4j`, then run `uvicorn backend.app.main:app` from the repository root. The backend loads the root `.env` without overriding process environment variables, checks Bolt readiness, and initializes its schema. From `frontend`, run `npm install` and `npm run dev`.

## Source record ingestion

Post a normalized record to `POST /api/v1/workspaces/{workspace_id}/records` with a bearer token that has `MANAGE_INGESTION`. For example:

```json
{
  "source_id": "github",
  "record_id": "org/repo/pull/42",
  "title": "Pull request 42",
  "uri": "https://example.org/pull/42",
  "acl_subjects": [],
  "entities": [
    {"external_id": "pr:42", "type": "PULL_REQUEST", "name": "Update service"},
    {"external_id": "service:api", "type": "SERVICE", "name": "API"}
  ],
  "relationships": [
    {"source_external_id": "pr:42", "target_external_id": "service:api", "type": "AFFECTS"}
  ]
}
```

The record ID and source ID identify the source fact within its tenant and workspace. Reposting updates it; deleting `/api/v1/workspaces/{workspace_id}/records/{source_id}/{record_id}` retracts it. `acl_subjects` restricts graph facts and evidence to listed principal subjects; an empty list allows all authorized principals in that workspace. A record may give an entity an explicit `canonical_id` when a stable authoritative identity is known across sources. Otherwise its identity is source-scoped; matching names never merge entities. When visible sources disagree on an entity property, the API returns `null` for that property and lists the conflicting values.

For a local file import, set `GRAPH_HACKS_API_TOKEN` to a principal with `MANAGE_INGESTION` and the file's `source_id` in its `source_ids`, then run `python scripts/import_json.py path/to/source.json --workspace WORKSPACE_ID`. The file uses this structure:

```json
{
  "source_id": "local",
  "records": [{
    "id": "record-1",
    "title": "Local source document",
    "revision": "1",
    "entities": [
      {"id": "person-1", "kind": "PERSON", "name": "A person"},
      {"id": "team-1", "kind": "TEAM", "name": "A team"}
    ],
    "links": [{"from": "person-1", "to": "team-1", "kind": "BELONGS_TO"}]
  }],
  "deleted_record_ids": []
}
```

The import endpoint parses and validates the file, assigns stable source-scoped identities, and writes each record transactionally. Reimporting the same revision is idempotent. The optional `POST /api/v1/workspaces/{workspace_id}/imports/github` endpoint imports up to 20 public pull requests from a named GitHub repository; a `GITHUB_TOKEN` may be supplied for GitHub API limits. The public connector was exercised against a real repository during development.

The explorer uses `/api/v1/workspaces`, `/search`, `/entities/{id}`, `/neighborhood`, `/history`, `/assertions`, `/evidence`, and `/sources`. Graph operations have bounded result counts and path depth. Neo4j Community uses application-enforced tenant and workspace scoping within one database.
