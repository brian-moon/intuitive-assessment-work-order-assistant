import { useCallback, useEffect, useState } from 'react'
import { api } from './api'
import type { NotificationRow, Recipient, Session } from './types'

const FAULTS: Record<string, string> = {
  telemetry_unavailable: 'Historian down', parts_invalid_response: 'Stores: invalid response',
  cmms_timeout: 'CMMS timeout', smtp_unavailable: 'SMTP unavailable',
}

export default function Admin({ session }: { session: Session }) {
  const [recipients, setRecipients] = useState<Recipient[]>([])
  const [log, setLog] = useState<NotificationRow[]>([])
  const [name, setName] = useState('')
  const [email, setEmail] = useState('')
  const [minSeverity, setMinSeverity] = useState<'critical' | 'warning'>('critical')
  const [message, setMessage] = useState('')
  const [faults, setFaults] = useState<Record<string, boolean>>({})

  const load = useCallback(async () => {
    setRecipients(await api<Recipient[]>('/api/admin/recipients', session))
    setLog(await api<NotificationRow[]>('/api/notifications', session))
    setFaults(await api<Record<string, boolean>>('/api/admin/faults', session))
  }, [session])
  useEffect(() => { load().catch((e) => setMessage((e as Error).message)) }, [load])

  async function add(e: React.FormEvent) {
    e.preventDefault(); setMessage('')
    try {
      await api('/api/admin/recipients', session, { method: 'POST', body: JSON.stringify({ name, email, min_severity: minSeverity }) })
      setName(''); setEmail(''); setMessage(`Registered ${email}. New ${minSeverity} alerts will be e-mailed to this address.`); load()
    } catch (err) { setMessage((err as Error).message) }
  }
  async function remove(id: number) { await api(`/api/admin/recipients/${id}`, session, { method: 'DELETE' }); load() }
  async function toggle(k: string) {
    setFaults(await api<Record<string, boolean>>('/api/admin/faults', session, { method: 'PUT', body: JSON.stringify({ [k]: !faults[k] }) }))
  }
  async function rescan() { await api('/api/scan', session, { method: 'POST' }); load() }

  return (
    <div className="grid">
      <section className="card">
        <h2>Alert recipients</h2>
        <form onSubmit={add} className="stack">
          <label>Name<input value={name} onChange={(e) => setName(e.target.value)} data-testid="recipient-name" /></label>
          <label>E-mail<input type="email" value={email} onChange={(e) => setEmail(e.target.value)} data-testid="recipient-email" /></label>
          <label>Notify from<select value={minSeverity} onChange={(e) => setMinSeverity(e.target.value as 'critical' | 'warning')}>
            <option value="critical">critical only</option><option value="warning">warning and critical</option></select></label>
          <button disabled={!name || !email} data-testid="add-recipient">Register</button>
        </form>
        {message && <p className="muted" data-testid="admin-message">{message}</p>}
        <ul className="plain">{recipients.map((r) => (
          <li key={r.id} data-testid="recipient">{r.name} &lt;{r.email}&gt; <span className="chip">{r.min_severity}</span>
            <button className="small ghost" onClick={() => remove(r.id)}>Remove</button></li>))}</ul>
      </section>
      <section className="card wide">
        <h2>Fault injection</h2>
        <p className="muted">Switch a dependency into failure, then start a run to see how the pipeline handles it.</p>
        <div className="row">{Object.entries(FAULTS).map(([k, label]) => (
          <label key={k} className="inline toggle"><input type="checkbox" checked={Boolean(faults[k])} onChange={() => toggle(k)}
            data-testid={`fault-${k}`} /> {label}</label>))}</div>
      </section>
      <section className="card">
        <div className="head"><h2>Notification log</h2><button className="small" onClick={rescan}>Scan now</button></div>
        <table><thead><tr><th>When</th><th>Channel</th><th>To</th><th>Subject</th><th>Status</th></tr></thead>
          <tbody>{log.map((n) => (
            <tr key={n.id}><td className="muted">{n.created_at.slice(0, 16).replace('T', ' ')}</td><td>{n.channel}</td>
              <td>{n.recipient || '—'}</td><td>{n.subject}</td>
              <td><span className={`chip ${n.status === 'sent' || n.status === 'shown' ? 'normal' : 'warning'}`}>{n.status}</span>
                {n.error && <div className="muted">{n.error}</div>}</td></tr>))}</tbody></table>
      </section>
    </div>
  )
}
