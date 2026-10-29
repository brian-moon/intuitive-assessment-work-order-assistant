-- Work-order assistant schema (PostgreSQL 15+). Applied idempotently at startup.
CREATE TABLE IF NOT EXISTS machine_categories (
    category          text PRIMARY KEY,
    label             text NOT NULL,
    max_data_age_h    numeric NOT NULL CHECK (max_data_age_h > 0),
    evidence_tolerance numeric NOT NULL DEFAULT 0 CHECK (evidence_tolerance >= 0),
    metrics           jsonb NOT NULL                 -- {metric: {unit, direction, warning, critical}}
);

CREATE TABLE IF NOT EXISTS assets (
    asset_id       text PRIMARY KEY CHECK (asset_id ~ '^[A-Z]-[0-9]{3}$'),
    name           text NOT NULL,
    category       text NOT NULL REFERENCES machine_categories(category),
    site           text NOT NULL,
    line           text NOT NULL,
    criticality    text NOT NULL CHECK (criticality IN ('low', 'medium', 'high')),
    energy_sources text[] NOT NULL
);

CREATE TABLE IF NOT EXISTS telemetry (
    id        bigserial PRIMARY KEY,
    asset_id  text NOT NULL REFERENCES assets(asset_id),
    ts        timestamptz NOT NULL,
    metric    text NOT NULL,
    value     double precision NOT NULL,
    unit      text NOT NULL,
    UNIQUE (asset_id, ts, metric)
);
CREATE INDEX IF NOT EXISTS telemetry_asset_ts ON telemetry (asset_id, ts DESC);

CREATE TABLE IF NOT EXISTS maintenance_history (
    work_order text PRIMARY KEY,
    asset_id   text NOT NULL REFERENCES assets(asset_id),
    date       date NOT NULL,
    summary    text NOT NULL,
    parts      text[] NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS spare_parts (
    part_no        text PRIMARY KEY,
    description    text NOT NULL,
    fits_types     text[] NOT NULL,
    on_hand        integer NOT NULL CHECK (on_hand >= 0),
    location       text NOT NULL,
    lead_time_days integer NOT NULL CHECK (lead_time_days >= 0)
);

-- Rows that failed validation on load: kept with the reason, never silently dropped.
CREATE TABLE IF NOT EXISTS data_rejections (
    id       bigserial PRIMARY KEY,
    source   text NOT NULL,
    asset_id text,
    row_data jsonb NOT NULL,
    reason   text NOT NULL
);

-- Result of the last anomaly scan, one row per asset and metric.
CREATE TABLE IF NOT EXISTS anomalies (
    asset_id   text NOT NULL REFERENCES assets(asset_id),
    metric     text NOT NULL,
    severity   text NOT NULL CHECK (severity IN ('normal', 'warning', 'critical')),
    value      double precision NOT NULL,
    unit       text NOT NULL,
    ts         timestamptz NOT NULL,
    threshold  double precision,
    stale      boolean NOT NULL,
    scanned_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (asset_id, metric)
);

CREATE TABLE IF NOT EXISTS alert_recipients (
    id           bigserial PRIMARY KEY,
    name         text NOT NULL,
    email        text NOT NULL UNIQUE,
    min_severity text NOT NULL DEFAULT 'critical' CHECK (min_severity IN ('warning', 'critical')),
    created_at   timestamptz NOT NULL DEFAULT now()
);

-- Every alert raised, per channel, with the delivery result (dashboard, email).
CREATE TABLE IF NOT EXISTS notifications (
    id         bigserial PRIMARY KEY,
    dedupe_key text NOT NULL,
    channel    text NOT NULL,
    recipient  text,
    severity   text NOT NULL,
    asset_id   text NOT NULL,
    subject    text NOT NULL,
    status     text NOT NULL,            -- sent | failed | disabled | shown
    error      text,
    created_at timestamptz NOT NULL DEFAULT now(),
    acknowledged_at timestamptz,
    UNIQUE (dedupe_key, channel, recipient)
);

CREATE TABLE IF NOT EXISTS runs (
    run_id     text PRIMARY KEY,
    asset_id   text NOT NULL REFERENCES assets(asset_id),
    request    text NOT NULL,
    started_by text NOT NULL,
    status     text NOT NULL,
    priority   text,
    reason     text,
    trace_id   text,
    usage      jsonb NOT NULL DEFAULT '{}',
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

-- The execution history: every phase event streamed to the UI, stored for diagnostics.
CREATE TABLE IF NOT EXISTS run_events (
    id      bigserial PRIMARY KEY,
    run_id  text NOT NULL REFERENCES runs(run_id),
    seq     integer NOT NULL,
    event   jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS run_events_run ON run_events (run_id, seq);

CREATE TABLE IF NOT EXISTS work_orders (
    work_order   text PRIMARY KEY,
    run_id       text NOT NULL REFERENCES runs(run_id),
    asset_id     text NOT NULL REFERENCES assets(asset_id),
    priority     text NOT NULL,
    title        text NOT NULL,
    body         jsonb NOT NULL,          -- the approved draft and the evidence it rests on
    document_md  text NOT NULL,           -- the generated work-order document
    approved_by  text NOT NULL,
    cmms_status  text NOT NULL,           -- exported | pending_retry
    created_at   timestamptz NOT NULL DEFAULT now()
);
