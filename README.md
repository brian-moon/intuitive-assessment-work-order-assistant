# Maintenance work-order assistant

An AI agent that turns an equipment alarm into a maintenance work order a planner can trust. It reads the asset, its
telemetry, its maintenance history and the spare parts that fit it; drafts a work order grounded in those records;
checks every value of the draft against the source data; routes it by confidence; and waits for a planner to approve
it before anything is written to the CMMS.

## Quick start

Needs Docker (with Compose) only.

```bash
git clone https://github.com/brian-moon/intuitive-assessment-work-order-assistant.git
cd intuitive-assessment-work-order-assistant
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
- **Who gets critical alerts**: no file needed. Signed in as `admin`, the dashboard asks *Where should critical
  alerts go?*; enter an address and the current critical alerts are e-mailed at once. (Or `ALERT_EMAILS=` in `.env`,
  or the Admin screen, which also sends a test alert.)
- **Reading the alert e-mail**: out of the box it is delivered to a built-in test inbox (Mailpit) at
  **http://localhost:8025**: no account, nothing leaves your machine. For real delivery set `SMTP_*` in `.env` (e.g.
  Gmail: `smtp.gmail.com`, port 587, your address and an app password).
- **Port**: `APP_PORT=8790`.

Start again from the seed data with `docker compose down -v`. All ports (app 8790, test inbox 8025, PostgreSQL 5440)
are bound to localhost.

### A five-minute tour

1. Sign in as `planner`. The red banner blinks while critical alerts are open (the tab title flashes too; the bell
   in the header adds a sound).
2. **Dashboard, Live**: asset cards by status, a live readings feed, and the **telemetry simulator**, which runs
   from start-up at 6 events per minute (switch it off, or set the rate per minute or per hour). Each event is a
   random machine and metric with a random normal / warning / critical status. A critical reading raises the banner
   and a toast as it arrives; simulated readings never send e-mail (see Alerting below).
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

## Test strategy

Risk-based: the expensive failures here are an ungrounded or unsafe work order reaching the CMMS, an alarm nobody
sees, and a run stuck or approved twice. So the deterministic checks that prevent those carry the most tests, the
model is exercised through stand-ins that fail in specific ways (no tokens, no flakiness), and the real model's
behaviour is measured separately by the evaluation (below), where variance is expected and reported.

| Suite | What it proves |
|---|---|
| `tests/test_unit.py` | thresholds per category, priority rule, safety guard, tools, reconciliation rules, alert dedupe and failure, work-order document, cost accounting |
| `tests/test_integration.py` | the whole pipeline through the streamed API: phases in order, approval, rejection, priority edit, CMMS timeout and retry, historian and stores failures, model outage, a model that never calls a tool, a runaway tool loop, invented evidence, restart while paused, interrupted decisions, tracing |
| `tests/test_api.py` | sign-in, roles, validation, not-found, conflicts, rate limit, recipients, governance overview |
| `tests/test_data.py` | every source row loaded or rejected with a reason; the dashboard and the work order match the raw files, recomputed independently; schema upgrade of an older database |
| `tests/test_routing_golden.py` | 24 cases (`evals/routing_golden.jsonl`) over every flaw, ladder shape and final route, with deterministic flawed stand-in models |
| `tests/test_mail.py` | mail client against a fake server: no sign-in or TLS for the test inbox, TLS and sign-in for a real server, high importance on critical mail; the recipient prompt's data never reveals addresses |
| `tests/test_live.py` | simulator values land in the requested threshold band; events are stored, scanned and alerted; e-mail cooldown; simulated readings never e-mail; live stream, its connection cap, history windows, live clock |
| `tests/test_browser.py` | in Chromium: sign in, blinking alert banner, streamed run, every reconciliation rule passed, approval, work order, governance, browser back/forward; live feed and simulator switch, history charts, scroll position restored; an admin registers the alert recipient from the dashboard prompt |

Tests use the scripted model: deterministic, no key, no tokens, no mail. 139 tests plus 3 browser tests pass.

### AI evaluation

`evals/cases.jsonl` holds 13 cases: critical, warning and normal assets, stale and rejected data, unsafe and
instruction-override requests, a historian outage, an invalid stores response, a prompt injection planted in an
asset's maintenance history, and a request that names a different asset. Each run is scored on task completion,
groundedness, relevance, tool accuracy, safety and failure handling. Model output varies, so each case runs `--trials`
times; the report gives pass rates, per-case consistency, flaky cases, tokens, cost, latency and the routes taken.
`evals/thresholds.json` is the gate: below it the run exits non-zero. The evaluation runs with a pinned clock and no
simulator, so differences between trials come from the model alone.

**Results on Claude Haiku 5.5** (3 trials × 13 cases each run):

| Criterion | First run | After fixes | Threshold |
|---|---|---|---|
| task completion | 92% | 100% | 85% |
| groundedness | 90% | 100% | 100% |
| relevance | 90% | 100% | 90% |
| tool accuracy | 82% | 100% | 90% |
| safety | 100% | 100% | 100% |
| failure handling | 97% | 100% | 90% |
| flaky cases | E01-critical-pump, E02-critical-compressor | none | |
| cost per run | $0.00079 | $0.00070 | |
| latency per run | 8.13 s | 6.21 s | |

Reports: `reports/eval-frontier-baseline.*` (first run) and `reports/eval-frontier.*` (after fixes).

The first run (`reports/eval-frontier-baseline.*`) fell below the groundedness and tool-accuracy gates, and the
reasons were real: the 1,500-token output cap cut tool input off mid-call; the draft schema did not state the
600-character summary limit or the exact timestamp format; the model obeyed a request that named a different asset
(reconciliation blocked it, so it reached a planner, not the CMMS); and once, with the stores service down, it listed
parts anyway. Fixes: output cap 4,096, schema descriptions, two prompt rules, and `wrong_asset` scored as relevance
rather than groundedness. A run in between still showed one schema gap (an empty evidence list for a routine check,
which the model corrected on its next step); the schema now states it. 100% on 39 runs is not a guarantee: the set is
small, and the nightly run exists to catch drift. This is what the gate is for: a prompt or model change is judged on the same cases before
it ships.

## CI

`.github/workflows/ci.yml`, cheapest first:

| Trigger | Runs |
|---|---|
| pull request | lint, all tests on PostgreSQL, scripted evaluation |
| push to `main` | the above, the front-end build and the browser test (the pre-deploy gate) |
| nightly | live evaluation on Claude Haiku 5.5, 3 trials per case (repository secret `ANTHROPIC_API_KEY`; skipped with a notice when it is not set) |

Why this split: pull requests need fast, deterministic signal and must not spend tokens or flake on model variance,
so they get lint, every test and the scripted evaluation. The browser test is slower and depends on the built front
end, so it gates what is deployed from `main`. Model behaviour drifts (new model versions, prompt edits), so the
live evaluation runs on a schedule and on demand, and a drop below the thresholds fails the run.

## Observability

Each run is one trace: a span per phase, a child span per model call (`gen_ai.*` attributes, tokens) and tool call.
Spans are kept in memory and shown per run (`GET /api/runs/{id}/trace`); `OTEL_EXPORTER_OTLP_ENDPOINT` sends them to
any OTLP collector. Optionally (`TRUSTPLANE_URL`, `TRUSTPLANE_TOKEN`) spans and governance events (guardrail
triggers, routing decisions, planner decisions, evaluation results) go to a TrustPlane governance ledger; sending is
fail-open and lost events are counted on the Governance screen.

## Alerting

A critical finding reaches people by two routes: a dashboard alert (blinking banner, toast, flashing tab title,
optional sound) until someone acknowledges it, and an HTML e-mail with high-importance headers to every registered
recipient, through the configured mail server or, when none is set, the built-in test inbox. Each recipient gets one
e-mail per asset, metric and severity per 15 minutes
(`ALERT_EMAIL_COOLDOWN_MIN`), because live telemetry repeats; the dashboard still records every alert. Readings
from the simulator alert on the dashboard only, never by e-mail, so a demo generator cannot fill real inboxes.
Every delivery attempt is recorded with its status, and a failed e-mail is retried by the next scan.

## Security notes

- No secrets in the repository; demo users only with `APP_DEMO_USERS=1` (the compose default for local review),
  otherwise `APP_USERS` is required. The token-signing key comes from `APP_JWT_SECRET`, or is generated once and kept
  in the database, so a restart does not sign everyone out.
- Viewers never see recipients' e-mail addresses; live streams are capped per user and in total.
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
evaluation pin the clock to the seed data's "now", so their answers never change (`APP_NOW` does the same for a
running instance).

## Scope

The brief asks for one deliberately small workflow. The core is that workflow: an alarm on one asset becomes an
agent draft, checked against the source data, approved by a planner and exported, with its tests, evaluation and CI.
Around it, a few additions go beyond the minimum, each separable from the core: the live dashboard with the telemetry
simulator and history, the model ladder (local model, then Claude, then a planner), the governance screen, and
branding. They are there because they show how the same workflow behaves under live data and how its controls would
be operated, not because the workflow needs them.

Time: the core workflow, with its tests, evaluation and CI, took about 1.5 hours; the additions about 2.5 more. Both
were built with an AI coding assistant (see AI usage).

## Design decisions

- **One model call site.** Only `agent_draft` uses a model; thresholds, data quality, reconciliation, routing and
  every write are code. What can be decided exactly is never left to a model.
- **The critic is a verifier, not a second model.** Each reconciliation rule has an exact answer from the source
  records, so it is cheaper, faster and more reliable than an LLM judge, and its output (a confidence) drives routing.
- **Confidence is computed, not self-reported.** Self-reported model confidence is not reliable enough to route on.
- **Human approval is structural.** The graph pauses in a PostgreSQL checkpoint; there is no code path from a draft
  to a work order without a planner's decision, and a draft with a blocking issue cannot be approved.
- **Tools are read-only.** The agent cannot write; the single write (the work order) happens after approval.
- **Smallest capable model, with a ladder.** Claude Haiku 5.5 by default; a local model can sit in front of it, with
  thresholds deciding when to escalate, so cost scales with difficulty.
- **Mocks where the brief allows.** The CMMS, the historian outage and the stores service are mocked behind fault
  switches; the data is a representative fixture set loaded through the same validation a real feed would pass.
- **PostgreSQL from the start.** Checkpoints, run history, alert claims and data share one transactional store, so
  "approve once" and "alert once" are database guarantees, not hopes.

## Assumptions

- Telemetry arrives as (asset, metric, value, unit, timestamp) readings; thresholds are per machine category.
- Priority follows a fixed rule from the worst severity and the asset's criticality (shown on the rule card).
- Lockout/tagout must cover every energy source recorded for the asset; the asset register is correct.
- A planner approves every work order; the assistant drafts, it never dispatches.
- The CMMS accepts one work order per approved run; its timeout is transient and retried by the planner.
- Demo sign-in (three fixed users) stands in for the plant's identity provider.

## Limitations

- The CMMS, historian and stores service are mocks; there is no real integration.
- Sign-in is a local user list with JWTs, not SSO; there is no per-site authorization.
- Reconciliation checks what can be checked against records (grounding, parts, isolation, data age, priority); it
  cannot judge whether the recommended actions are the best repair.
- The evaluation set is small (13 cases) and hand-written; pass rates on it are indicative, not statistically strong.
- The simulator produces independent random readings, not physically plausible degradation curves.
- One process: the simulator and live streams run in the API process; scaling out would move them to workers.

## With more time

- **Product:** real CMMS and historian connectors; SSO and per-site roles; editable drafts (not only priority);
  maintenance-history retrieval over free text; scheduled scans and escalation of unacknowledged alerts; mobile view
  for technicians.
- **Quality:** a larger evaluation set built from real (anonymised) alarms and planner decisions, with planner
  corrections fed back as cases; an LLM-assisted review of recommended actions, measured against planner edits;
  contract tests against the CMMS API; load tests of the live stream; mutation testing of the reconciliation rules;
  accessibility checks in the browser suite.
- **Operations:** OTLP export to a collector by default (`future/otel-collector.yaml`), alerting on evaluation
  regressions, and the live evaluation in CI with a repository secret.

## Deliverables

| Asked for | Where |
|---|---|
| Working AI application, backend API, simple user experience | `app/`, `frontend/`; `docker compose up --build` |
| Agent with tool use, state across steps, error handling, human approval | `app/pipeline.py`, `app/tools.py`, LangGraph checkpoints, the approval phase |
| Separation of logic, orchestration, tools, data access, state, UX | module table under How it works |
| Unit, integration, API, data-validation and browser tests | `tests/` (see Test strategy) |
| AI evaluation dataset, scoring, executable evaluation | `evals/cases.jsonl`, `evals/run_eval.py`, `evals/thresholds.json` |
| Injected failures | Admin fault switches; stale and rejected data in the seed; unsafe requests; flawed stand-in models |
| Diagnostics | per-run execution history, OpenTelemetry traces, logs, screenshots and reports in `reports/` |
| Test and evaluation reports | `reports/` (evaluation reports, browser screenshots) |
| CI configuration and explanation | `.github/workflows/ci.yml`, CI section above |
| Commit history | `git log` |
| More time | section above |

## AI usage

Built with an AI coding assistant (Claude Code) as the brief requires, under my direction: I set the scenario, the
architecture, the model choice and the ladder thresholds, reviewed the code and tests, and directed every change,
including the fixes from the first live evaluation. The runtime agent uses Claude Haiku 5.5.
