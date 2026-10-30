# Maintenance work-order assistant

An AI agent that turns an equipment alarm into a maintenance work order a planner can trust. It reads the asset, its
telemetry, its maintenance history and the spare parts that fit it; drafts a work order grounded in those records;
checks every value of the draft against the source data; routes it by confidence; and waits for a planner to approve
it before anything is written to the CMMS.

## Quick start

Needs Docker (with Compose) only.

```bash
git clone https://github.com/brian-moon/intuitive-assessment.git
cd intuitive-assessment
docker compose up --build            # first build takes about 2 minutes
```

Open **http://localhost:8790** and sign in:

| User | Password | Can |
|---|---|---|
| `planner` | `planner-demo` | run the pipeline, approve or reject drafts, acknowledge alerts |
| `admin` | `admin-demo` | everything a planner can, plus alert recipients and fault injection |
| `viewer` | `viewer-demo` | read only |

**Settings** live in one file. Copy the template, fill in what you need, restart:

```bash
cp .env.example .env        # port, Claude key, alert e-mail and Gmail, simulator, logo; each line is commented
docker compose up -d
```

- **Claude**: `ANTHROPIC_API_KEY=` your own key (none ships with this repository). Without it a deterministic demo
  model drafts, labelled "Demo model" in the header, so everything can still be clicked through offline.
- **Who gets critical alerts**: `ALERT_EMAILS="Plant Admin <admin@example.com>"`, plus `SMTP_*` to actually send
  (Gmail: `smtp.gmail.com` and an app password). More recipients can be added on the Admin screen, which also shows
  the effective configuration and sends a test alert.
- **Port**: `APP_PORT=8790`.

Start again from the seed data with `docker compose down -v`. Both ports (app, PostgreSQL 5440) are bound to
localhost.

### A five-minute tour

1. Sign in as `planner`. The red banner blinks while critical alerts are open (the tab title flashes too; the bell
   in the header adds a sound).
2. **Dashboard, Live**: asset cards by status, a live readings feed, and the **telemetry simulator**: switch it on and
   set events per minute or per hour; each event is a random machine and metric with a random normal / warning /
   critical status. Critical readings raise the banner, a toast and the alert e-mails as they arrive.
   **History** shows any past window (1 h to 30 days, or custom): a summary per asset, or charts per metric with the
   warning and critical lines, and the alerts, runs and work orders of that window.
3. P-101 → **Run** → **Run pipeline**. Each phase streams in as it runs. Only the phase marked **LLM** (the agent's
   draft) calls a model; the **critic** is reconciliation, rule-based code. The **Reconciliation rules** card beside
   the draft shows every rule, the parameter that applies to this machine category, whether the draft passed it, and
   the confidence arithmetic that decides the routing.
4. Read the draft (every evidence row quotes a recorded reading), then **Approve**. The work order is generated and
   exported to the mock CMMS. Every screen has its own URL: the browser's back and forward arrows work and return
   to the same scroll position.
5. **Governance** shows the controls at work: the model ladder and its thresholds, where drafts ended up and why,
   what the guardrails stopped, the planners' decisions, alert delivery, cost per tier, the latest evaluation, and
   whether telemetry reaches the observability backend.
6. Try the failure paths:
   - Request "replace the bearing while it is running": refused before any model call.
   - S-401: its telemetry is stale, so the draft must say so.
   - As `admin` (Admin tab): register an alert recipient, or switch on a fault (historian down, stores invalid
     response, CMMS timeout, SMTP unavailable) and run P-101 again.

### Tests and evaluation

```bash
docker compose exec app python -m evals.run_eval --llm scripted              # harness check, no tokens
docker compose exec app python -m evals.run_eval --llm frontier --trials 3   # Claude Haiku 5.5, needs the key
```

The report lands in `reports/` inside the container and on the Governance screen. To run the test suites on the host
(Python 3.11 and Node 20; the compose database must be running):

```bash
python3.11 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/pytest -q                                                   # unit, integration, API, data, routing golden set
(cd frontend && npm ci && npm run build)
.venv/bin/playwright install chromium && .venv/bin/pytest -q -m browser   # the critical workflow in Chromium
```

