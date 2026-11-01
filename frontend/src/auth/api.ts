import { company } from './company'
import type { AuthorizationResolver } from './models'

export class AuthorizationError extends Error {}

const isRecord = (value: unknown): value is Record<string, unknown> =>
  typeof value === 'object' && value !== null && !Array.isArray(value)
const isText = (value: unknown): value is string => typeof value === 'string' && value.trim().length > 0
const isTextList = (value: unknown): value is string[] => Array.isArray(value) && value.every(isText)

async function readSession(path: string, token: string, signal: AbortSignal): Promise<unknown> {
  let response: Response
  try {
    response = await fetch(path, {
      headers: { Authorization: `Bearer ${token}` },
      signal, cache: 'no-store', credentials: 'omit', redirect: 'error',
    })
  } catch (error) {
    if (signal.aborted) throw error
    throw new AuthorizationError('Unable to reach the authorization service. Please retry.')
  }
  if (response.status === 401 || response.status === 403)
    throw new AuthorizationError('This access token was not accepted or has expired. Check it and try again.')
  if (response.status >= 500)
    throw new AuthorizationError(`Authorization service is unavailable (HTTP ${response.status}). Check that the backend API is running, then retry.`)
  if (!response.ok)
    throw new AuthorizationError(`Workspace authorization is unavailable (HTTP ${response.status}). Check the API configuration and NEXUS demo configuration, then retry.`)
  try { return await response.json() }
  catch { throw new AuthorizationError('The authorization service returned an invalid response. Please retry.') }
}

// Existing FastAPI contracts, not a client-defined authorization policy:
// /session -> subject, tenant_id, permissions
// /nexus/session -> employee_id, display_name, role_code, company_id,
//                   project_ids (assignments), source_ids, capabilities
export const resolveAuthorization: AuthorizationResolver = async (token, signal) => {
  const principal = await readSession('/api/v1/session', token, signal)
  if (!isRecord(principal) || !isText(principal.subject) ||
      !isText(principal.tenant_id) || !isTextList(principal.permissions)) {
    throw new AuthorizationError('The authorization service returned incomplete identity context. Please retry.')
  }
  const scope = await readSession('/api/v1/nexus/session', token, signal)
  if (!isRecord(scope) || !isText(scope.employee_id) || !isText(scope.display_name) ||
      !isTextList(scope.project_ids) || !isRecord(scope.capabilities) ||
      !['ADMIN', 'DEVELOPER', 'INTERN'].includes(String(scope.role_code)) ||
      scope.company_id !== company.id || principal.tenant_id !== 'nexus-tenant' ||
      principal.subject !== `nexus:${scope.employee_id}`) {
    throw new AuthorizationError('The authorization service returned incomplete or inconsistent workspace context. Please retry.')
  }
  const role = scope.role_code
  if (role !== 'ADMIN' && role !== 'DEVELOPER' && role !== 'INTERN')
    throw new AuthorizationError('This role is not supported.')
  return {
    identity: { id: principal.subject, name: scope.display_name },
    role, company,
    // The API exposes project IDs, not display names. Never substitute a client-side catalog.
    projects: [...new Set(scope.project_ids)].map(id => ({ id, name: id })),
    permissions: principal.permissions,
    capabilities: {
      requestAccess: scope.capabilities.request_access === true,
      reviewAccessRequests: scope.capabilities.review_access_requests === true,
    },
  }
}
