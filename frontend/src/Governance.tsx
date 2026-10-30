import { useEffect, useState } from 'react'
import { api } from './api'
import type { Session } from './types'

interface Attempt { tier: string; model: string; outcome: string; confidence: number | null; threshold: number | null; action: string; reason: string }
interface Criterion { pass_rate: number | null; n: number; threshold: number }
interface Gov {
  ladder: { tier: string; model: string; provider: string; threshold: number; by_priority: Record<string, number> }[]
  demo_model: boolean
  policy: { confidence: { start: number; blocking_penalty: number; warning_penalty: number } }
  runs: { total: number; by_route: Record<string, number> }
  escalations: Record<string, number>
  guardrails: { refused_requests: number; blocking_issues: { code: string; n: number }[] }
  hitl: { approved: number; corrected: number; rejected: number; pending: number; median_wait_ms: number | null }
  alerts: Record<string, number>
  cost: { by_tier: Record<string, { calls: number; input_tokens: number; output_tokens: number; cost_usd: number }>
          frontier_cost_per_run_usd: number | null; estimated_frontier_cost_avoided_usd: number | null }
  evals: { file: string; model: string; trials: number; cases: number; generated_at: string; criteria: Record<string, Criterion>
           flaky_cases: string[]; cost_usd: { total: number }; routes?: { by_route: Record<string, number>; first_tier_escalation_rate: number | null } }[]
  telemetry: { enabled: boolean; otlp: boolean; url: string | null; producer: string; deployment: string; emitted: number
               sent: number; dropped: number; queued: number; last_error: string | null }
  recent: { run_id: string; asset_id: string; status: string; route: string | null; decision: string | null; priority: string | null; attempts: Attempt[] }[]
}

const TIER_LABEL: Record<string, string> = { local: 'Local model', frontier: 'Frontier model', demo: 'Demo model', model: 'Model', human: 'Planner' }
const usd = (v: number | null | undefined) => (v == null ? '—' : `$${v.toFixed(v < 0.01 ? 5 : 3)}`)

function Bars({ rows, tone }: { rows: [string, number][]; tone: string }) {
  const max = Math.max(1, ...rows.map(([, n]) => n))
  if (!rows.length) return <p className="muted">None yet.</p>
  return (
    <ul className="bars">{rows.map(([k, n]) => (
      <li key={k}><span className="bar-label">{k}</span>
        <span className="bar-track"><span className={`bar-fill ${tone}`} style={{ width: `${(n / max) * 100}%` }} /></span>
        <span className="bar-n">{n}</span></li>))}
    </ul>)
}

