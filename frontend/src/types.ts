export type Role = 'viewer' | 'planner' | 'admin'
export type Severity = 'normal' | 'warning' | 'critical'

export interface Session { token: string; user: string; role: Role }

export interface Finding { metric: string; severity: Severity; value: number; unit: string; threshold: number | null }

export interface AssetRow {
  asset_id: string; name: string; site: string; line: string; criticality: string; category: string
  severity: Severity; stale: boolean | null; last_reading_at: string | null; rejected: number; findings: Finding[]
}

export interface Alert { id: number; severity: Severity; asset_id: string; subject: string; created_at: string }

export interface Dashboard {
  assets: AssetRow[]; alerts: Alert[]
  pending_approvals: { run_id: string; asset_id: string; priority: string | null; started_by: string; created_at: string }[]
  work_orders: { work_order: string; asset_id: string; priority: string; title: string; cmms_status: string; created_at: string }[]
  rejected_rows: { source: string; asset_id: string | null; reason: string }[]
  email: 'enabled' | 'disabled'
  now: string; live_clock: boolean; simulator: SimState
}

export type PhaseStatus = 'running' | 'done' | 'warning' | 'critical' | 'failed' | 'skipped' | 'waiting'

export interface PhaseEvent {
  phase: string; label: string; status: PhaseStatus | string; detail: string; t?: number; run_id?: string
  data?: Record<string, unknown>
}

export interface Evidence { metric: string; value: number; unit: string; timestamp: string }
export interface Draft {
  asset_id: string; priority: string; problem_summary: string; evidence: Evidence[]; recommended_actions: string[]
  parts: { part_no: string; qty: number }[]
  safety: { lockout_tagout_required: boolean; energy_sources: string[]; notes: string }
  needs_data_refresh: boolean
}
export interface Issue { severity: 'blocking' | 'warning'; code: string; message: string }

export interface Recipient { id: number; name: string; email: string; min_severity: 'warning' | 'critical' }
export interface NotificationRow {
  id: number; channel: string; recipient: string | null; severity: Severity; asset_id: string; subject: string
  status: string; error: string | null; created_at: string; acknowledged_at: string | null
}

export interface SimState {
  enabled: boolean; rate: number; unit: 'minute' | 'hour'; mix: Record<'normal' | 'warning' | 'critical', number>
  updated_by: string | null; updated_at: string; events_last_hour: number; last_event_at: string | null; max_per_minute: number
}
export interface Reading {
  id: number; asset_id: string; metric: string; value: number; unit: string; ts: string; source: string
  severity: Severity; backlog?: boolean
}
export type LiveEvent =
  | ({ type: 'reading' } & Reading)
  | { type: 'alert'; id: number; channel: string; recipient: string | null; severity: Severity; asset_id: string
      subject: string; status: string; created_at: string }
  | { type: 'heartbeat'; now: string; simulator: SimState }
export interface Branding { name: string; subtitle: string; logo: string | null; deployment: string }
