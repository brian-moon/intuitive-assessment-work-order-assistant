import { useCallback, useEffect, useRef, useState } from 'react'
import { api, ApiError, listen, loadSession, saveSession } from './api'
import { useRoute } from './router'
import type { Branding, Dashboard as DashboardData, LiveEvent, Reading, Session, SimState } from './types'
import Login from './Login'
import Dashboard from './Dashboard'
import History from './History'
import RunPanel from './RunPanel'
import Governance from './Governance'
import Admin from './Admin'
import Brand from './Brand'

export interface Live { connected: boolean; now: string | null; feed: (Reading & { fresh?: boolean })[]; sim: SimState | null }
interface Toast { id: number; title: string; detail: string }


export default function App() {
  const { route, navigate } = useRoute()
  const [session, setSession] = useState<Session | null>(loadSession())
  const [brand, setBrand] = useState<Branding | null>(null)
  const [data, setData] = useState<DashboardData | null>(null)
  const [error, setError] = useState('')
  const [live, setLive] = useState<Live>({ connected: false, now: null, feed: [], sim: null })
  const [toasts, setToasts] = useState<Toast[]>([])
  const [sound, setSound] = useState(() => { try { return localStorage.getItem('wo.sound') === '1' } catch { return false } })
  const soundRef = useRef(sound)
  soundRef.current = sound

  const signOut = useCallback(() => { saveSession(null); setSession(null); setData(null) }, [])

  const [demoModel, setDemoModel] = useState(false)
  useEffect(() => {
    api<Branding>('/api/branding', null).then(setBrand).catch(() => setBrand(null))
    api<{ demo_model: boolean }>('/health', null).then((h) => setDemoModel(h.demo_model)).catch(() => undefined)
  }, [])

  const refresh = useCallback(async () => {
    if (!session) return
    try { setData(await api<DashboardData>('/api/dashboard', session)); setError('') }
    catch (e) { if (e instanceof ApiError && e.status === 401) signOut(); else setError((e as Error).message) }
  }, [session, signOut])

  useEffect(() => { refresh(); const t = setInterval(refresh, 30000); return () => clearInterval(t) }, [refresh])

  // live events: readings, alerts and the simulator heartbeat; reconnects when the stream ends or drops
  useEffect(() => {
    if (!session) return
    const ctl = new AbortController()
    let refreshTimer: number | undefined
    const soon = () => { window.clearTimeout(refreshTimer); refreshTimer = window.setTimeout(refresh, 600) }
    const onEvent = (e: LiveEvent) => {
      if (e.type === 'reading') {
        setLive((l) => ({ ...l, feed: [{ ...e, fresh: !e.backlog }, ...l.feed.map((x) => ({ ...x, fresh: false }))].slice(0, 60) }))
        if (!e.backlog) soon()
      } else if (e.type === 'alert') {
        soon()
        if (e.severity === 'critical') {
          const id = e.id
          setToasts((t) => [...t, { id, title: 'Critical alert', detail: e.subject }].slice(-4))
          window.setTimeout(() => setToasts((t) => t.filter((x) => x.id !== id)), 9000)
          if (soundRef.current) beep()
        }
      } else if (e.type === 'heartbeat') {
        setLive((l) => ({ ...l, now: e.now, sim: e.simulator }))
      }
    }
    ;(async () => {
      while (!ctl.signal.aborted) {
        try {
          setLive((l) => ({ ...l, connected: true }))
          await listen<LiveEvent>('/api/live/stream', session, onEvent, ctl.signal)
        } catch (e) {
          if (ctl.signal.aborted) return
          if (e instanceof ApiError && e.status === 401) { signOut(); return }
          setLive((l) => ({ ...l, connected: false }))
          await new Promise((r) => setTimeout(r, 3000))
        }
      }
    })()
    return () => { ctl.abort(); window.clearTimeout(refreshTimer) }
  }, [session, refresh, signOut])

  useEffect(() => { if (session && route.parts.length === 0) navigate('/dashboard', { replace: true }) }, [session, route, navigate])

  const critical = data?.alerts.filter((a) => a.severity === 'critical') ?? []

  // the tab title flashes while critical alerts are open, so they are noticed from another tab too
  useEffect(() => {
    const base = brand?.name ?? 'Work-order assistant'
    if (!critical.length) { document.title = base; return }
    let on = false
    const t = setInterval(() => { on = !on; document.title = on ? `(${critical.length}) CRITICAL · ${base}` : base }, 1000)
    return () => { clearInterval(t); document.title = base }
  }, [critical.length, brand])

  if (!session) return <Login brand={brand} onSignedIn={(s) => { saveSession(s); setSession(s) }} />

  const [section = 'dashboard', sub, sub2] = route.parts
  const go = (to: string) => navigate(to)
  const openRun = (assetId: string, runId?: string) => go(`/run/${assetId}${runId ? `/${runId}` : ''}`)
  const ackAll = async () => { await api('/api/alerts/ack-all', session, { method: 'POST' }); refresh() }
  const toggleSound = () => { const v = !sound; setSound(v); try { localStorage.setItem('wo.sound', v ? '1' : '0') } catch { /* ignore */ } if (v) beep() }
  const tab = (name: string, label: string, testid?: string) => (
    <button className={section === name ? 'on' : ''} onClick={() => go(`/${name}`)} data-testid={testid}>{label}</button>)
  const isHistory = section === 'dashboard' && sub === 'history'

  return (
    <div className="shell">
      <header className="top">
        <Brand b={brand} />
        {isHistory ? <span className="pill history">History</span>
          : <span className="pill live" title={live.connected ? 'Receiving live events' : 'Reconnecting'}>{live.connected ? 'Live' : 'Offline'}</span>}
        {demoModel && <span className="pill demo" title="No model configured: the deterministic demo model drafts">Demo model</span>}
        <nav>
          {tab('dashboard', 'Dashboard')}
          {tab('run', 'Run')}
          {tab('governance', 'Governance', 'tab-governance')}
          {session.role === 'admin' && tab('admin', 'Admin', 'tab-admin')}
        </nav>
        <div className="who">
          <button className="iconbtn" onClick={toggleSound} title="Sound on critical alerts">{sound ? '🔔 on' : '🔕 off'}</button>
          {session.user} · {session.role} <button className="link" onClick={signOut}>Sign out</button>
        </div>
      </header>
      {critical.length > 0 && (
        <div className="banner" role="alert" data-testid="critical-banner">
          <span className="icon">⚠</span>
          <strong>{critical.length} critical alert{critical.length > 1 ? 's' : ''}</strong>
          <span>{critical[0].subject}{critical.length > 1 ? ` and ${critical.length - 1} more` : ''}</span>
          <button className="small" onClick={() => go('/dashboard')}>Review</button>
          {session.role !== 'viewer' && <button className="link light" onClick={ackAll}>Acknowledge all</button>}
        </div>
      )}
      {error && <div className="error" style={{ padding: '8px 22px' }}>{error}</div>}
      <main>
        {section === 'dashboard' && !isHistory && <Dashboard data={data} session={session} live={live} onRefresh={refresh}
          onOpen={openRun} onHistory={(q) => go(`/dashboard/history${q ?? ''}`)} />}
        {isHistory && <History session={session} assets={data?.assets ?? []} query={route.query}
          onQuery={(q) => navigate(`/dashboard/history?${q}`, { keepScroll: true })} onLive={() => go('/dashboard')} onOpen={openRun} />}
        {section === 'run' && <RunPanel session={session} assets={data?.assets ?? []} assetId={sub} runId={sub2}
          onRunId={(a, r) => navigate(`/run/${a}/${r}`, { replace: true })} onAsset={(a) => navigate(`/run/${a}`, { replace: true })}
          onChanged={refresh} />}
        {section === 'governance' && <Governance session={session} />}
        {section === 'admin' && session.role === 'admin' && <Admin session={session} />}
      </main>
      <div className="toasts">{toasts.map((t) => (
        <div key={t.id} className="toast" role="status">⚠ {t.title}<small>{t.detail}</small></div>))}</div>
    </div>
  )
}

function beep() {
  try {
    const Ctx = window.AudioContext || (window as unknown as { webkitAudioContext: typeof AudioContext }).webkitAudioContext
    const ctx = new Ctx()
    for (let i = 0; i < 2; i++) {
      const o = ctx.createOscillator(), g = ctx.createGain()
      o.frequency.value = 880; o.connect(g); g.connect(ctx.destination)
      g.gain.setValueAtTime(0.15, ctx.currentTime + i * 0.25)
      o.start(ctx.currentTime + i * 0.25); o.stop(ctx.currentTime + i * 0.25 + 0.15)
    }
  } catch { /* audio not available */ }
}
