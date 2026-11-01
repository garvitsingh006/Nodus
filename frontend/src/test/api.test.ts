import { afterEach, expect, it, vi } from 'vitest'
import { resolveAuthorization } from '../auth/api'

const principal = { subject: 'nexus:employee', tenant_id: 'nexus-tenant', permissions: ['READ_GRAPH'] }
const scope = {
  employee_id: 'employee', display_name: 'Test person', company_id: 'nexus',
  role_code: 'INTERN', project_ids: ['A1'], source_ids: [],
  capabilities: { request_access: true, review_access_requests: false },
}
function mockSessions(overrides = {}) {
  const fetcher = vi.fn()
    .mockResolvedValueOnce(new Response(JSON.stringify(principal)))
    .mockResolvedValueOnce(new Response(JSON.stringify({ ...scope, ...overrides })))
  vi.stubGlobal('fetch', fetcher)
  return fetcher
}
afterEach(() => vi.unstubAllGlobals())

it('reads actual session contracts using only bearer headers and server projects', async () => {
  const fetcher = mockSessions({ project_ids: ['A1', 'A1', 'B2'] })
  const result = await resolveAuthorization('4', new AbortController().signal)
  expect(result.role).toBe('INTERN')
  expect(result.company.name).toBe('NEXUS Technologies')
  expect(result.projects).toEqual([{ id: 'A1', name: 'A1' }, { id: 'B2', name: 'B2' }])
  expect(result.capabilities).toEqual({ requestAccess: true, reviewAccessRequests: false })
  expect(fetcher.mock.calls.map(call => call[0])).toEqual(['/api/v1/session', '/api/v1/nexus/session'])
  for (const call of fetcher.mock.calls)
    expect(call[1]).toEqual(expect.objectContaining({ headers: { Authorization: 'Bearer 4' }, cache: 'no-store', redirect: 'error' }))
})
it.each([
  { employee_id: 'another-employee' },
  { company_id: 'another-company' },
  { role_code: 'OWNER' },
  { project_ids: ['A1', null] },
  { capabilities: null },
])('rejects malformed or mismatched scope: %j', async overrides => {
  mockSessions(overrides)
  await expect(resolveAuthorization('opaque', new AbortController().signal)).rejects.toThrow('workspace context')
})
it('does not coerce capability strings into permission', async () => {
  mockSessions({ capabilities: { request_access: 'true', review_access_requests: 'true' } })
  const result = await resolveAuthorization('opaque', new AbortController().signal)
  expect(result.capabilities).toEqual({ requestAccess: false, reviewAccessRequests: false })
})
it('accepts a genuinely empty project scope without inventing a fallback', async () => {
  mockSessions({ project_ids: [] })
  expect((await resolveAuthorization('opaque', new AbortController().signal)).projects).toEqual([])
})
it('reports the missing scope route without guessing another resolver', async () => {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValueOnce(new Response(JSON.stringify(principal)))
    .mockResolvedValueOnce(new Response('', { status: 404 })))
  await expect(resolveAuthorization('opaque', new AbortController().signal)).rejects.toThrow('NEXUS demo configuration')
})
it.each([401, 403])('rejects invalid or revoked authorization (%i)', async status => {
  const fetcher = vi.fn().mockResolvedValue(new Response('private backend details', { status }))
  vi.stubGlobal('fetch', fetcher)
  await expect(resolveAuthorization('secret', new AbortController().signal)).rejects.toThrow('not accepted or has expired')
  expect(fetcher).toHaveBeenCalledTimes(1)
})
it('maps network failures to retry guidance', async () => {
  vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new TypeError('network')))
  await expect(resolveAuthorization('opaque', new AbortController().signal)).rejects.toThrow('Please retry')
})
it('explains proxy/backend failures without exposing response details', async () => {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response('internal proxy details', { status: 500 })))
  await expect(resolveAuthorization('opaque', new AbortController().signal)).rejects.toThrow('Check that the backend API is running')
})
it('rejects malformed JSON', async () => {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response('not-json')))
  await expect(resolveAuthorization('opaque', new AbortController().signal)).rejects.toThrow('invalid response')
})
