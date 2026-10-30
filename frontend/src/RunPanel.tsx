import { useEffect, useState } from 'react'
import { api, stream } from './api'
import type { AssetRow, Draft, Issue, PhaseEvent, Session } from './types'

const PHASES = ['guard', 'load_asset', 'data_quality', 'detect_anomalies', 'alert', 'agent_draft', 'reconcile', 'route',
                'approval', 'generate', 'export'] as const
const LABEL: Record<string, string> = {
  guard: 'Safety check', load_asset: 'Asset and category rules', data_quality: 'Data quality', detect_anomalies: 'Anomalies',
  alert: 'Alerts', agent_draft: 'Agent drafts the work order', reconcile: 'Critic: reconciliation', route: 'Model routing',
  approval: 'Planner approval', generate: 'Work order', export: 'CMMS export',
}
const KIND: Record<string, [string, string]> = { agent_draft: ['llm', 'LLM'], reconcile: ['rule', 'critic · rule-based'], route: ['rule', 'policy'] }
const ICON: Record<string, string> = { running: '…', done: '✓', warning: '!', critical: '!', failed: '✕', skipped: '–', waiting: '⏸' }

interface RunView { run_id: string; asset_id: string; status: string; draft: Draft | null; issues: Issue[]; events: PhaseEvent[] }
interface Rule { id: string; title: string; codes: string[]; severity: 'blocking' | 'warning'; checks: string; parameter: string }
interface RuleCard { asset_id: string; category: string; rules: Rule[]
  policy: { confidence: { start: number; blocking_penalty: number; warning_penalty: number } } }

function ReconciliationCard({ card, issues, checked, route }: {
  card: RuleCard | null; issues: Issue[] | null; checked: string | null; route: PhaseEvent | undefined
}) {
  if (!card) return null
  const failed = new Set((issues ?? []).map((i) => i.code))
  const wrongAsset = failed.has('wrong_asset') || failed.has('unknown_asset')
  const status = (r: Rule) => {
    if (!issues) return 'idle'
    if (r.codes.some((c) => failed.has(c))) return r.severity === 'blocking' ? 'fail' : 'warn'
    return wrongAsset && r.id !== 'asset' ? 'skip' : 'pass'
  }
  const mark: Record<string, string> = { idle: '·', pass: '✓', fail: '✕', warn: '!', skip: '–' }
  const p = card.policy.confidence
  const b = (issues ?? []).filter((i) => i.severity === 'blocking').length
  const w = (issues ?? []).length - b
  const conf = Math.max(0, p.start - b * p.blocking_penalty - w * p.warning_penalty)
  const rd = route?.data as { threshold?: number | null; action?: string; tier?: string } | undefined
  return (
    <section className="card accent" data-testid="reconciliation-card">
      <div className="head"><h2>Reconciliation rules</h2><span className="chip rule">{card.category}</span></div>
      <p className="muted" style={{ marginTop: 0 }}>
        The draft is checked against the source records by code, not by a model. A failed blocking rule disables approval.
        {checked && <> Checked: the <strong>{checked}</strong> draft.</>}</p>
      <ul className="rules">{card.rules.map((r) => {
        const st = status(r)
        const msgs = (issues ?? []).filter((i) => r.codes.includes(i.code))
        return (
          <li key={r.id} className={st} data-testid={`rule-${r.id}`} data-status={st}>
            <span className="mark">{mark[st]}</span>
            <div><strong>{r.title}</strong> <span className={`chip ${r.severity === 'blocking' ? 'critical' : 'warning'}`}>{r.severity}</span>
              <div className="what">{r.checks}</div>
              <div className="param">{r.parameter}</div>
              {msgs.map((m) => <div key={m.message} className={`issue ${m.severity}`}>{m.message}</div>)}
            </div>
          </li>)
      })}</ul>
      {issues && (
        <div className="score" data-testid="confidence">
          <span>Confidence</span>
          <span className="big">{conf.toFixed(2)}</span>
          <span className="muted">= {p.start.toFixed(2)} − {p.blocking_penalty.toFixed(2)} × {b} blocking − {p.warning_penalty.toFixed(2)} × {w} warning</span>
          {rd?.threshold != null && <span className={`chip ${rd.action === 'accept' ? 'normal' : rd.action === 'escalate' ? 'warning' : 'critical'}`}>
            {rd.tier} threshold {rd.threshold.toFixed(2)}: {rd.action === 'accept' ? 'accepted' : rd.action === 'escalate' ? 'escalated to the next model' : 'handed to a planner'}</span>}
        </div>)}
    </section>)
}

