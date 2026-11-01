# NODUS frontend

From this directory:

```powershell
npm.cmd install
npm.cmd run dev
```

Vite serves the page at http://127.0.0.1:5173 and proxies /api to
http://127.0.0.1:8000. Start FastAPI separately from the repository root with
the existing Python environment and configured databases:

```powershell
.venv\Scripts\python.exe -m uvicorn backend.app.main:app --host 127.0.0.1 --port 8000
```

## Authorization and workspace context

The frontend uses these existing GET endpoints with an Authorization bearer
header and no cached responses:

- /api/v1/session: subject, tenant_id, permissions.
- /api/v1/nexus/session: employee_id, display_name, role_code, company_id,
  project_ids, source_ids, capabilities.

The fixed company display name comes from src/auth/company.ts, matching
nexus_src/7A/NEXUS_Stage_7A/company.json. It is displayed only after the
server confirms that company and a matching identity. The server's project_ids
are assignment IDs; no project display-name catalog or company-wide admin
project list is inferred. A missing/empty scope never unlocks other projects.
Access actions require the corresponding server capability, and admin review
also requires the server's ADMIN role and permission.

Each project switch re-resolves both session endpoints, removes the prior
objective immediately and aborts the old project scope. Future project requests
must use the new scope's projectId and signal. There is no project data cache.
Access-management actions currently open informational panels; no requests,
approvals or investigations are submitted.

The NEXUS session route requires the existing server-side demo mode and
imported PostgreSQL governance identities, roles and project assignments.
Database availability alone does not populate those records. This frontend
does not seed identities, enable demo mode, or map token values to roles.

## Checks

```powershell
npm.cmd run lint
npm.cmd run build
npm.cmd test
npm.cmd run test:e2e
```

Component/API tests and access.spec.ts use test-only mocks. For a real
FastAPI authorization check with no interception:

```powershell
$env:NODUS_LIVE_AUTH = '1'
npm.cmd run test:e2e -- live-authorization.spec.ts
```

This attempts token 4. If FastAPI rejects it, the diagnostic verifies that the
page remains locked and records that success is blocked. It does not count
rejection as proof of successful authorization. To require a successful flow,
supply a valid locally configured credential in NODUS_LIVE_TOKEN using your
local environment; do not commit it. The live test never creates credentials.

Production hosting must route /api to FastAPI on the same origin. The Vite
development proxy is not a production deployment configuration.
