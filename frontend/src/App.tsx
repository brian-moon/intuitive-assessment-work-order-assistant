import { useCallback, useEffect, useState } from 'react'
import { api, ApiError, loadSession, saveSession } from './api'
import type { Dashboard as DashboardData, Session } from './types'
import Login from './Login'
import Dashboard from './Dashboard'
import RunPanel from './RunPanel'
import Admin from './Admin'
import Governance from './Governance'

type Tab = 'dashboard' | 'run' | 'governance' | 'admin'

export default function App() {
  const [session, setSession] = useState<Session | null>(loadSession())
  const [tab, setTab] = useState<Tab>('dashboard')
  const [data, setData] = useState<DashboardData | null>(null)
  const [error, setError] = useState('')
  const [runTarget, setRunTarget] = useState<{ assetId: string; runId?: string } | null>(null)

  const signOut = useCallback(() => { saveSession(null); setSession(null); setData(null) }, [])

  const refresh = useCallback(async () => {
    if (!session) return
    try { setData(await api<DashboardData>('/api/dashboard', session)); setError('') }
    catch (e) { if (e instanceof ApiError && e.status === 401) signOut(); else setError((e as Error).message) }
  }, [session, signOut])

  useEffect(() => { refresh(); const t = setInterval(refresh, 15000); return () => clearInterval(t) }, [refresh])

  if (!session) return <Login onSignedIn={(s) => { saveSession(s); setSession(s) }} />

  const critical = data?.alerts.filter((a) => a.severity === 'critical') ?? []
  const open = (assetId: string, runId?: string) => { setRunTarget({ assetId, runId }); setTab('run') }

  return (
    <div className="shell">
      <header className="top">
        <div className="brand"><span className="mark">WO</span> Work-order assistant</div>
        <nav>
          <button className={tab === 'dashboard' ? 'on' : ''} onClick={() => setTab('dashboard')}>Dashboard</button>
          <button className={tab === 'run' ? 'on' : ''} onClick={() => setTab('run')}>Run</button>
          <button className={tab === 'governance' ? 'on' : ''} onClick={() => setTab('governance')} data-testid="tab-governance">Governance</button>
          {session.role === 'admin' && <button className={tab === 'admin' ? 'on' : ''} onClick={() => setTab('admin')}>Admin</button>}
        </nav>
        <div className="who">{session.user} · {session.role} <button className="link" onClick={signOut}>Sign out</button></div>
      </header>
      {critical.length > 0 && (
        <div className="banner" role="alert" data-testid="critical-banner">
          <strong>{critical.length} critical alert{critical.length > 1 ? 's' : ''}</strong>
          <span>{critical[0].subject}{critical.length > 1 ? ` and ${critical.length - 1} more` : ''}</span>
          <button className="link light" onClick={() => setTab('dashboard')}>Review</button>
        </div>
      )}
      {error && <div className="error">{error}</div>}
      <main>
        {tab === 'dashboard' && <Dashboard data={data} session={session} onRefresh={refresh} onOpen={open} />}
        {tab === 'run' && <RunPanel session={session} assets={data?.assets ?? []} target={runTarget} onChanged={refresh} />}
        {tab === 'governance' && <Governance session={session} />}
        {tab === 'admin' && session.role === 'admin' && <Admin session={session} />}
      </main>
    </div>
  )
}