export default function RunPanel({ session, assets, assetId: routeAsset, runId: routeRun, onRunId, onAsset, onChanged }: {
  session: Session; assets: AssetRow[]; assetId?: string; runId?: string
  onRunId: (assetId: string, runId: string) => void; onAsset: (assetId: string) => void; onChanged: () => void
}) {
  const assetId = routeAsset ?? assets[0]?.asset_id ?? ''
  const [request, setRequest] = useState('Investigate the alarm and draft a work order.')
  const [events, setEvents] = useState<PhaseEvent[]>([])
  const [status, setStatus] = useState('idle')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [priority, setPriority] = useState('')
  const [card, setCard] = useState<RuleCard | null>(null)
  const [shownRun, setShownRun] = useState<string | undefined>(undefined)
  const canAct = session.role !== 'viewer'

  // the URL names the run: load it (back/forward, a link, a page reload), unless it is the one streaming now
  useEffect(() => {
    if (!routeRun) { if (!busy) { setEvents([]); setStatus('idle'); setShownRun(undefined) } return }
    if (routeRun === shownRun) return
    setShownRun(routeRun)
    api<RunView>(`/api/runs/${routeRun}`, session).then((r) => { setEvents(r.events); setStatus(r.status) })
      .catch((e) => setError((e as Error).message))
  }, [routeRun, shownRun, busy, session])

  useEffect(() => {
    if (!assetId) return
    api<RuleCard>(`/api/reconciliation/rules?asset_id=${assetId}`, session).then(setCard).catch(() => setCard(null))
  }, [assetId, session])

  const onEvent = (e: PhaseEvent) => {
    if (e.phase === 'run') {
      setStatus(e.status)
      if (e.run_id) { setShownRun(e.run_id); onRunId(assetId, e.run_id) }
      return
    }
    setEvents((prev) => [...prev, e])
  }

  async function start() {
    setBusy(true); setError(''); setEvents([]); setShownRun(undefined); setStatus('running'); setPriority('')
    try { await stream('/api/runs/stream', session, { asset_id: assetId, request }, onEvent) }
    catch (e) { setError((e as Error).message); setStatus('error') }
    finally { setBusy(false); onChanged() }
  }

  async function decide(decision: 'approve' | 'reject') {
    if (!shownRun) return
    setBusy(true); setError(''); setStatus('running')
    const body = decision === 'approve' ? { decision, ...(priority ? { edited_priority: priority } : {}) }
                                        : { decision, reason: 'Rejected by planner in the UI' }
    try { await stream(`/api/runs/${shownRun}/decision/stream`, session, body, onEvent) }
    catch (e) { setError((e as Error).message) }
    finally { setBusy(false); onChanged() }
  }

  const latest = (p: string) => [...events].reverse().find((e) => e.phase === p)
  const waiting = [...events].reverse().find((e) => e.phase === 'approval' && e.status === 'waiting')
  const draft = waiting?.data?.draft as Draft | undefined
  const issues = (waiting?.data?.issues as Issue[] | undefined) ?? []
  const lastRecon = latest('reconcile')
  const checkedIssues = waiting ? issues : (lastRecon?.data?.issues as Issue[] | undefined) ?? null
  const lastRoute = latest('route')
  const checkedTier = (lastRoute?.data?.tier as string | undefined) ?? null
  const canApprove = status === 'awaiting_approval' && Boolean(waiting?.data?.can_approve)
  const document = (events.find((e) => e.phase === 'generate' && e.data?.document_md)?.data?.document_md as string | undefined)
  const toolSteps = events.filter((e) => e.phase === 'agent_draft' && e.data?.tool)
  const routeSteps = events.filter((e) => e.phase === 'route')
  const route = waiting?.data?.route as string | undefined
  const model = (events.find((e) => e.phase === 'agent_draft')?.detail ?? '').split(',')[0]

  return (
    <>
      <div className="pagehead"><h1>Work-order run{assetId ? ` · ${assetId}` : ''}</h1>
        <span className="muted">Only <span className="chip llm">LLM</span> phases call a model; every other phase is deterministic code.</span></div>
      <div className="grid run">
        <section className="card accent">
          <h2>Start a run</h2>
          <label>Asset<select value={assetId} onChange={(e) => onAsset(e.target.value)} data-testid="asset-select">
            {assets.map((a) => <option key={a.asset_id} value={a.asset_id}>{a.asset_id} {a.name} ({a.severity})</option>)}
          </select></label>
          <label>Request<textarea rows={2} value={request} onChange={(e) => setRequest(e.target.value)} data-testid="request" /></label>
          <button disabled={busy || !canAct || !assetId} onClick={start} data-testid="start-run">{busy ? 'Running…' : 'Run pipeline'}</button>
          {!canAct && <p className="muted">Viewers can watch runs but not start them.</p>}
          {error && <p className="error">{error}</p>}
          <p>Status: <strong data-testid="run-status">{status.replace(/_/g, ' ')}</strong>{shownRun && <span className="muted"> · run {shownRun}</span>}</p>
        </section>

        <section className="card">
          <h2>Pipeline</h2>
          <ol className="phases" data-testid="phases">
            {PHASES.map((p) => {
              const e = latest(p)
              const k = KIND[p]
              return (
                <li key={p} className={`phase ${e?.status ?? 'pending'}`} data-testid={`phase-${p}`}>
                  <span className="icon">{e ? ICON[e.status] ?? '•' : '○'}</span>
                  <div><div className="name"><strong>{LABEL[p]}</strong>
                    {k && <span className={`chip ${k[0]}`}>{k[1]}{p === 'agent_draft' && model ? `: ${model.replace(/ model /, ' ')}` : ''}</span>}</div>
                    <div className="muted">{e?.detail ?? ''}</div>
                    {p === 'agent_draft' && toolSteps.length > 0 && (
                      <ul className="tools">{toolSteps.map((t, i) => <li key={i} className={t.data?.is_error ? 'bad' : ''}>{String(t.data?.tier ?? '')} {String(t.data?.tool)}{t.data?.is_error ? ' (error)' : ''}</li>)}</ul>)}
                    {p === 'route' && routeSteps.length > 1 && (
                      <ul className="tools">{routeSteps.map((t, i) => <li key={i}>{t.detail}</li>)}</ul>)}
                  </div>
                </li>)
            })}
          </ol>
        </section>

        <section className="card" data-testid={draft ? 'draft' : 'no-draft'}>
          {!draft ? <><h2>Draft</h2><p className="muted">The agent's draft appears here, with the rules it was checked against beside it.</p></> : (<>
            <div className="head"><h2>Draft for {draft.asset_id}</h2>
              <span>{route && <span className={`chip ${route === 'human' ? 'critical' : 'normal'}`} data-testid="route">{route === 'human' ? 'planner review' : `${route} model`}</span>}
                <span className={`chip ${draft.priority === 'P1' ? 'critical' : 'warning'}`} data-testid="priority">{draft.priority}</span></span></div>
            {route === 'human' && <div className="issue blocking">No model reached its confidence threshold: check every field before approving.</div>}
            <p>{draft.problem_summary}</p>
            <div className="scroll"><table><thead><tr><th>Evidence</th><th>Value</th><th>Recorded</th></tr></thead>
              <tbody>{draft.evidence.map((ev) => <tr key={ev.metric + ev.timestamp}><td>{ev.metric}</td><td>{ev.value} {ev.unit}</td><td className="muted">{ev.timestamp}</td></tr>)}</tbody></table></div>
            <h3>Actions</h3><ol>{draft.recommended_actions.map((a) => <li key={a}>{a}</li>)}</ol>
            <p><strong>Parts</strong> {draft.parts.length ? draft.parts.map((x) => `${x.part_no} × ${x.qty}`).join(', ') : 'none'}</p>
            <p><strong>Safety</strong> {draft.safety.lockout_tagout_required ? 'Lockout/tagout required' : 'No lockout listed'}: {draft.safety.energy_sources.join(', ')}. {draft.safety.notes}</p>
            {draft.needs_data_refresh && <p><span className="chip warning">confirm the data on site</span></p>}
            {canAct && status === 'awaiting_approval' && (
              <div className="row">
                <label className="inline">Priority<select value={priority} onChange={(e) => setPriority(e.target.value)}>
                  <option value="">keep {draft.priority}</option>{['P1', 'P2', 'P3', 'P4'].map((x) => <option key={x}>{x}</option>)}</select></label>
                <button disabled={busy || !canApprove} onClick={() => decide('approve')} data-testid="approve">Approve</button>
                <button className="ghost" disabled={busy} onClick={() => decide('reject')} data-testid="reject">Reject</button>
              </div>)}
          </>)}
        </section>

        <ReconciliationCard card={card} issues={checkedIssues} checked={checkedTier} route={lastRoute} />

        {document && (
          <section className="card wide" data-testid="work-order-doc"><h2>Work order</h2><pre className="doc">{document}</pre></section>
        )}
      </div>
    </>
  )
}