## How it works

```
guard ─refused→ end                          deterministic: no unsafe request reaches a model
load_asset → data_quality → detect_anomalies thresholds per machine category; stale and rejected data flagged
alert                                        critical finding → dashboard banner + e-mail to registered recipients
agent_draft                                  model + 4 read tools + strict propose_work_order, at most 6 steps
reconcile                                    every value, part, isolation and data-age field checked → confidence
route                                        accept / next model / planner (the model ladder below)
approval                                     human in the loop; the pause is checkpointed in PostgreSQL
generate → export                            numbered work order, mock CMMS; a timeout keeps it for a retry
```

**Where the model runs.** One phase, `agent_draft`, calls a model: it reads the asset, telemetry, history and parts
through tools and proposes a draft. Everything before it (safety guard, thresholds, data quality, alerting) and after
it (reconciliation, routing, approval, generation, export) is deterministic code. Reconciliation is the **critic**: a
rule-based verifier, not a second model, because each check (is this reading in the records, does isolation cover every
energy source) has an exact answer. It produces the confidence the router uses.

The pipeline is an explicit LangGraph state machine (`app/pipeline.py`). Every phase emits an event that is
streamed to the browser (SSE), stored as the run's execution history (`run_events`), and recorded as an
OpenTelemetry span with GenAI semantic-convention attributes on model calls.

| Module | Responsibility |
|---|---|
| `app/api.py` | HTTP API, JWT roles, error mapping (401/403/404/409/422/429), serves the React app |
| `app/pipeline.py` | the state machine and its phases |
| `app/llm.py` | model access: Claude (prompt caching, low effort, usage and cost), any Messages-API server, scripted stand-in |
| `app/routing.py`, `data/routing.json` | the model ladder and its thresholds |
| `app/tools.py` | read tools and the strict draft schema; tool errors are results, not exceptions |
| `app/reconcile.py` | grounding, parts, isolation, data age and priority checks |
| `app/anomaly.py`, `app/notify.py` | anomaly scan and alerting (dashboard, e-mail; claimed once, retried on failure) |
| `app/safety.py` | the pre-model request guard |
| `app/db.py`, `app/schema.sql` | PostgreSQL store; source rows validated on load, rejects kept with the reason |
| `app/observability.py`, `app/trustplane.py` | OpenTelemetry tracing and governance events |
| `app/simulator.py` | live telemetry simulator (adjustable rate, random machine and status) |
| `frontend/` | React + TypeScript (Vite): live and history dashboard, run, governance, admin |

### Model ladder

A draft's **confidence is computed, not reported by the model**: reconciliation starts at 1.0 and subtracts 0.5 per
blocking issue (an evidence value not in the records, an unknown part, incomplete isolation, unflagged stale data,
the wrong asset) and 0.15 per warning (priority off the rule, a part on backorder).

| Tier | Configured by | Accepted at | Otherwise |
|---|---|---|---|
| Local model (e.g. Qwen through an Anthropic-compatible gateway) | `LOCAL_LLM_BASE_URL` | ≥ 0.85; P1 work needs 1.00 | next tier |
| Frontier: Claude Haiku 5.5 | `ANTHROPIC_API_KEY` | ≥ 0.70 | planner |
| Planner | always | | |

- With no local model configured, Claude is the first tier. With neither tier configured, the demo model runs.
- P1 risk comes from the data (worst severity × asset criticality), not from the draft, so a model that under-rates a
  P1 cannot slip under the stricter threshold.
- A data outage (historian down) goes straight to a planner: another model cannot fix missing data, so no frontier
  tokens are spent on it.
- Every work order is approved by a planner in any case. "Planner" in the ladder means no model met its threshold:
  the best draft is shown flagged for a field-by-field check, with approval disabled while it has blocking issues.

Claude Haiku 5.5 is the smallest current Claude model, and drafting from tool results does not need a larger one.
The stable prefix (tools and system prompt) is cached, effort is low, output is capped, and every call's tokens and
cost are recorded per tier.

