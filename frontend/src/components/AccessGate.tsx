import { useState } from 'react'
interface Props { pending: boolean; error: string; onSubmit: (token: string) => void; onCancel: () => void }
export function AccessGate({ pending, error, onSubmit, onCancel }: Props) {
  const [token, setToken] = useState('')
  return <form className="gate" noValidate onSubmit={event => { event.preventDefault(); onSubmit(token) }} aria-busy={pending}>
    <label htmlFor="access-token">Access token</label>
    <input id="access-token" type="password" value={token} onChange={event => setToken(event.target.value)}
      autoFocus autoComplete="off" autoCapitalize="none" spellCheck={false} required disabled={pending}
      aria-invalid={!!error} aria-describedby={error ? 'access-error' : undefined} placeholder="Enter your access token" />
    {error && <p id="access-error" className="error" role="alert">{error}</p>}
    <button className="primary" type="submit" disabled={pending}>{pending ? 'Validating…' : 'Continue'}<span aria-hidden="true">→</span></button>
    {pending && <button className="text-button" type="button" onClick={() => { setToken(''); onCancel() }}>Cancel validation</button>}
  </form>
}