export default function Governance({ session }: { session: Session }) {
  const [g, setG] = useState<Gov | null>(null)
  const [error, setError] = useState('')
  useEffect(() => {
    const load = () => api<Gov>('/api/governance', session).then(setG).catch((e) => setError((e as Error).message))
    load(); const t = setInterval(load, 15000); return () => clearInterval(t)
  }, [session])
  if (error) return <p className="error">{error}</p>
  if (!g) return <p className="muted">Loading…</p>

  const r = g.runs.by_route
  const drafted = (r.local ?? 0) + (r.frontier ?? 0) + (r.demo ?? 0) + (r.model ?? 0) + (r.human ?? 0)
  const pct = (n: number | undefined) => (drafted ? `${Math.round(((n ?? 0) / drafted) * 100)}%` : '—')
  const hasLocal = g.ladder.some((t) => t.tier === 'local')
  const latestEval = g.evals.length ? g.evals.reduce((a, b) => (a.generated_at > b.generated_at ? a : b)) : null

  return (
    <div className="grid" data-testid="governance">
      <section className="card wide">
        <h2>Model ladder</h2>
        <div className="ladder" data-testid="ladder">
          {!hasLocal && !g.demo_model && <div className="step off"><strong>Local model</strong><span className="muted">not configured: the frontier model is the first tier</span></div>}
          {g.ladder.map((t) => (
            <div className="step" key={t.tier} data-testid={`tier-${t.tier}`}>
              <strong>{TIER_LABEL[t.tier] ?? t.tier}</strong>
              <span>{t.model} <span className="muted">({t.provider})</span></span>
              <span className="muted">accept at confidence ≥ {t.threshold.toFixed(2)}
                {Object.entries(t.by_priority).map(([p, v]) => `; ${p} needs ${v.toFixed(2)}`).join('')}</span>
            </div>))}
          <div className="step human"><strong>Planner</strong><span className="muted">below every threshold, or no usable draft; every work order is approved by a planner in any case</span></div>
        </div>
        <p className="muted">Confidence is computed by reconciliation against the source data, never taken from the model:
          {' '}{g.policy.confidence.start.toFixed(2)} minus {g.policy.confidence.blocking_penalty.toFixed(2)} per blocking issue and
          {' '}{g.policy.confidence.warning_penalty.toFixed(2)} per warning (data/routing.json).</p>
        {g.demo_model && <p className="chip warning">Demo model: no local model and no ANTHROPIC_API_KEY configured</p>}
      </section>

      <section className="card wide">
        <div className="kpis">
          <div><span className="kpi">{g.runs.total}</span><span className="muted">runs</span></div>
          {hasLocal && <div data-testid="kpi-local"><span className="kpi">{pct(r.local)}</span><span className="muted">accepted locally ({r.local ?? 0})</span></div>}
          {g.demo_model && <div data-testid="kpi-demo"><span className="kpi">{pct(r.demo)}</span><span className="muted">by the demo model ({r.demo ?? 0})</span></div>}
          <div data-testid="kpi-frontier"><span className="kpi">{pct(r.frontier)}</span><span className="muted">by the frontier model ({r.frontier ?? 0})</span></div>
          <div data-testid="kpi-human"><span className="kpi">{pct(r.human)}</span><span className="muted">handed to a planner ({r.human ?? 0})</span></div>
          <div data-testid="kpi-refused"><span className="kpi">{g.guardrails.refused_requests}</span><span className="muted">unsafe requests refused before any model call</span></div>
        </div>
      </section>

      <section className="card"><h2>Why drafts were escalated</h2>
        <Bars rows={Object.entries(g.escalations).sort((a, b) => b[1] - a[1])} tone="warning" /></section>
      <section className="card"><h2>Blocking issues caught by reconciliation</h2>
        <Bars rows={g.guardrails.blocking_issues.map((b) => [b.code.replace(/_/g, ' '), b.n] as [string, number])} tone="critical" /></section>

      <section className="card"><h2>Planner decisions (human in the loop)</h2>
        <table><tbody>
          <tr><td>Approved as drafted</td><td>{g.hitl.approved}</td></tr>
          <tr><td>Approved with a correction</td><td>{g.hitl.corrected}</td></tr>
          <tr><td>Rejected</td><td>{g.hitl.rejected}</td></tr>
          <tr><td>Waiting now</td><td>{g.hitl.pending}</td></tr>
          <tr><td>Median wait for a decision</td><td>{g.hitl.median_wait_ms == null ? '—' : `${Math.round(g.hitl.median_wait_ms / 1000)} s`}</td></tr>
        </tbody></table></section>
      <section className="card"><h2>Critical-anomaly alerts</h2>
        <table><tbody>
          <tr><td>Open on the dashboard</td><td>{g.alerts.open_on_dashboard ?? 0}</td></tr>
          <tr><td>E-mails sent</td><td>{g.alerts.sent ?? 0}</td></tr>
          <tr><td>E-mails failed (retried by the next scan)</td><td>{g.alerts.failed ?? 0}</td></tr>
          <tr><td>E-mail disabled (no SMTP configured)</td><td>{g.alerts.disabled ?? 0}</td></tr>
        </tbody></table></section>

      <section className="card"><h2>Model usage and cost</h2>
        <table><thead><tr><th>Tier</th><th>Calls</th><th>Tokens in / out</th><th>Cost</th></tr></thead>
          <tbody>{Object.entries(g.cost.by_tier).map(([t, c]) => (
            <tr key={t}><td>{TIER_LABEL[t] ?? t}</td><td>{c.calls}</td><td>{c.input_tokens} / {c.output_tokens}</td><td>{usd(c.cost_usd)}</td></tr>))}
          </tbody></table>
        {hasLocal && <p className="muted">Frontier cost per run {usd(g.cost.frontier_cost_per_run_usd)}; estimated frontier cost avoided by local drafts {usd(g.cost.estimated_frontier_cost_avoided_usd)}.</p>}
      </section>
      <section className="card"><h2>Telemetry</h2>
        <table><tbody>
          <tr><td>TrustPlane</td><td>{g.telemetry.enabled ? <span className="chip normal">connected</span> : <span className="chip">not configured</span>} {g.telemetry.url ?? ''}</td></tr>
          <tr><td>OTLP spans</td><td>{g.telemetry.otlp ? 'exported' : 'in memory only'}</td></tr>
          <tr><td>Governance events</td><td>{g.telemetry.emitted} emitted, {g.telemetry.sent} sent, {g.telemetry.dropped} dropped, {g.telemetry.queued} queued</td></tr>
          <tr><td>Producer / deployment</td><td>{g.telemetry.producer} / {g.telemetry.deployment}</td></tr>
          {g.telemetry.last_error && <tr><td>Last error</td><td className="error">{g.telemetry.last_error}</td></tr>}
        </tbody></table></section>

      <section className="card wide"><h2>Latest evaluation</h2>
        {!latestEval ? <p className="muted">No report in reports/ yet (python -m evals.run_eval).</p> : (<>
          <p className="muted">{latestEval.model}, {latestEval.trials} trial(s) × {latestEval.cases} cases, {latestEval.generated_at}, cost {usd(latestEval.cost_usd.total)}
            {latestEval.routes?.first_tier_escalation_rate != null && `, first-tier escalation rate ${Math.round(latestEval.routes.first_tier_escalation_rate * 100)}%`}
            {latestEval.flaky_cases.length > 0 && `, flaky: ${latestEval.flaky_cases.join(', ')}`}</p>
          <table data-testid="eval-table"><thead><tr><th>Criterion</th><th>Pass rate</th><th>Threshold</th><th /></tr></thead>
            <tbody>{Object.entries(latestEval.criteria).map(([c, v]) => {
              const ok = v.pass_rate == null || v.pass_rate >= v.threshold
              return <tr key={c}><td>{c.replace(/_/g, ' ')}</td><td>{v.pass_rate == null ? '—' : `${Math.round(v.pass_rate * 100)}%`}</td>
                <td>{Math.round(v.threshold * 100)}%</td><td><span className={`chip ${ok ? 'normal' : 'critical'}`}>{ok ? 'pass' : 'below'}</span></td></tr>
            })}</tbody></table></>)}
      </section>

      <section className="card wide"><h2>Recent routing decisions</h2>
        <table data-testid="recent-routes"><thead><tr><th>Run</th><th>Asset</th><th>Path</th><th>Outcome</th></tr></thead>
          <tbody>{g.recent.map((x) => (
            <tr key={x.run_id}><td className="muted">{x.run_id}</td><td>{x.asset_id}</td>
              <td>{x.attempts.length === 0 ? <span className="chip critical">refused by the guard</span> : x.attempts.map((a, i) => (
                <span key={i} className={`chip ${a.action === 'accept' ? 'normal' : a.action === 'escalate' ? 'warning' : 'critical'}`}
                  title={a.reason}>{a.tier} {a.confidence == null ? a.outcome.replace(/_/g, ' ') : a.confidence.toFixed(2)} {a.action === 'accept' ? '✓' : '→'}</span>))}
                {x.route === 'human' && <span className="chip critical">planner</span>}</td>
              <td>{x.status.replace(/_/g, ' ')}{x.decision ? ` · ${x.decision}` : ''}</td></tr>))}
          </tbody></table></section>
    </div>
  )
}
