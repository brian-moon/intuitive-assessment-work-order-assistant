import { useState } from 'react'
import { api } from './api'
import Brand from './Brand'
import type { Branding, Session } from './types'

export default function Login({ brand, onSignedIn }: { brand: Branding | null; onSignedIn: (s: Session) => void }) {
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  async function submit(e: React.FormEvent) {
    e.preventDefault(); setBusy(true); setError('')
    try { onSignedIn(await api<Session>('/api/login', null, { method: 'POST', body: JSON.stringify({ username, password }) })) }
    catch (err) { setError((err as Error).message) }
    finally { setBusy(false) }
  }

  return (
    <div className="login">
      <form onSubmit={submit} className="card">
        <Brand b={brand} />
        <h1>Sign in</h1>
        <p className="muted">Turns an equipment alarm into a work order a planner can trust.</p>
        <label>User name<input value={username} onChange={(e) => setUsername(e.target.value)} autoComplete="username" data-testid="username" /></label>
        <label>Password<input type="password" value={password} onChange={(e) => setPassword(e.target.value)} autoComplete="current-password" data-testid="password" /></label>
        {error && <p className="error">{error}</p>}
        <button disabled={busy || !username || !password} data-testid="sign-in">{busy ? 'Signing in…' : 'Sign in'}</button>
      </form>
    </div>
  )
}
