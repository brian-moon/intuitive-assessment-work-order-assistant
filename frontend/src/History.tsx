import { useEffect, useMemo, useState } from 'react'
import { api } from './api'
import type { AssetRow, Reading, Session } from './types'

interface Rule { unit: string; direction: 'high' | 'low'; warning: number; critical: number }
interface Hist {
  start: string; end: string; asset_id: string | null; truncated: boolean
  summary: { asset_id: string; readings: number; warning: number; critical: number; first: string; last: string }[]
  alerts: { id: number; severity: string; asset_id: string; subject: string; created_at: string; acknowledged_at: string | null }[]
  runs: { run_id: string; asset_id: string; status: string; route: string | null; priority: string | null; started_by: string; created_at: string }[]
  work_orders: { work_order: string; asset_id: string; priority: string; title: string; cmms_status: string; created_at: string }[]
  readings?: Reading[]; thresholds?: Record<string, Rule>; asset?: { name: string; criticality: string }
}

const RANGES: Record<string, number> = { '1h': 1, '6h': 6, '24h': 24, '7d': 168, '30d': 720 }
const fmt = (s: string) => new Date(s).toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })
const toLocalInput = (d: Date) => new Date(d.getTime() - d.getTimezoneOffset() * 60000).toISOString().slice(0, 16)

/** The window from the URL: ?range=24h, or ?start=...&end=... (ISO). */
function windowOf(q: URLSearchParams): { start: Date; end: Date; range: string } {
  const range = q.get('range') ?? (q.get('start') ? 'custom' : '24h')
  if (range !== 'custom' && RANGES[range]) {
    const end = new Date()
    return { start: new Date(end.getTime() - RANGES[range] * 3600_000), end, range }
  }
  const start = new Date(q.get('start') ?? Date.now() - 86400_000), end = new Date(q.get('end') ?? Date.now())
  return { start, end, range: 'custom' }
}

function Chart({ metric, rule, points, start, end }: { metric: string; rule?: Rule; points: Reading[]; start: Date; end: Date }) {
  const W = 640, H = 170, L = 44, R = 10, T = 12, B = 22
  const vals = points.map((p) => p.value).concat(rule ? [rule.warning, rule.critical] : [])
  let lo = Math.min(...vals), hi = Math.max(...vals)
  if (lo === hi) { lo -= 1; hi += 1 }
  const pad = (hi - lo) * 0.08; lo -= pad; hi += pad
  const t0 = start.getTime(), t1 = end.getTime()
  const x = (t: string) => L + ((new Date(t).getTime() - t0) / (t1 - t0)) * (W - L - R)
  const y = (v: number) => T + (1 - (v - lo) / (hi - lo)) * (H - T - B)
  const path = points.map((p, i) => `${i ? 'L' : 'M'}${x(p.ts).toFixed(1)},${y(p.value).toFixed(1)}`).join(' ')
  const colour = (s: string) => (s === 'critical' ? 'var(--critical)' : s === 'warning' ? 'var(--warning)' : 'var(--normal)')
  return (
    <figure style={{ margin: 0 }}>
      <figcaption className="head"><strong>{metric}</strong>
        {rule && <span className="muted">warning {rule.warning} · critical {rule.critical} {rule.unit} ({rule.direction === 'high' ? 'above' : 'below'})</span>}</figcaption>
      <svg className="chart" viewBox={`0 0 ${W} ${H}`} role="img" aria-label={`${metric} over time`}>
        <line className="axis" x1={L} y1={H - B} x2={W - R} y2={H - B} />
        <line className="axis" x1={L} y1={T} x2={L} y2={H - B} />
        <text x={L - 4} y={T + 8} textAnchor="end">{hi.toFixed(1)}</text>
        <text x={L - 4} y={H - B} textAnchor="end">{lo.toFixed(1)}</text>
        <text x={L} y={H - 6}>{fmt(start.toISOString())}</text>
        <text x={W - R} y={H - 6} textAnchor="end">{fmt(end.toISOString())}</text>
        {rule && <><line className="warn" x1={L} x2={W - R} y1={y(rule.warning)} y2={y(rule.warning)} />
          <line className="crit" x1={L} x2={W - R} y1={y(rule.critical)} y2={y(rule.critical)} /></>}
        {points.length > 1 && <path className="line" d={path} />}
        {points.map((p) => <circle key={p.id} cx={x(p.ts)} cy={y(p.value)} r={3.2} fill={colour(p.severity)}>
          <title>{`${fmt(p.ts)}  ${p.value} ${p.unit}  ${p.severity}`}</title></circle>)}
      </svg>
    </figure>)
}

