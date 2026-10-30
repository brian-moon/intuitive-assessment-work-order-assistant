import type { PhaseEvent, Session } from './types'

const KEY = 'wo.session'

export function loadSession(): Session | null {
  try { const s = sessionStorage.getItem(KEY); return s ? JSON.parse(s) as Session : null } catch { return null }
}
export function saveSession(s: Session | null) {
  try { if (s) sessionStorage.setItem(KEY, JSON.stringify(s)); else sessionStorage.removeItem(KEY) } catch { /* private mode */ }
}

export class ApiError extends Error { constructor(public status: number, msg: string) { super(msg) } }

async function fail(r: Response): Promise<never> {
  const body = await r.json().catch(() => ({}))
  const d = (body as { detail?: unknown }).detail
  throw new ApiError(r.status, typeof d === 'string' ? d : d ? JSON.stringify(d) : r.statusText)
}

export async function api<T>(path: string, session: Session | null, init: RequestInit = {}): Promise<T> {
  const r = await fetch(path, {
    ...init,
    headers: { 'Content-Type': 'application/json', ...(session ? { Authorization: `Bearer ${session.token}` } : {}),
               ...(init.headers || {}) },
  })
  if (!r.ok) await fail(r)
  return r.json() as Promise<T>
}

/** POST that answers with Server-Sent Events; calls onEvent for every phase event as it arrives. */
export async function stream(path: string, session: Session, body: unknown, onEvent: (e: PhaseEvent) => void) {
  const r = await fetch(path, {
    method: 'POST', body: JSON.stringify(body),
    headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${session.token}`, Accept: 'text/event-stream' },
  })
  if (!r.ok || !r.body) await fail(r)
  const reader = r.body!.getReader()
  const dec = new TextDecoder()
  let buf = ''
  for (;;) {
    const { value, done } = await reader.read()
    if (done) break
    buf += dec.decode(value, { stream: true })
    let i
    while ((i = buf.indexOf('\n\n')) >= 0) {
      const chunk = buf.slice(0, i); buf = buf.slice(i + 2)
      const line = chunk.split('\n').find((l) => l.startsWith('data: '))
      if (line) onEvent(JSON.parse(line.slice(6)) as PhaseEvent)
    }
  }
}

/** GET Server-Sent Events (with the bearer token, which EventSource cannot send). Resolves when the stream ends. */
export async function listen<T>(path: string, session: Session, onEvent: (e: T) => void, signal: AbortSignal) {
  const r = await fetch(path, { headers: { Authorization: `Bearer ${session.token}`, Accept: 'text/event-stream' }, signal })
  if (!r.ok || !r.body) await fail(r)
  const reader = r.body!.getReader()
  const dec = new TextDecoder()
  let buf = ''
  for (;;) {
    const { value, done } = await reader.read()
    if (done) break
    buf += dec.decode(value, { stream: true })
    let i
    while ((i = buf.indexOf('\n\n')) >= 0) {
      const chunk = buf.slice(0, i); buf = buf.slice(i + 2)
      const line = chunk.split('\n').find((l) => l.startsWith('data: '))
      if (line) onEvent(JSON.parse(line.slice(6)) as T)
    }
  }
}
