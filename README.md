# Maintenance work-order assistant

An AI agent that turns an equipment alarm into a maintenance work order a planner can trust: it reads the asset, its
telemetry, its maintenance history and the spare parts that fit it, drafts a work order grounded in those records,
checks the draft, and waits for a planner to approve it before anything is written to the CMMS.

> Status: skeleton. Sections marked _TODO_ are filled in as the build proceeds.

## Quick start

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/uvicorn app.main:app --reload          # scripted model, no API key needed
# open http://127.0.0.1:8000  (API key: planner-demo-key)

APP_LLM=anthropic ANTHROPIC_API_KEY=... .venv/bin/uvicorn app.main:app   # Claude Haiku 5.5
```

## The workflow

1. A planner picks an asset in alarm and asks for a work order.
2. **Safety guard** (deterministic, no model call): requests to skip lockout/tagout, bypass interlocks or work on
   energised equipment are refused.
3. **Agent** (Claude Haiku 5.5, bounded to 6 steps) calls the read tools and proposes a draft with a strict schema.
4. **Validation** (deterministic) checks the draft against the source data: every evidence value must match a recorded
   reading, every part must be a stocked part for that asset type, lockout must cover every energy source, stale or
   rejected data must be flagged. A blocking issue disables approval.
5. **Approval** (human in the loop): the run pauses in a checkpoint until the planner approves, edits the priority,
   or rejects. The pause survives a server restart.
6. **Create**: the only write, to a mock CMMS. A CMMS timeout keeps the run waiting so the planner can retry.

## Architecture

```
app/
  api.py          HTTP API, roles, error mapping, serves the UI
  agent.py        LangGraph state machine: guard → agent → validate → approval → create
  llm.py          model interface: Claude Haiku 5.5 (caching, low effort, usage/cost) and a scripted stand-in
  tools.py        read tools + the strict propose_work_order schema; compact results, errors as is_error
  validation.py   groundedness and safety checks on the draft
  safety.py       deterministic refusal rules
  data_access.py  loads and validates source data; bad rows are quarantined, not passed on
  cmms.py         mock system of record (the only write)
  faults.py       switchable dependency failures for tests, evaluation and demo
  static/         single-page UI
data/             representative fixtures: 8 assets, telemetry, history, stores
```

_TODO: design decisions, assumptions, test strategy, evaluation method, CI tiers, limitations, next steps._