export default function History({ session, assets, query, onQuery, onLive, onOpen }: {
  session: Session; assets: AssetRow[]; query: URLSearchParams; onQuery: (q: string) => void; onLive: () => void
  onOpen: (assetId: string, runId?: string) => void
}) {
  const asset = query.get('asset') ?? ''
  const win = useMemo(() => windowOf(query), [query])
  const [data, setData] = useState<Hist | null>(null)
  const [error, setError] = useState('')
  const [custom, setCustom] = useState({ start: toLocalInput(win.start), end: toLocalInput(win.end) })

  useEffect(() => {
    setError('')
    const q = new URLSearchParams({ start: win.start.toISOString(), end: win.end.toISOString(), ...(asset ? { asset_id: asset } : {}) })
    api<Hist>(`/api/history?${q}`, session).then(setData).catch((e) => setError((e as Error).message))
  }, [win, asset, session])

  const set = (patch: Record<string, string | null>) => {
    const q = new URLSearchParams(query)
    for (const [k, v] of Object.entries(patch)) { if (v) q.set(k, v); else q.delete(k) }
    onQuery(q.toString())
  }
  const byMetric = useMemo(() => {
    const m: Record<string, Reading[]> = {}
    for (const r of data?.readings ?? []) (m[r.metric] ??= []).push(r)
    return m
  }, [data])

  return (
    <>
      <div className="pagehead">
        <h1>Plant history</h1>
        <div className="segmented" role="tablist">
          <button role="tab" aria-selected="false" onClick={onLive} data-testid="mode-live">Live</button>
          <button className="on" role="tab" aria-selected="true">History</button>
        </div>
      </div>
      <div className="historybar" data-testid="historybar">
        <span className="pill history">Past data</span>
        <label className="inline toggle">Asset
          <select value={asset} onChange={(e) => set({ asset: e.target.value || null })} data-testid="history-asset">
            <option value="">All assets</option>{assets.map((a) => <option key={a.asset_id} value={a.asset_id}>{a.asset_id} {a.name}</option>)}
          </select></label>
        <div className="segmented">{[...Object.keys(RANGES), 'custom'].map((r) => (
          <button key={r} className={win.range === r ? 'on' : ''} data-testid={`range-${r}`}
            onClick={() => (r === 'custom' ? set({ range: 'custom', start: win.start.toISOString(), end: win.end.toISOString() })
                                             : set({ range: r, start: null, end: null }))}>{r}</button>))}</div>
        {win.range === 'custom' && <>
          <input type="datetime-local" value={custom.start} onChange={(e) => setCustom({ ...custom, start: e.target.value })} />
          <span>to</span>
          <input type="datetime-local" value={custom.end} onChange={(e) => setCustom({ ...custom, end: e.target.value })} />
          <button className="small" onClick={() => set({ range: 'custom', start: new Date(custom.start).toISOString(), end: new Date(custom.end).toISOString() })}>Show</button>
        </>}
        <span className="muted">{fmt(win.start.toISOString())} – {fmt(win.end.toISOString())}</span>
      </div>
      {error && <p className="error">{error}</p>}
      {!data ? <p className="muted">Loading…</p> : (
        <div className="grid">
          {!asset ? (
            <section className="card wide accent"><h2>Assets in this window</h2>
              {data.summary.length === 0 ? <p className="muted">No readings in this window.</p> : (
                <div className="scroll"><table data-testid="history-summary"><thead><tr><th>Asset</th><th>Readings</th><th>Warning</th><th>Critical</th><th>First</th><th>Last</th><th /></tr></thead>
                  <tbody>{data.summary.map((s) => (
                    <tr key={s.asset_id}><td><strong>{s.asset_id}</strong></td><td>{s.readings}</td>
                      <td>{s.warning ? <span className="chip warning">{s.warning}</span> : 0}</td>
                      <td>{s.critical ? <span className="chip critical">{s.critical}</span> : 0}</td>
                      <td className="muted">{fmt(s.first)}</td><td className="muted">{fmt(s.last)}</td>
                      <td><button className="small ghost" onClick={() => set({ asset: s.asset_id })}>Open</button></td></tr>))}</tbody></table></div>)}
            </section>
          ) : (
            <section className="card wide accent" data-testid="history-charts">
              <div className="head"><h2>{asset} {data.asset?.name}</h2>
                <span className="muted">{data.readings?.length ?? 0} readings{data.truncated ? ' (first 5,000 shown)' : ''}</span></div>
              {Object.keys(byMetric).length === 0 ? <p className="muted">No readings for {asset} in this window.</p> :
                <div className="grid">{Object.entries(byMetric).map(([m, pts]) => (
                  <Chart key={m} metric={m} rule={data.thresholds?.[m]} points={pts} start={win.start} end={win.end} />))}</div>}
            </section>)}
          <section className="card"><h2>Alerts in this window</h2>
            {data.alerts.length === 0 ? <p className="muted">None.</p> : (
              <ul className="alerts">{data.alerts.map((a) => (
                <li key={a.id}><span className={`chip ${a.severity}`}>{a.severity}</span>{a.subject}
                  <span className="muted">· {fmt(a.created_at)}{a.acknowledged_at ? ' · acknowledged' : ' · open'}</span></li>))}</ul>)}
          </section>
          <section className="card"><h2>Runs and work orders in this window</h2>
            {data.runs.length === 0 && data.work_orders.length === 0 ? <p className="muted">None.</p> : (<>
              <ul className="plain">{data.runs.map((r) => (
                <li key={r.run_id}><button className="link" onClick={() => onOpen(r.asset_id, r.run_id)}>{r.asset_id} run {r.run_id}</button>
                  {' '}<span className="chip">{r.status.replace(/_/g, ' ')}</span>{r.route && <span className="chip rule">{r.route}</span>}
                  <span className="muted">{fmt(r.created_at)} by {r.started_by}</span></li>))}</ul>
              <ul className="plain">{data.work_orders.map((w) => (
                <li key={w.work_order}><strong>{w.work_order}</strong> {w.title} <span className="chip">{w.priority}</span>
                  <span className="muted">{fmt(w.created_at)}</span></li>))}</ul></>)}
          </section>
        </div>)}
    </>
  )
}
