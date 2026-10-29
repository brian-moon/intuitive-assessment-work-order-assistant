import { api } from './api'
import type { Dashboard as Data, Session } from './types'

const fmt = (s: string | null) => (s ? s.replace('T', ' ').slice(0, 16) + ' UTC' : '—')

export default function Dashboard({ data, session, onRefresh, onOpen }: {
  data: Data | null; session: Session; onRefresh: () => void; onOpen: (assetId: string, runId?: string) => void
}) {
  if (!data) return <p className="muted">Loading…</p>
  const canAct = session.role !== 'viewer'

  async function ack(id: number) { await api(`/api/alerts/${id}/ack`, session, { method: 'POST' }); onRefresh() }

  return (
    <div className="grid">
      <section className="card wide">
        <div className="head"><h2>Open alerts</h2><span className="muted">e-mail channel: {data.email}</span></div>
        {data.alerts.length === 0 ? <p className="muted">No open alerts.</p> : (
          <ul className="alerts">
            {data.alerts.map((a) => (
              <li key={a.id} className={a.severity} data-testid="alert">
                <span className="chip critical">{a.severity}</span> {a.subject}
                <span className="muted"> · {fmt(a.created_at)}</span>
                {canAct && <><button className="small" onClick={() => onOpen(a.asset_id)}>Draft work order</button>
                  <button className="small ghost" onClick={() => ack(a.id)}>Acknowledge</button></>}
              </li>))}
          </ul>)}
      </section>

      <section className="card wide">
        <h2>Assets</h2>
        <table>
          <thead><tr><th>Asset</th><th>Category</th><th>Severity</th><th>Findings</th><th>Last reading</th><th></th></tr></thead>
          <tbody>
            {data.assets.map((a) => (
              <tr key={a.asset_id} data-testid={`asset-${a.asset_id}`}>
                <td><strong>{a.asset_id}</strong> {a.name}<div className="muted">{a.site} · {a.line} · {a.criticality}</div></td>
                <td>{a.category}</td>
                <td><span className={`chip ${a.severity}`}>{a.severity}</span>
                  {a.stale && <span className="chip warning">stale</span>}
                  {a.rejected > 0 && <span className="chip warning">{a.rejected} rejected</span>}</td>
                <td>{a.findings.map((f) => <div key={f.metric}>{f.metric} {f.value} {f.unit} <span className="muted">(limit {f.threshold})</span></div>)}</td>
                <td className="muted">{fmt(a.last_reading_at)}</td>
                <td>{canAct && <button className="small" onClick={() => onOpen(a.asset_id)} data-testid={`open-${a.asset_id}`}>Run</button>}</td>
              </tr>))}
          </tbody>
        </table>
        {data.rejected_rows.length > 0 && <p className="muted">{data.rejected_rows.length} source row(s) rejected by validation: {data.rejected_rows.map((r) => `${r.asset_id} (${r.reason})`).join('; ')}</p>}
      </section>

      <section className="card">
        <h2>Pending approvals</h2>
        {data.pending_approvals.length === 0 ? <p className="muted">Nothing waiting.</p> : (
          <ul className="plain">{data.pending_approvals.map((p) => (
            <li key={p.run_id}><button className="link" onClick={() => onOpen(p.asset_id, p.run_id)} data-testid="pending">
              {p.asset_id} {p.priority ?? ''}</button> <span className="muted">by {p.started_by} · {fmt(p.created_at)}</span></li>))}</ul>)}
      </section>

      <section className="card">
        <h2>Work orders</h2>
        {data.work_orders.length === 0 ? <p className="muted">None yet.</p> : (
          <ul className="plain">{data.work_orders.map((w) => (
            <li key={w.work_order} data-testid="work-order"><strong>{w.work_order}</strong> {w.title}
              <span className={`chip ${w.cmms_status === 'exported' ? 'normal' : 'warning'}`}>{w.cmms_status}</span></li>))}</ul>)}
      </section>
    </div>
  )
}
