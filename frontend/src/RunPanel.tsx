import { useEffect, useState } from 'react'
import { api, stream } from './api'
import type { AssetRow, Draft, Issue, PhaseEvent, Session } from './types'

const PHASES = ['guard', 'load_asset', 'data_quality', 'detect_anomalies', 'alert', 'agent_draft', 'reconcile', 'approval',
                'generate', 'export'] as const
const LABEL: Record<string, string> = {
  guard: 'Safety check', load_asset: 'Asset and category rules', data_quality: 'Data quality', detect_anomalies: 'Anomalies',
  alert: 'Alerts', agent_draft: 'Agent draft', reconcile: 'Reconciliation', approval: 'Planner approval',
  generate: 'Work order', export: 'CMMS export',
}
const ICON: Record<string, string> = { running: '…', done: '✓', warning: '!', critical: '!', failed: '✕', skipped: '–', waiting: '⏸' }

interface RunView { run_id: string; asset_id: string; status: string; draft: Draft | null; issues: Issue[]; events: PhaseEvent[] }

export default function RunPanel({ session, assets, target, onChanged }: {
  session: Session; assets: AssetRow[]; target: { assetId: string; runId?: string } | null; onChanged: () => void
}) {
  const [assetId, setAssetId] = useState(target?.assetId ?? assets[0]?.asset_id ?? '')
  const [request, setRequest] = useState('Investigate the alarm and draft a work order.')
  const [events, setEvents] = useState<PhaseEvent[]>([])
  const [runId, setRunId] = useState<string | undefined>(target?.runId)
  const [status, setStatus] = useState('idle')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [priority, setPriority] = useState('')
  const canAct = session.role !== 'viewer'

  useEffect(() => {
    if (target?.assetId) setAssetId(target.assetId)
    if (target?.runId) {
      setRunId(target.runId)
      api<RunView>(`/api/runs/${target.runId}`, session).then((r) => { setEvents(r.events); setStatus(r.status) })
        .catch((e) => setError((e as Error).message))
    }
  }, [target, session])

  const onEvent = (e: PhaseEvent) => {
    if (e.phase === 'run') { setStatus(e.status); if (e.run_id) setRunId(e.run_id); return }
    setEvents((prev) => [...prev, e])
  }

  async function start() {
    setBusy(true); setError(''); setEvents([]); setRunId(undefined); setStatus('running')
    try { await stream('/api/runs/stream', session, { asset_id: assetId, request }, onEvent) }
    catch (e) { setError((e as Error).message); setStatus('error') }
    finally { setBusy(false); onChanged() }
  }

  async function decide(decision: 'approve' | 'reject') {
    if (!runId) return
    setBusy(true); setError(''); setStatus('running')
    const body = decision === 'approve' ? { decision, ...(priority ? { edited_priority: priority } : {}) }
                                        : { decision, reason: 'Rejected by planner in the UI' }
    try { await stream(`/api/runs/${runId}/decision/stream`, session, body, onEvent) }
    catch (e) { setError((e as Error).message) }
    finally { setBusy(false); onChanged() }
  }

  const latest = (p: string) => [...events].reverse().find((e) => e.phase === p)
  const waiting = [...events].reverse().find((e) => e.phase === 'approval' && e.status === 'waiting')
  const draft = waiting?.data?.draft as Draft | undefined
  const issues = (waiting?.data?.issues as Issue[] | undefined) ?? []
  const canApprove = status === 'awaiting_approval' && Boolean(waiting?.data?.can_approve)
  const document = (events.find((e) => e.phase === 'generate' && e.data?.document_md)?.data?.document_md as string | undefined)
  const toolSteps = events.filter((e) => e.phase === 'agent_draft' && e.data?.tool)

  return (
    <div className="grid run">
      <section className="card">
        <h2>Start a run</h2>
        <label>Asset<select value={assetId} onChange={(e) => setAssetId(e.target.value)} data-testid="asset-select">
          {assets.map((a) => <option key={a.asset_id} value={a.asset_id}>{a.asset_id} {a.name} ({a.severity})</option>)}
        </select></label>
        <label>Request<textarea rows={2} value={request} onChange={(e) => setRequest(e.target.value)} data-testid="request" /></label>
        <button disabled={busy || !canAct || !assetId} onClick={start} data-testid="start-run">{busy ? 'Running…' : 'Run pipeline'}</button>
        {!canAct && <p className="muted">Viewers can watch runs but not start them.</p>}
        {error && <p className="error">{error}</p>}
        <p>Status: <strong data-testid="run-status">{status.replace('_', ' ')}</strong>{runId && <span className="muted"> · run {runId}</span>}</p>
      </section>

      <section className="card">
        <h2>Pipeline</h2>
        <ol className="phases" data-testid="phases">
          {PHASES.map((p) => {
            const e = latest(p)
            return (
              <li key={p} className={`phase ${e?.status ?? 'pending'}`} data-testid={`phase-${p}`}>
                <span className="icon">{e ? ICON[e.status] ?? '•' : '○'}</span>
                <div><strong>{LABEL[p]}</strong><div className="muted">{e?.detail ?? ''}</div>
                  {p === 'agent_draft' && toolSteps.length > 0 && (
                    <ul className="tools">{toolSteps.map((t, i) => <li key={i} className={t.data?.is_error ? 'bad' : ''}>{String(t.data?.tool)}{t.data?.is_error ? ' (error)' : ''}</li>)}</ul>)}
                </div>
              </li>)
          })}
        </ol>
      </section>

      {draft && (
        <section className="card wide" data-testid="draft">
          <div className="head"><h2>Draft for {draft.asset_id}</h2><span className={`chip ${draft.priority === 'P1' ? 'critical' : 'warning'}`} data-testid="priority">{draft.priority}</span></div>
          <p>{draft.problem_summary}</p>
          <table><thead><tr><th>Evidence</th><th>Value</th><th>Recorded</th></tr></thead>
            <tbody>{draft.evidence.map((ev) => <tr key={ev.metric + ev.timestamp}><td>{ev.metric}</td><td>{ev.value} {ev.unit}</td><td className="muted">{ev.timestamp}</td></tr>)}</tbody></table>
          <h3>Actions</h3><ol>{draft.recommended_actions.map((a) => <li key={a}>{a}</li>)}</ol>
          <p><strong>Parts</strong> {draft.parts.length ? draft.parts.map((p) => `${p.part_no} × ${p.qty}`).join(', ') : 'none'}</p>
          <p><strong>Safety</strong> {draft.safety.lockout_tagout_required ? 'Lockout/tagout required' : 'No lockout listed'}: {draft.safety.energy_sources.join(', ')}. {draft.safety.notes}</p>
          {draft.needs_data_refresh && <p className="chip warning">confirm the data on site</p>}
          {issues.map((i) => <div key={i.code + i.message} className={`issue ${i.severity}`} data-testid="issue">{i.severity}: {i.message}</div>)}
          {canAct && status === 'awaiting_approval' && (
            <div className="row">
              <label className="inline">Priority<select value={priority} onChange={(e) => setPriority(e.target.value)}>
                <option value="">keep {draft.priority}</option>{['P1', 'P2', 'P3', 'P4'].map((p) => <option key={p}>{p}</option>)}</select></label>
              <button disabled={busy || !canApprove} onClick={() => decide('approve')} data-testid="approve">Approve</button>
              <button className="ghost" disabled={busy} onClick={() => decide('reject')} data-testid="reject">Reject</button>
            </div>)}
        </section>
      )}

      {document && (
        <section className="card wide" data-testid="work-order-doc"><h2>Work order</h2><pre className="doc">{document}</pre></section>
      )}
    </div>
  )
}
