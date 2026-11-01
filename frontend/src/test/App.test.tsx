import { act, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, expect, it, vi } from 'vitest'
import App from '../App'
import { AuthorizationError, resolveAuthorization } from '../auth/api'
import type { AuthorizationContext } from '../auth/models'
vi.mock('../auth/api', async original => ({ ...await original<typeof import('../auth/api')>(), resolveAuthorization: vi.fn() }))
const resolve = vi.mocked(resolveAuthorization)
const context: AuthorizationContext = { identity: { id: 'user', name: 'Test person' }, role: 'INTERN', company: { id: 'company', name: 'Test company' }, projects: [{ id: 'a', name: 'Project A' }], permissions: ['READ_GRAPH'], capabilities: { requestAccess: true, reviewAccessRequests: false } }
beforeEach(() => { resolve.mockReset() })
async function submit(token = 'opaque-token') {
  const user = userEvent.setup()
  await user.type(screen.getByLabelText('Access token'), token)
  await user.keyboard('{Enter}')
  return user
}
it('initially shows only the gate', () => {
  render(<App />)
  expect(screen.getByRole('heading', { name: 'NODUS' })).toBeVisible()
  expect(screen.queryByLabelText('Authorized workspace')).not.toBeInTheDocument()
  expect(screen.queryByRole('textbox')).not.toBeInTheDocument()
})
it('blocks an empty token', async () => {
  render(<App />)
  await userEvent.click(screen.getByRole('button', { name: 'Continue' }))
  expect(screen.getByRole('alert')).toHaveTextContent('Enter an access token')
  expect(resolve).not.toHaveBeenCalled()
})
it('shows an invalid token error', async () => {
  resolve.mockImplementation(async () => { throw new AuthorizationError('Token not accepted') })
  render(<App />); await submit()
  expect(await screen.findByRole('alert')).toHaveTextContent('Token not accepted')
})
it('recovers from a service failure by retrying', async () => {
  resolve.mockRejectedValueOnce(new AuthorizationError('Service unavailable. Please retry.')).mockResolvedValueOnce(context)
  render(<App />); const user = await submit()
  expect(await screen.findByRole('alert')).toHaveTextContent('retry')
  await user.click(screen.getByRole('button', { name: 'Continue' }))
  expect(await screen.findByText('Project A')).toBeVisible()
})
it('uses server context and does not infer admin from token 4; supports keyboard submission', async () => {
  resolve.mockResolvedValue(context)
  render(<App />); await submit('4')
  expect(await screen.findByText('INTERN')).toBeVisible()
  expect(screen.getByText('Test company')).toBeVisible()
  expect(screen.queryByText('ADMIN')).not.toBeInTheDocument()
  expect(screen.queryByLabelText('Access token')).not.toBeInTheDocument()
  expect(screen.getByRole('button', { name: 'Continue' })).toBeDisabled()
})
it('changing token removes context and objective state', async () => {
  resolve.mockResolvedValue(context)
  render(<App />); const user = await submit()
  await user.type(await screen.findByLabelText('What would you like to investigate?'), 'Private question')
  await user.click(screen.getByRole('button', { name: 'Change token' }))
  expect(screen.queryByText('Project A')).not.toBeInTheDocument()
  expect(screen.queryByDisplayValue('Private question')).not.toBeInTheDocument()
  expect(screen.getByLabelText('Access token')).toHaveValue('')
  await submit()
  expect(await screen.findByLabelText('What would you like to investigate?')).toHaveValue('')
})
it('aborts requests, prevents duplicates and ignores stale responses', async () => {
  let finish!: (value: AuthorizationContext) => void
  resolve.mockImplementationOnce(() => new Promise(done => { finish = done })).mockResolvedValueOnce(context)
  render(<App />); const user = await submit('old')
  expect(screen.getByRole('button', { name: 'Validating…' })).toBeDisabled()
  const signal = resolve.mock.calls[0][1]
  await user.click(screen.getByRole('button', { name: 'Cancel validation' }))
  expect(signal.aborted).toBe(true)
  await submit('new')
  expect(await screen.findByText('Project A')).toBeVisible()
  await act(async () => finish({ ...context, company: { id: 'old', name: 'Old secret' } }))
  expect(screen.queryByText('Old secret')).not.toBeInTheDocument()
  expect(resolve).toHaveBeenCalledTimes(2)
})