## Testing

| Suite | What it proves |
|---|---|
| `tests/test_unit.py` | thresholds per category, priority rule, safety guard, tools, reconciliation rules, alert dedupe and failure, work-order document, cost accounting |
| `tests/test_integration.py` | the whole pipeline through the streamed API: phases in order, approval, rejection, priority edit, CMMS timeout and retry, historian and stores failures, model outage, a model that never calls a tool, a runaway tool loop, invented evidence, restart while paused, interrupted decisions, tracing |
| `tests/test_api.py` | sign-in, roles, validation, not-found, conflicts, rate limit, recipients, governance overview |
| `tests/test_data.py` | every source row loaded or rejected with a reason; the dashboard and the work order match the raw files, recomputed independently; schema upgrade of an older database |
| `tests/test_routing_golden.py` | 24 cases (`evals/routing_golden.jsonl`) over every flaw, ladder shape and final route, with deterministic flawed stand-in models |
| `tests/test_browser.py` | the critical workflow in Chromium: sign in, alert banner, streamed run, approval, work order, governance |

Tests use the scripted model: deterministic, no key, no tokens, no mail.

### AI evaluation

`evals/cases.jsonl` holds 13 cases: critical, warning and normal assets, stale and rejected data, unsafe and
instruction-override requests, a historian outage, an invalid stores response, a prompt injection planted in an
asset's maintenance history, and a request that names a different asset. Each run is scored on task completion,
groundedness, relevance, tool accuracy, safety and failure handling. Model output varies, so each case runs `--trials`
times; the report gives pass rates, per-case consistency, flaky cases, tokens, cost, latency and the routes taken.
`evals/thresholds.json` is the gate: below it the run exits non-zero.

## CI

`.github/workflows/ci.yml`, cheapest first:

| Trigger | Runs |
|---|---|
| pull request | lint, all tests on PostgreSQL, scripted evaluation |
| push to `main` | the above, the front-end build and the browser test (the pre-deploy gate) |
| nightly | live evaluation on Claude Haiku 5.5, 3 trials per case (repository secret `ANTHROPIC_API_KEY`) |

## Observability

Each run is one trace: a span per phase, a child span per model call (`gen_ai.*` attributes, tokens) and tool call.
Spans are kept in memory and shown per run (`GET /api/runs/{id}/trace`); `OTEL_EXPORTER_OTLP_ENDPOINT` sends them to
any OTLP collector. Optionally (`TRUSTPLANE_URL`, `TRUSTPLANE_TOKEN`) spans and governance events (guardrail
triggers, routing decisions, planner decisions, evaluation results) go to a TrustPlane governance ledger; sending is
fail-open and lost events are counted on the Governance screen.

## Security notes

- No secrets in the repository; demo users only with `APP_DEMO_USERS=1` (the compose default for local review).
  Otherwise `APP_USERS` and `APP_JWT_SECRET` are required.
- Tool inputs and the draft are schema-validated; tool results and the request are treated as data, not
  instructions, and the guard runs before any model call.
- Decisions and alert deliveries are claimed atomically: no double approval, one work order per run, one e-mail per
  recipient and reading, failed e-mails retried.
- The container runs as a non-root user; both ports are bound to localhost.

## Configuration

Every setting is in `.env.example`, commented: port, models, users, alert e-mail and SMTP (cooldown included), the
simulator's start-up state, and branding. Thresholds of the model ladder are in `data/routing.json`; machine-category
thresholds in `data/categories.json`.

**Branding.** Put a logo file in `branding/` (not committed; mounted read-only) and set `APP_BRAND_LOGO`,
`APP_BRAND_NAME` and `APP_BRAND_SUBTITLE`; the header and sign-in page use them.

**Live data.** The clock is real; the seed data is shifted at first start so its newest reading is just before
start-up (the demo opens with the same alarms, then the data ages and the simulator adds new readings). Tests and the
evaluation pin the clock (`APP_NOW`) so their answers never change.
