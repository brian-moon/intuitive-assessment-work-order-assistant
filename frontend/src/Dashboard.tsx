import { useEffect, useState } from 'react'
import { api } from './api'
import type { Live } from './App'
import type { Dashboard as Data, Session, SimState } from './types'

const fmt = (s: string | null) => (s ? new Date(s).toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }) : '—')
const clock = (s: string | null) => (s ? new Date(s).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' }) : '—')
const ago = (s: string | null, now: number) => {
  if (!s) return 'never'
  const d = Math.max(0, Math.round((now - new Date(s).getTime()) / 1000))
  return d < 60 ? `${d}s ago` : d < 3600 ? `${Math.round(d / 60)} min ago` : `${Math.round(d / 3600)} h ago`
}

function SimulatorControls({ sim, session, onChange }: { sim: SimState; session: Session; onChange: (s: SimState) => void }) {
  const [rate, setRate] = useState(String(sim.rate))
  const [unit, setUnit] = useState(sim.unit)
  const [err, setErr] = useState('')
  useEffect(() => { setRate(String(sim.rate)); setUnit(sim.unit) }, [sim.rate, sim.unit])
  const canAct = session.role !== 'viewer'
  const put = async (body: Partial<SimState>) => {
    setErr('')
    try { onChange(await api<SimState>('/api/simulator', session, { method: 'PUT', body: JSON.stringify(body) })) }
    catch (e) { setErr((e as Error).message) }
  }
  const emit = async () => { try { await api('/api/simulator/emit', session, { method: 'POST' }) } catch (e) { setErr((e as Error).message) } }
  return (
    <div className="sim" data-testid="simulator">
      <span>Telemetry simulator</span>
      <button className={`switch ${sim.enabled ? 'on' : ''}`} disabled={!canAct} aria-pressed={sim.enabled} title={sim.enabled ? 'Running: click to stop' : 'Stopped: click to start'}
        onClick={() => put({ enabled: !sim.enabled })} data-testid="sim-switch" />
      <input type="number" min="0.1" step="any" value={rate} disabled={!canAct} onChange={(e) => setRate(e.target.value)} aria-label="events" data-testid="sim-rate" />
      <span>events per</span>
      <select value={unit} disabled={!canAct} onChange={(e) => setUnit(e.target.value as SimState['unit'])} data-testid="sim-unit">
        <option value="minute">minute</option><option value="hour">hour</option></select>
      <button className="small" disabled={!canAct || (Number(rate) === sim.rate && unit === sim.unit)} onClick={() => put({ rate: Number(rate), unit })}>Apply</button>
      <button className="small ghost" disabled={!canAct} onClick={emit} title="One event now">+1 event</button>
      <span className="muted" style={{ color: '#a9b1d6' }}>{sim.events_last_hour} in the last hour</span>
      {err && <span className="error">{err}</span>}
    </div>)
}

/** Shown while nobody receives critical-alert e-mail: an admin registers an address on the spot. */
function RecipientPrompt({ data, session, onDone }: { data: Data; session: Session; onDone: () => void }) {
  const [name, setName] = useState('Plant admin')
  const [email, setEmail] = useState('')
  const [msg, setMsg] = useState('')
  const [busy, setBusy] = useState(false)
  const inbox = data.mail_inbox_url
  if (session.role !== 'admin') return (
    <section className="card prompt" data-testid="recipient-prompt">
      <strong>No one receives critical-alert e-mail yet.</strong> <span className="muted">An admin can register an address here or on the Admin screen.</span>
    </section>)
  async function register(e: React.FormEvent) {
    e.preventDefault(); setBusy(true); setMsg('')
    try {
      await api('/api/admin/recipients', session, { method: 'POST', body: JSON.stringify({ name, email, min_severity: 'critical' }) })
      try { await api('/api/scan', session, { method: 'POST' }) } catch { /* a scan ran in the last 10 s: new alerts still arrive */ }
      onDone()
    } catch (err) { setMsg((err as Error).message) } finally { setBusy(false) }
  }
  return (
    <section className="card prompt" data-testid="recipient-prompt">
      <form onSubmit={register} className="row" style={{ marginTop: 0 }}>
        <div style={{ flex: '1 1 260px' }}><strong>Where should critical alerts go?</strong>
          <div className="muted">{inbox ? <>E-mail goes to the built-in test inbox (<a href={inbox} target="_blank" rel="noreferrer">{inbox}</a>) until a mail server is set in .env.</>
            : 'Alerts are e-mailed through the configured mail server.'} The current critical alerts are sent right away.</div></div>
        <label className="inline">Name<input value={name} onChange={(e) => setName(e.target.value)} data-testid="prompt-name" /></label>
        <label className="inline" style={{ width: 240 }}>E-mail<input type="email" required value={email} onChange={(e) => setEmail(e.target.value)} placeholder="you@company.com" data-testid="prompt-email" /></label>
        <button disabled={busy || !email || !name} data-testid="prompt-register">{busy ? 'Registering…' : 'Register'}</button>
      </form>
      {msg && <p className="error">{msg}</p>}
    </section>)
}

export default function Dashboard({ data, session, live, onRefresh, onOpen, onHistory }: {
  data: Data | null; session: Session; live: Live; onRefresh: () => void; onOpen: (assetId: string, runId?: string) => void
  onHistory: (query?: string) => void
}) {
  const [sim, setSim] = useState<SimState | null>(null)
  const [tick, setTick] = useState(Date.now())
  useEffect(() => { const t = setInterval(() => setTick(Date.now()), 1000); return () => clearInterval(t) }, [])
  useEffect(() => { if (live.sim) setSim(live.sim) }, [live.sim])
  useEffect(() => { if (data?.simulator && !sim) setSim(data.simulator) }, [data, sim])
  if (!data) return <p className="muted">Loading…</p>
  const canAct = session.role !== 'viewer'
  const ack = async (id: number) => { await api(`/api/alerts/${id}/ack`, session, { method: 'POST' }); onRefresh() }
  const count = (s: string) => data.assets.filter((a) => a.severity === s).length
  const serverNow = live.now ? new Date(live.now).getTime() + (tick - Date.now()) : tick

  return (
    <>
      <div className="pagehead">
        <h1>Plant overview</h1>
        <div className="segmented" role="tablist">
          <button className="on" role="tab" aria-selected="true">Live</button>
          <button role="tab" aria-selected="false" onClick={() => onHistory()} data-testid="mode-history">History</button>
        </div>
      </div>

      <div className="livebar" data-testid="livebar">
        <span className="pill live">{live.connected ? 'Live' : 'Reconnecting'}</span>
        <span className="clock" title="Server time">{clock(new Date(serverNow).toISOString())}</span>
        <span>last reading {ago(live.feed[0]?.ts ?? data.assets.reduce<string | null>((m, a) => (a.last_reading_at && (!m || a.last_reading_at > m) ? a.last_reading_at : m), null), serverNow)}</span>
        {sim && <SimulatorControls sim={sim} session={session} onChange={setSim} />}
      </div>

      {data.alert_recipients === 0 && <RecipientPrompt data={data} session={session} onDone={onRefresh} />}

      <div className="kpis" style={{ marginBottom: 16 }}>
        <div className={count('critical') ? 'critical' : ''} data-testid="kpi-critical"><span className="kpi">{count('critical')}</span><span className="muted">assets critical</span></div>
        <div className={count('warning') ? 'warning' : ''}><span className="kpi">{count('warning')}</span><span className="muted">assets in warning</span></div>
        <div className={data.alerts.length ? 'critical' : ''}><span className="kpi">{data.alerts.length}</span><span className="muted">open alerts</span></div>
        <div><span className="kpi">{data.pending_approvals.length}</span><span className="muted">drafts waiting for a planner</span></div>
        <div><span className="kpi">{data.work_orders.length}</span><span className="muted">recent work orders</span></div>
      </div>

      <div className="grid three">
        <section>
          <div className="assets" data-testid="assets">
            {data.assets.map((a) => (
              <div key={a.asset_id} className={`asset ${a.severity}`} data-testid={`asset-${a.asset_id}`}>
                <div className="title"><strong>{a.asset_id}</strong><span className={`chip ${a.severity}`}>{a.severity}</span></div>
                <div>{a.name}</div>
                <div className="muted">{a.category} · {a.site} · {a.line} · {a.criticality} criticality</div>
                {a.findings.map((f) => <div key={f.metric} className={`finding ${f.severity}`}>
                  <span className={`chip ${f.severity}`}>{f.metric}</span>{f.value} {f.unit} <span className="muted">limit {f.threshold}</span></div>)}
                {a.stale && <div><span className="chip warning">stale data</span></div>}
                {a.rejected > 0 && <div><span className="chip warning">{a.rejected} reading rejected</span></div>}
                <div className="foot">
                  <span className="muted">{fmt(a.last_reading_at)}</span>
                  <span>
                    <button className="small ghost" onClick={() => onHistory(`?asset=${a.asset_id}&range=24h`)}>History</button>
                    {canAct && <button className="small" onClick={() => onOpen(a.asset_id)} data-testid={`open-${a.asset_id}`}>Run</button>}
                  </span>
                </div>
              </div>))}
          </div>
          {data.rejected_rows.length > 0 && <p className="muted">{data.rejected_rows.length} source row(s) rejected by validation: {data.rejected_rows.map((r) => `${r.asset_id} (${r.reason})`).join('; ')}</p>}
        </section>

        <section className="card accent">
          <div className="head"><h2>Live readings</h2><span className="muted">newest first</span></div>
          {live.feed.length === 0 ? <p className="muted">Waiting for readings… switch the simulator on, or press +1 event.</p> : (
            <ul className="feed" data-testid="feed">{live.feed.map((r) => (
              <li key={r.id} className={`${r.fresh ? 'fresh' : ''} ${r.severity === 'critical' ? 'critical' : ''}`}>
                <span className="t">{clock(r.ts)}</span><strong>{r.asset_id}</strong>
                <span>{r.metric} <strong>{r.value}</strong> {r.unit}</span><span className={`chip ${r.severity}`}>{r.severity}</span>
              </li>))}</ul>)}
        </section>

        <section className="card">
          <div className="head"><h2>Open alerts</h2><span className="muted">e-mail: {data.mail_inbox_url
            ? <a href={data.mail_inbox_url} target="_blank" rel="noreferrer" data-testid="inbox-link">test inbox</a> : data.email}
            {' '}· {data.alert_recipients} recipient{data.alert_recipients === 1 ? '' : 's'}</span></div>
          {data.alerts.length === 0 ? <p className="muted">No open alerts.</p> : (
            <ul className="alerts">{data.alerts.map((a) => (
              <li key={a.id} data-testid="alert">
                <span className={`chip ${a.severity}`}>{a.severity}</span> {a.subject}
                <span className="muted">· {fmt(a.created_at)}</span>
                {canAct && <><button className="small" onClick={() => onOpen(a.asset_id)}>Draft work order</button>
                  <button className="small ghost" onClick={() => ack(a.id)}>Acknowledge</button></>}
              </li>))}</ul>)}
        </section>

        <section className="card">
          <h2>Waiting for a planner</h2>
          {data.pending_approvals.length === 0 ? <p className="muted">Nothing waiting.</p> : (
            <ul className="plain">{data.pending_approvals.map((p) => (
              <li key={p.run_id}><button className="link" onClick={() => onOpen(p.asset_id, p.run_id)} data-testid="pending">
                {p.asset_id} {p.priority ?? ''}</button> <span className="muted">by {p.started_by} · {fmt(p.created_at)}</span></li>))}</ul>)}
          <h2 style={{ marginTop: 16 }}>Recent work orders</h2>
          {data.work_orders.length === 0 ? <p className="muted">None yet.</p> : (
            <ul className="plain">{data.work_orders.map((w) => (
              <li key={w.work_order} data-testid="work-order"><strong>{w.work_order}</strong> {w.title}
                <span className={`chip ${w.cmms_status === 'exported' ? 'normal' : 'warning'}`}>{w.cmms_status}</span></li>))}</ul>)}
        </section>
      </div>
    </>
  )
}
