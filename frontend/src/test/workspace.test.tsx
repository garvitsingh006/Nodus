import { act, render, screen } from '@testing-library/react'
import { renderHook } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, expect, it, vi } from 'vitest'
import App from '../App'
import { resolveAuthorization, AuthorizationError } from '../auth/api'
import type { AuthorizationContext } from '../auth/models'
import { useAuthorization } from '../auth/useAuthorization'

vi.mock('../auth/api', async original => ({ ...await original<typeof import('../auth/api')>(), resolveAuthorization: vi.fn() }))
const resolve = vi.mocked(resolveAuthorization)
const base: AuthorizationContext = {
  identity: { id: 'person', name: 'Test person' }, role: 'DEVELOPER',
  company: { id: 'nexus', name: 'NEXUS Technologies' },
  projects: [{ id: 'a', name: 'Project A' }, { id: 'b', name: 'Project B' }],
  permissions: ['READ_GRAPH'], capabilities: { requestAccess: true, reviewAccessRequests: false },
}
beforeEach(() => { resolve.mockReset() })
async function enter() {
  const user = userEvent.setup()
  render(<App />)
  await user.type(screen.getByLabelText('Access token'), 'opaque{Enter}')
  await screen.findByRole('heading', { name: 'Project A' })
  return user
}
it.each(['ADMIN', 'DEVELOPER', 'INTERN'] as const)('shows the authorized %s access action', async role => {
  resolve.mockResolvedValue({
    ...base, role, permissions: role === 'ADMIN' ? ['ADMIN'] : ['READ_GRAPH'],
    capabilities: { requestAccess: true, reviewAccessRequests: role === 'ADMIN' },
  })
  const user = await enter()
  expect(screen.getByText('NEXUS Technologies')).toBeVisible()
  expect(screen.getByLabelText('Authenticated role')).toHaveTextContent(role)
  const action = role === 'ADMIN' ? 'Review access requests' : 'Request additional access'
  await user.click(screen.getByRole('button', { name: action }))
  expect(screen.getByRole('complementary', { name: action })).toHaveTextContent('future update')
  if (role !== 'ADMIN') expect(screen.queryByRole('button', { name: 'Review access requests' })).not.toBeInTheDocument()
})
it('hides the access entry when the backend denies that capability', async () => {
  resolve.mockResolvedValue({ ...base, capabilities: { requestAccess: false, reviewAccessRequests: true } })
  await enter()
  expect(screen.queryByRole('button', { name: 'Request additional access' })).not.toBeInTheDocument()
  expect(screen.queryByRole('button', { name: 'Review access requests' })).not.toBeInTheDocument()
})
it('only lists returned projects', async () => {
  resolve.mockResolvedValue(base)
  await enter()
  expect(screen.getAllByRole('option').map(item => item.textContent)).toEqual(['Choose an authorized project', 'Project A', 'Project B'])
  expect(screen.queryByText('Project C')).not.toBeInTheDocument()
})
it('omits switching for a single authorized project', async () => {
  resolve.mockResolvedValue({ ...base, projects: [base.projects[0]] })
  await enter()
  expect(screen.queryByRole('combobox')).not.toBeInTheDocument()
})
it('shows restricted access without objective entry for zero projects', async () => {
  resolve.mockResolvedValue({ ...base, projects: [] })
  const user = userEvent.setup()
  render(<App />)
  await user.type(screen.getByLabelText('Access token'), 'opaque{Enter}')
  expect(await screen.findByRole('heading', { name: 'No authorized projects' })).toBeVisible()
  expect(screen.queryByRole('combobox')).not.toBeInTheDocument()
  expect(screen.queryByLabelText('What would you like to investigate?')).not.toBeInTheDocument()
})
it('revalidates on switching and discards the previous objective, including when switching back', async () => {
  resolve.mockResolvedValue(base)
  const user = await enter()
  await user.type(screen.getByLabelText('What would you like to investigate?'), 'Private A objective')
  await user.selectOptions(screen.getByLabelText('Switch project'), 'b')
  expect(await screen.findByRole('heading', { name: 'Project B' })).toBeVisible()
  expect(screen.getByLabelText('What would you like to investigate?')).toHaveValue('')
  await user.selectOptions(screen.getByLabelText('Switch project'), 'a')
  expect(await screen.findByRole('heading', { name: 'Project A' })).toBeVisible()
  expect(screen.getByLabelText('What would you like to investigate?')).toHaveValue('')
  expect(resolve).toHaveBeenCalledTimes(3)
  expect(screen.getByText('NEXUS Technologies')).toBeVisible()
})
it('clears previous content immediately and ignores out-of-order switch responses', async () => {
  let finishOld!: (value: AuthorizationContext) => void
  let finishNew!: (value: AuthorizationContext) => void
  resolve.mockResolvedValueOnce(base)
    .mockImplementationOnce(() => new Promise(done => { finishOld = done }))
    .mockImplementationOnce(() => new Promise(done => { finishNew = done }))
  const user = await enter()
  await user.type(screen.getByLabelText('What would you like to investigate?'), 'Private A objective')
  await user.selectOptions(screen.getByLabelText('Switch project'), 'b')
  expect(screen.queryByLabelText('What would you like to investigate?')).not.toBeInTheDocument()
  expect(screen.queryByRole('heading', { name: 'Project A' })).not.toBeInTheDocument()
  const oldSignal = resolve.mock.calls[1][1]
  await user.selectOptions(screen.getByLabelText('Switch project'), 'a')
  expect(oldSignal.aborted).toBe(true)
  await act(async () => finishNew(base))
  expect(await screen.findByRole('heading', { name: 'Project A' })).toBeVisible()
  await act(async () => finishOld({ ...base, projects: [{ id: 'b', name: 'Stale B' }] }))
  expect(screen.queryByText('Stale B')).not.toBeInTheDocument()
  expect(screen.getByRole('heading', { name: 'Project A' })).toBeVisible()
})
it('does not fall back when a selected project has been revoked', async () => {
  resolve.mockResolvedValueOnce(base).mockResolvedValueOnce({ ...base, projects: [base.projects[0]] }).mockResolvedValue(base)
  const user = await enter()
  await user.selectOptions(screen.getByLabelText('Switch project'), 'b')
  expect(await screen.findByRole('alert')).toHaveTextContent('no longer available')
  expect(screen.queryByLabelText('What would you like to investigate?')).not.toBeInTheDocument()
  expect(screen.queryByRole('heading', { name: 'Project B' })).not.toBeInTheDocument()
  await user.click(screen.getByRole('button', { name: 'Use authorized project' }))
  expect(await screen.findByRole('heading', { name: 'Project A' })).toBeVisible()
})
it.each([
  new AuthorizationError('This access token was not accepted or has expired.'),
  new AuthorizationError('Service unavailable. Please retry.'),
])('locks and removes all project context when validation fails: %s', async error => {
  resolve.mockResolvedValueOnce(base).mockRejectedValueOnce(error)
  const user = await enter()
  await user.selectOptions(screen.getByLabelText('Switch project'), 'b')
  expect(await screen.findByLabelText('Access token')).toBeVisible()
  expect(screen.queryByText('NEXUS Technologies')).not.toBeInTheDocument()
  expect(screen.queryByRole('combobox')).not.toBeInTheDocument()
  expect(screen.queryByLabelText('What would you like to investigate?')).not.toBeInTheDocument()
})
it('changing token while switching aborts and ignores the old identity response', async () => {
  let finish!: (value: AuthorizationContext) => void
  resolve.mockResolvedValueOnce(base).mockImplementationOnce(() => new Promise(done => { finish = done }))
  const user = await enter()
  await user.selectOptions(screen.getByLabelText('Switch project'), 'b')
  const signal = resolve.mock.calls[1][1]
  await user.click(screen.getByRole('button', { name: 'Change token' }))
  expect(signal.aborted).toBe(true)
  await act(async () => finish(base))
  expect(screen.queryByText('NEXUS Technologies')).not.toBeInTheDocument()
  expect(screen.getByLabelText('Access token')).toHaveValue('')
})
it('aborts project request scopes and replaces them on every project or identity change', async () => {
  resolve.mockResolvedValue(base)
  const { result } = renderHook(useAuthorization)
  await act(async () => { await result.current.authorize('opaque') })
  const previous = result.current.scope!
  expect(previous.projectId).toBe('a')
  await act(async () => { await result.current.selectProject('b') })
  expect(previous.signal.aborted).toBe(true)
  expect(result.current.scope!.projectId).toBe('b')
  const next = result.current.scope!
  act(() => result.current.reset())
  expect(next.signal.aborted).toBe(true)
  expect(result.current.scope).toBeNull()
  expect(result.current.context).toBeNull()
})
it('rejects a project ID outside the current server-authorized set without requesting it', async () => {
  resolve.mockResolvedValue(base)
  const { result } = renderHook(useAuthorization)
  await act(async () => { await result.current.authorize('opaque') })
  await act(async () => { await result.current.selectProject('unauthorized') })
  expect(resolve).toHaveBeenCalledTimes(1)
  expect(result.current.context).toBeNull()
  expect(result.current.scope).toBeNull()
})
it('rejects an identity change during project validation', async () => {
  resolve.mockResolvedValueOnce(base).mockResolvedValueOnce({ ...base, identity: { id: 'another', name: 'Another person' } })
  const user = await enter()
  await user.selectOptions(screen.getByLabelText('Switch project'), 'b')
  expect(await screen.findByRole('alert')).toHaveTextContent('Authorization changed')
  expect(screen.queryByText('Another person')).not.toBeInTheDocument()
})
