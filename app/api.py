"""HTTP API: auth, dashboard data, the streamed pipeline (SSE), approvals, alerts, admin and work orders.

Roles (JWT from ``POST /api/login``): *viewer* reads; *planner* runs the pipeline, decides, acknowledges alerts;
*admin* does everything a planner does and manages alert recipients and fault injection. Validation errors are 422,
unknown ids 404, a decision on a run that is not waiting for one (or an approval of a draft with blocking issues) 409.
"""
from __future__ import annotations

import hmac
import json
import logging
import re
import threading
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path

import jwt
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from langgraph.checkpoint.postgres import PostgresSaver
from psycopg.errors import UniqueViolation
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from . import anomaly
from .cmms import MockCMMS
from .config import SEED_ANCHOR, Settings
from .db import Database
from .faults import Faults
from .llm import LLM, Tier, make_tiers
from .models import Decision, Login, Recipient, RunRequest, SimulatorUpdate
from .notify import Notifier, smtp_sender
from .observability import finished_spans, setup
from .pipeline import Pipeline
from .reconcile import has_blocking, rule_card
from .routing import Policy
from .simulator import Simulator
from .tools import Toolbox
from .trustplane import GovernanceSink

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("app.api")
DIST = Path(__file__).resolve().parent.parent / "frontend" / "dist"
RANKS = {"viewer": 0, "planner": 1, "admin": 2}


def parse_recipients(raw: str) -> list[tuple[str, str]]:
    """``Name <a@b.org>, c@d.org`` -> [(name, email)]; entries that are not an address are skipped with a warning."""
    out = []
    for item in filter(None, (x.strip() for x in raw.split(","))):
        m = re.fullmatch(r"(?:(.*?)\s*<)?([^<>\s]+@[^<>\s]+\.[^<>\s]+)>?", item)
        if not m:
            log.warning("ALERT_EMAILS entry %r is not an e-mail address; skipped", item)
            continue
        out.append(((m.group(1) or m.group(2).split("@")[0]).strip(), m.group(2).lower()))
    return out


def create_app(settings: Settings | None = None, llm: LLM | None = None, send=None, reset: bool = False,
               tiers: list[Tier] | None = None, gov: GovernanceSink | None = None) -> FastAPI:
    """``llm`` injects one model as the only tier, ``tiers`` a whole ladder (tests); otherwise the ladder comes from
    the environment (llm.make_tiers)."""
    settings = settings or Settings()
    db = Database(settings.database_url)
    db.init_schema()
    if reset:
        db.reset()
    if not db.row("SELECT 1 AS x FROM assets LIMIT 1"):
        # live clock: move the seed data so its newest reading is just before now (same alarms, current times)
        shift = timedelta(minutes=int((settings.now - SEED_ANCHOR).total_seconds() // 60)) if settings.live else timedelta(0)
        log.info("seeded %s (time shift %s)", db.seed(settings.data_dir, shift), shift)
    for name, email in parse_recipients(settings.alert_emails):
        db.exec("INSERT INTO alert_recipients (name, email) VALUES (%s, %s) ON CONFLICT (email) DO NOTHING", name, email)
    jwt_secret = settings.jwt_secret or db.secret("jwt_signing_key")
    faults = Faults()
    notifier = Notifier(db, faults, settings.public_url, send if send is not None else smtp_sender(settings),
                        settings.email_cooldown_min)
    findings = anomaly.scan(db, settings.now)
    notifier.alert(findings)

    saver_pool = ConnectionPool(settings.database_url, min_size=1, max_size=4, open=True,
                                kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row})
    saver = PostgresSaver(saver_pool)
    saver.setup()
    tiers = tiers or ([Tier("model", llm)] if llm else make_tiers(settings))
    policy = Policy.load(settings.routing_file)
    gov = gov or GovernanceSink(settings.trustplane_url, settings.trustplane_token, settings.trustplane_producer,
                                settings.deployment)
    tracer = setup(settings.trustplane_url, settings.trustplane_token, settings.deployment)
    pipeline = Pipeline(db, tiers, Toolbox(db, faults, lambda: settings.now), notifier, MockCMMS(faults, settings.cmms_timeout_s),
                        settings, saver, tracer, policy, gov)
    log.info("model ladder: %s -> planner", " -> ".join(f"{t.name}:{t.llm.model}" for t in tiers))

    app = FastAPI(title="Maintenance work-order assistant", version="0.2.0")
    app.state.settings, app.state.db, app.state.faults, app.state.pipeline = settings, db, faults, pipeline
    app.state.notifier, app.state.tiers, app.state.policy, app.state.gov = notifier, tiers, policy, gov
    sim = Simulator(db, notifier, lambda: settings.now, autostart=settings.sim_autostart, rate=settings.sim_rate,
                    unit=settings.sim_unit, start_thread=settings.live)
    app.state.simulator = sim

    def close() -> None:   # release both connection pools (tests create many apps; a server process exits instead)
        sim.close()
        gov.close()
        saver_pool.close()
        db.close()
    app.state.close = close

    # --- auth -----------------------------------------------------------------------------------------------------
    def user(authorization: str | None = Header(default=None)) -> dict:
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(401, "Sign in first.")
        try:
            claims = jwt.decode(authorization[7:], jwt_secret, algorithms=["HS256"])
        except jwt.PyJWTError:
            raise HTTPException(401, "Session expired or invalid; sign in again.") from None
        return {"name": claims["sub"], "role": claims["role"]}

    def need(role: str):
        def check(u: dict = Depends(user)) -> dict:
            if RANKS[u["role"]] < RANKS[role]:
                raise HTTPException(403, f"Only a {role} can do this.")
            return u
        return check

    @app.post("/api/login")
    def login(body: Login) -> dict:
        entry = settings.users.get(body.username)
        if not entry or not hmac.compare_digest(entry[0], body.password):
            time.sleep(0.3)
            raise HTTPException(401, "Wrong user name or password.")
        token = jwt.encode({"sub": body.username, "role": entry[1], "exp": int(time.time()) + settings.jwt_ttl_s},
                           jwt_secret, algorithm="HS256")
        return {"token": token, "user": body.username, "role": entry[1]}

    @app.get("/api/branding")
    def branding() -> dict:
        """Public (the sign-in page shows it). The logo is a file the operator put in the branding folder."""
        name = Path(settings.brand_logo).name
        logo = f"/branding/{name}" if name and (settings.brand_dir / name).is_file() else None
        return {"name": settings.brand_name, "subtitle": settings.brand_subtitle, "logo": logo,
                "deployment": settings.deployment}

    @app.get("/api/me")
    def me(u: dict = Depends(user)) -> dict:
        return u

    @app.get("/health")
    def health() -> dict:
        return {"ok": True, "ladder": [t.describe() for t in tiers],
                "demo_model": [t.name for t in tiers] == ["demo"],
                "email": "enabled" if notifier.send is not None else "disabled", "trustplane": gov.enabled}

    # --- dashboard ------------------------------------------------------------------------------------------------
    @app.get("/api/dashboard")
    def dashboard(_: dict = Depends(user)) -> dict:
        assets = db.rows("""
            SELECT a.asset_id, a.name, a.site, a.line, a.criticality, c.label AS category,
                   coalesce(max(CASE n.severity WHEN 'critical' THEN 2 WHEN 'warning' THEN 1 ELSE 0 END), 0) AS rank,
                   bool_or(n.stale) AS stale, max(n.ts) AS last_reading_at,
                   (SELECT count(*) FROM data_rejections r WHERE r.asset_id = a.asset_id) AS rejected,
                   coalesce(json_agg(json_build_object('metric', n.metric, 'severity', n.severity, 'value', n.value,
                            'unit', n.unit, 'threshold', n.threshold) ORDER BY n.metric)
                            FILTER (WHERE n.severity <> 'normal'), '[]') AS findings
            FROM assets a JOIN machine_categories c USING (category) LEFT JOIN anomalies n USING (asset_id)
            GROUP BY a.asset_id, c.label ORDER BY rank DESC, a.asset_id""")
        for a in assets:
            a["severity"] = ["normal", "warning", "critical"][a.pop("rank")]
        alerts = db.rows("SELECT id, severity, asset_id, subject, created_at FROM notifications "
                         "WHERE channel = 'dashboard' AND acknowledged_at IS NULL ORDER BY created_at DESC")
        pending = db.rows("SELECT run_id, asset_id, priority, started_by, created_at FROM runs "
                          "WHERE status = 'awaiting_approval' ORDER BY created_at")
        orders = db.rows("SELECT work_order, asset_id, priority, title, cmms_status, created_at FROM work_orders "
                         "ORDER BY created_at DESC LIMIT 10")
        return {"assets": assets, "alerts": alerts, "pending_approvals": pending, "work_orders": orders,
                "rejected_rows": db.rows("SELECT source, asset_id, reason FROM data_rejections ORDER BY id"),
                "email": "enabled" if notifier.send is not None else "disabled",
                "now": settings.now, "live_clock": settings.live, "simulator": sim.state()}

    # --- live telemetry and history -------------------------------------------------------------------------------
    @app.get("/api/simulator")
    def simulator_state(_: dict = Depends(user)) -> dict:
        return sim.state()

    @app.put("/api/simulator")
    def simulator_update(body: SimulatorUpdate, u: dict = Depends(need("planner"))) -> dict:
        try:
            return sim.update(u["name"], **body.model_dump())
        except ValueError as e:
            raise HTTPException(422, str(e)) from None

    @app.post("/api/simulator/emit")
    def simulator_emit(_: dict = Depends(need("planner"))) -> dict:
        """One event now, whatever the switch says (for a quick look without waiting)."""
        return sim.emit_one()

    rules_cache: dict[str, dict] = {}

    def severity_of(asset_id: str, metric: str, value: float) -> str:
        if asset_id not in rules_cache:
            rules_cache[asset_id] = db.category(db.asset(asset_id).category).metrics
        rule = rules_cache[asset_id].get(metric)
        return rule.severity(value) if rule else "normal"

    def reading_event(r: dict, backlog: bool = False) -> dict:
        return {"type": "reading", "id": r["id"], "asset_id": r["asset_id"], "metric": r["metric"], "value": r["value"],
                "unit": r["unit"], "ts": r["ts"], "source": r["source"],
                "severity": severity_of(r["asset_id"], r["metric"], r["value"]), "backlog": backlog}

    # Each open stream holds a worker thread, so they are capped. A slot carries an expiry that the running stream
    # renews every second; one whose stream never started (client gone first) frees itself after 60 s. Release is
    # idempotent.
    stream_slots: dict[str, tuple[str, float]] = {}
    stream_lock = threading.Lock()

    @app.get("/api/live/stream")
    def live_stream(seconds: int = 600, u: dict = Depends(user)):
        """Server-sent events: new readings and new alerts as they are stored, and a heartbeat with the simulator
        state every few seconds. The stream ends after ``seconds`` (at most 10 minutes); the browser reconnects."""
        seconds = max(1, min(seconds, 600))
        def gen():
            recent = db.rows("SELECT id, asset_id, metric, value, unit, ts, source FROM telemetry "
                             "ORDER BY ts DESC, id DESC LIMIT 20")
            for r in reversed(recent):
                yield reading_event(r, backlog=True)
            last_t = db.row("SELECT coalesce(max(id), 0) AS m FROM telemetry")["m"]
            last_n = db.row("SELECT coalesce(max(id), 0) AS m FROM notifications")["m"]
            started, beat = time.time(), 0.0
            while time.time() - started < seconds:
                with stream_lock:                    # a running stream keeps its slot alive
                    if slot in stream_slots:
                        stream_slots[slot] = (u["name"], time.time() + 60)
                for r in db.rows("SELECT id, asset_id, metric, value, unit, ts, source FROM telemetry WHERE id > %s "
                                 "ORDER BY id LIMIT 200", last_t):
                    last_t = r["id"]
                    yield reading_event(r)
                for n in db.rows("SELECT id, channel, severity, asset_id, subject, status, created_at "
                                 "FROM notifications WHERE id > %s ORDER BY id LIMIT 200", last_n):
                    last_n = n["id"]
                    if n["channel"] == "dashboard":       # e-mail rows name recipients: planners see those in Admin
                        yield {"type": "alert", **n}
                if time.time() - beat > 5:
                    beat = time.time()
                    yield {"type": "heartbeat", "now": settings.now, "simulator": sim.state()}
                time.sleep(1)
        slot = uuid.uuid4().hex
        with stream_lock:
            now_ = time.time()
            for k in [k for k, (_, exp) in stream_slots.items() if exp < now_]:
                del stream_slots[k]
            mine = sum(1 for who, _ in stream_slots.values() if who == u["name"])
            if len(stream_slots) >= 20 or mine >= 3:
                raise HTTPException(429, "Too many live streams open; close another tab.")
            stream_slots[slot] = (u["name"], now_ + 60)     # renewed every second once the stream runs

        def counted():
            try:
                yield from gen()
            finally:
                with stream_lock:
                    stream_slots.pop(slot, None)
        return sse(counted())

    @app.get("/api/history")
    def history(start: datetime, end: datetime, asset_id: str | None = None, _: dict = Depends(user)) -> dict:
        """Past data for a time window: per asset a summary, or for one asset every reading with its thresholds,
        and the alerts, runs and work orders of the window."""
        if end <= start:
            raise HTTPException(422, "end must be after start")
        if end - start > timedelta(days=93):
            raise HTTPException(422, "the window is limited to 93 days")
        where, args = "ts >= %s AND ts < %s", [start, end]
        if asset_id:
            if db.asset(asset_id) is None:
                raise HTTPException(404, f"No asset {asset_id}.")
            where += " AND asset_id = %s"
            args.append(asset_id)
        readings = db.rows(f"SELECT id, asset_id, metric, value, unit, ts, source FROM telemetry WHERE {where} "
                           "ORDER BY ts LIMIT 5001", *args)
        truncated = len(readings) > 5000
        readings = [{**reading_event(r), "type": None} for r in readings[:5000]]
        summary: dict[str, dict] = {}
        for r in readings:
            s = summary.setdefault(r["asset_id"], {"asset_id": r["asset_id"], "readings": 0, "warning": 0, "critical": 0,
                                                   "first": r["ts"], "last": r["ts"]})
            s["readings"] += 1
            s["last"] = r["ts"]
            if r["severity"] in ("warning", "critical"):
                s[r["severity"]] += 1
        aw = "created_at >= %s AND created_at < %s" + (" AND asset_id = %s" if asset_id else "")
        out = {"start": start, "end": end, "asset_id": asset_id, "truncated": truncated,
               "summary": sorted(summary.values(), key=lambda x: (-x["critical"], -x["warning"], x["asset_id"])),
               "alerts": db.rows(f"SELECT id, severity, asset_id, subject, created_at, acknowledged_at FROM notifications "
                                 f"WHERE channel = 'dashboard' AND {aw} ORDER BY created_at DESC LIMIT 500", *args),
               "runs": db.rows(f"SELECT run_id, asset_id, status, route, priority, started_by, created_at FROM runs "
                               f"WHERE {aw} ORDER BY created_at DESC LIMIT 200", *args),
               "work_orders": db.rows(f"SELECT work_order, asset_id, priority, title, cmms_status, created_at FROM work_orders "
                                      f"WHERE {aw} ORDER BY created_at DESC LIMIT 200", *args)}
        if asset_id:
            a = db.asset(asset_id)
            out["readings"] = readings
            out["thresholds"] = {m: r.model_dump() for m, r in db.category(a.category).metrics.items()}
            out["asset"] = a.model_dump()
        return out

    @app.get("/api/governance")
    def governance(_: dict = Depends(user)) -> dict:
        """One screen of evidence that the controls work: the ladder and its thresholds, where drafts ended up and
        why, what the guardrails stopped, what the planners decided, alert delivery, cost by tier, the latest
        evaluation, and whether telemetry reaches TrustPlane."""
        runs_ = db.rows("SELECT run_id, asset_id, status, route, confidence, attempts, usage, decision, wait_ms, "
                        "priority, created_at FROM runs ORDER BY created_at DESC")
        by_route: dict[str, int] = {}
        escalations: dict[str, int] = {}
        cost: dict[str, dict] = {}
        for r in runs_:
            key = r["route"] or ("refused" if r["status"] == "refused" else r["status"])
            by_route[key] = by_route.get(key, 0) + 1
            for a in r["attempts"] or []:
                if a["action"] in ("escalate", "human"):
                    label = f"{a['tier']}: " + (", ".join(a["issues"]) or a["outcome"].replace("_", " "))
                    escalations[label] = escalations.get(label, 0) + 1
            for t, u in ((r["usage"] or {}).get("by_tier") or {}).items():
                c = cost.setdefault(t, {"calls": 0, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0})
                for k in c:
                    c[k] = round(c[k] + (u.get(k) or 0), 6)
        frontier_runs = [r for r in runs_ if r["route"] == "frontier" and (r["usage"] or {}).get("by_tier", {}).get("frontier")]
        per_frontier = (sum(r["usage"]["by_tier"]["frontier"]["cost_usd"] for r in frontier_runs) / len(frontier_runs)
                        if frontier_runs else None)
        local_ok = by_route.get("local", 0)
        blocked = db.rows("""SELECT i->>'code' AS code, count(*) AS n FROM run_events e,
                             jsonb_array_elements(e.event->'data'->'issues') i
                             WHERE e.event->>'phase' = 'reconcile' AND i->>'severity' = 'blocking'
                             GROUP BY 1 ORDER BY 2 DESC""")
        decisions = db.row("""SELECT count(*) FILTER (WHERE decision = 'approve') AS approved,
                              count(*) FILTER (WHERE decision = 'correct') AS corrected,
                              count(*) FILTER (WHERE decision = 'reject') AS rejected,
                              count(*) FILTER (WHERE status = 'awaiting_approval') AS pending,
                              percentile_cont(0.5) WITHIN GROUP (ORDER BY wait_ms) AS median_wait_ms FROM runs""")
        alerts = {r["status"]: r["n"] for r in db.rows(
            "SELECT status, count(*) AS n FROM notifications WHERE channel = 'email' GROUP BY status")}
        alerts["open_on_dashboard"] = db.row("SELECT count(*) AS n FROM notifications WHERE channel = 'dashboard' "
                                             "AND acknowledged_at IS NULL")["n"]
        evals = []
        for f in sorted((settings.data_dir.parent / "reports").glob("eval-*.json")):
            try:
                e = json.loads(f.read_text(encoding="utf-8"))
                evals.append({"file": f.name, "model": e["model"], "llm": e["llm"], "trials": e["trials"],
                              "cases": e["cases"], "generated_at": e["generated_at"], "criteria": e["criteria"],
                              "flaky_cases": e["flaky_cases"], "cost_usd": e["cost_usd"],
                              "routes": e.get("routes")})
            except (OSError, ValueError, KeyError):
                continue
        recent = [{k: r[k] for k in ("run_id", "asset_id", "status", "route", "confidence", "decision", "priority",
                                     "created_at")} | {"attempts": [{k: a.get(k) for k in
                                     ("tier", "model", "outcome", "confidence", "threshold", "action", "reason")}
                                     for a in r["attempts"] or []]} for r in runs_[:12]]
        return {
            "ladder": [{**t.describe(), "threshold": policy.accept.get(t.name, policy.accept.get("model")),
                        "by_priority": policy.by_priority.get(t.name, {})} for t in tiers],
            "policy": policy.as_dict(), "demo_model": [t.name for t in tiers] == ["demo"],
            "runs": {"total": len(runs_), "by_route": by_route}, "escalations": escalations,
            "guardrails": {"refused_requests": by_route.get("refused", 0), "blocking_issues": blocked},
            "hitl": decisions, "alerts": alerts,
            "cost": {"by_tier": cost, "frontier_cost_per_run_usd": per_frontier,
                     "estimated_frontier_cost_avoided_usd": round(per_frontier * local_ok, 6) if per_frontier else None},
            "evals": evals, "telemetry": {**gov.status(), "otlp": bool(settings.trustplane_url and settings.trustplane_token)},
            "recent": recent,
        }

    @app.get("/api/reconciliation/rules")
    def reconciliation_rules(asset_id: str, _: dict = Depends(user)) -> dict:
        if db.asset(asset_id) is None:
            raise HTTPException(404, f"No asset {asset_id}.")
        return {**rule_card(db, asset_id, settings.now), "policy": policy.as_dict()}

    @app.get("/api/categories")
    def categories(_: dict = Depends(user)) -> list[dict]:
        return db.rows("SELECT * FROM machine_categories ORDER BY category")

    last_scan = {"t": 0.0}

    @app.post("/api/scan")
    def rescan(_: dict = Depends(need("planner"))) -> dict:
        if time.time() - last_scan["t"] < 10:
            raise HTTPException(429, "A scan ran less than 10 seconds ago.")
        last_scan["t"] = time.time()
        found = anomaly.scan(db, settings.now)
        return {"findings": len(found), "raised": notifier.alert(found)}

    @app.post("/api/alerts/ack-all")
    def ack_all(u: dict = Depends(need("planner"))) -> dict:
        n = db.rows("UPDATE notifications SET acknowledged_at = now() WHERE channel = 'dashboard' "
                    "AND acknowledged_at IS NULL RETURNING id")
        log.info("%d alerts acknowledged by %s", len(n), u["name"])
        return {"acknowledged": len(n)}

    @app.post("/api/alerts/{alert_id}/ack")
    def ack(alert_id: int, u: dict = Depends(need("planner"))) -> dict:
        r = db.row("UPDATE notifications SET acknowledged_at = now() WHERE id = %s AND channel = 'dashboard' "
                   "AND acknowledged_at IS NULL RETURNING id", alert_id)
        if not r:
            raise HTTPException(404, "No open alert with that id.")
        log.info("alert %s acknowledged by %s", alert_id, u["name"])
        return {"acknowledged": alert_id}

    # --- runs (SSE) -----------------------------------------------------------------------------------------------
    def sse(gen):
        def stream():
            try:
                for ev in gen:
                    yield "data: " + json.dumps(ev, default=str) + "\n\n"
            except Exception as e:  # visible to the operator instead of a silently broken stream
                log.exception("run stream failed")
                yield "data: " + json.dumps({"phase": "run", "status": "error", "detail": f"{type(e).__name__}"}) + "\n\n"
        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.post("/api/runs/stream")
    def start(body: RunRequest, u: dict = Depends(need("planner"))):
        if db.asset(body.asset_id) is None:
            raise HTTPException(404, f"No asset {body.asset_id}.")
        return sse(pipeline.start(uuid.uuid4().hex[:12], body.asset_id, body.request, u["name"]))

    def run_view(run_id: str) -> dict:
        row = db.row("SELECT * FROM runs WHERE run_id = %s", run_id)
        if not row:
            raise HTTPException(404, f"No run {run_id}.")
        st = pipeline.state(run_id) or {}
        events = [r["event"] for r in db.rows("SELECT event FROM run_events WHERE run_id = %s ORDER BY seq", run_id)]
        return {**row, "draft": st.get("draft"), "issues": st.get("issues", []), "work_order": st.get("work_order"),
                "can_approve": row["status"] == "awaiting_approval" and not has_blocking(st.get("issues", [])),
                "events": events}

    @app.get("/api/runs")
    def runs(_: dict = Depends(user)) -> list[dict]:
        return db.rows("SELECT run_id, asset_id, status, priority, started_by, created_at FROM runs "
                       "ORDER BY created_at DESC LIMIT 50")

    @app.get("/api/runs/{run_id}")
    def get_run(run_id: str, _: dict = Depends(user)) -> dict:
        return run_view(run_id)

    @app.post("/api/runs/{run_id}/decision/stream")
    def decide(run_id: str, body: Decision, u: dict = Depends(need("planner"))):
        current = run_view(run_id)
        if current["status"] != "awaiting_approval":
            raise HTTPException(409, f"Run is {current['status']}; no decision is pending.")
        if not (pipeline.state(run_id) or {}).get("waiting"):
            raise HTTPException(409, "The run is not paused at the approval step.")
        if body.decision == "approve" and not current["can_approve"]:
            raise HTTPException(409, "The draft has blocking issues; reject it or start a new run.")
        # Claim the decision atomically: of two concurrent decisions on the same run, only one gets the row.
        if not db.row("UPDATE runs SET status = 'deciding', updated_at = now() WHERE run_id = %s "
                      "AND status = 'awaiting_approval' RETURNING run_id", run_id):
            raise HTTPException(409, "Another decision on this run is already in progress.")
        return sse(pipeline.resume(run_id, {**body.model_dump(exclude_none=True), "by": u["name"]}))

    @app.post("/api/runs/{run_id}/continue/stream")
    def continue_run(run_id: str, _: dict = Depends(need("planner"))):
        if run_view(run_id)["status"] != "interrupted":
            raise HTTPException(409, "Only an interrupted run can be continued.")
        if not db.row("UPDATE runs SET status = 'running', updated_at = now() WHERE run_id = %s "
                      "AND status = 'interrupted' RETURNING run_id", run_id):
            raise HTTPException(409, "The run is already being continued.")
        return sse(pipeline.resume_interrupted(run_id))

    @app.get("/api/runs/{run_id}/trace")
    def run_trace(run_id: str, _: dict = Depends(user)) -> dict:
        tid = run_view(run_id)["trace_id"]
        spans = [{"name": s.name, "ms": round((s.end_time - s.start_time) / 1e6, 1), "attributes": dict(s.attributes)}
                 for s in finished_spans() if format(s.context.trace_id, "032x") == tid]
        return {"trace_id": tid, "spans": spans}

    # --- work orders ----------------------------------------------------------------------------------------------
    @app.get("/api/work-orders")
    def work_orders(_: dict = Depends(user)) -> list[dict]:
        return db.rows("SELECT work_order, run_id, asset_id, priority, title, approved_by, cmms_status, created_at "
                       "FROM work_orders ORDER BY created_at DESC")

    @app.get("/api/work-orders/{number}")
    def work_order(number: str, _: dict = Depends(user)) -> dict:
        r = db.row("SELECT * FROM work_orders WHERE work_order = %s", number)
        if not r:
            raise HTTPException(404, f"No work order {number}.")
        return r

    # --- admin ----------------------------------------------------------------------------------------------------
    @app.get("/api/admin/recipients")
    def recipients(_: dict = Depends(need("admin"))) -> list[dict]:
        return db.rows("SELECT id, name, email, min_severity, created_at FROM alert_recipients ORDER BY id")

    @app.post("/api/admin/recipients", status_code=201)
    def add_recipient(body: Recipient, u: dict = Depends(need("admin"))) -> dict:
        try:
            r = db.row("INSERT INTO alert_recipients (name, email, min_severity) VALUES (%s,%s,%s) "
                       "RETURNING id, name, email, min_severity", body.name, body.email, body.min_severity)
        except UniqueViolation:
            raise HTTPException(409, f"{body.email} is already registered.") from None
        log.info("recipient %s added by %s", body.email, u["name"])
        return r

    @app.delete("/api/admin/recipients/{rid}")
    def delete_recipient(rid: int, _: dict = Depends(need("admin"))) -> dict:
        if not db.row("DELETE FROM alert_recipients WHERE id = %s RETURNING id", rid):
            raise HTTPException(404, "No such recipient.")
        return {"deleted": rid}

    @app.get("/api/notifications")
    def notifications(_: dict = Depends(need("planner"))) -> list[dict]:
        return db.rows("SELECT id, channel, recipient, severity, asset_id, subject, status, error, created_at, "
                       "acknowledged_at FROM notifications ORDER BY id DESC LIMIT 100")

    @app.get("/api/admin/config")
    def config_view(_: dict = Depends(need("admin"))) -> dict:
        """What the running instance is configured with (from .env / the environment); secrets as set / not set."""
        return {"ladder": [t.describe() for t in tiers],
                "anthropic_api_key": "set" if settings.anthropic_configured else "not set",
                "local_llm": settings.local_base_url or "not set",
                "clock": "live" if settings.live else f"pinned at {settings.now}",
                "smtp": {"host": settings.smtp_host or "not set", "user": settings.smtp_user or "not set",
                         "password": "set" if settings.smtp_password else "not set",
                         "from": settings.smtp_from or settings.smtp_user or "not set"},
                "public_url": settings.public_url, "email_cooldown_min": settings.email_cooldown_min,
                "alert_emails_from_env": [e for _, e in parse_recipients(settings.alert_emails)],
                "trustplane": settings.trustplane_url or "not set", "deployment": settings.deployment}

    @app.post("/api/admin/test-alert")
    def test_alert(u: dict = Depends(need("admin"))) -> dict:
        """Send a sample critical alert e-mail to every registered recipient (to see how it looks)."""
        recips = db.rows("SELECT email FROM alert_recipients ORDER BY id")
        if not recips:
            raise HTTPException(409, "Register a recipient first.")
        subject = "[CRITICAL] TEST P-101 vibration_rms = 8.4 mm/s"
        lines = ["This is a test of the alert e-mail, sent from the Admin screen by " + u["name"] + ".",
                 "A real alert names the asset, the reading, when it was taken and the threshold it crossed."]
        from .notify import render_html
        out = []
        for r in recips:
            status, error = "disabled", None
            if notifier.send is not None:
                try:
                    notifier.send(r["email"], subject, "\n".join(lines), render_html("critical", subject, lines,
                                                                                    settings.public_url), "critical")
                    status = "sent"
                except Exception as e:
                    status, error = "failed", f"{type(e).__name__}: {e}"[:300]
            db.exec("INSERT INTO notifications (dedupe_key, channel, recipient, severity, asset_id, subject, status, error) "
                    "VALUES (%s, 'email', %s, 'critical', 'P-101', %s, %s, %s)", f"test:{uuid.uuid4().hex}", r["email"],
                    subject, status, error)
            out.append({"to": r["email"], "status": status, "error": error})
        return {"results": out}

    @app.get("/api/admin/faults")
    def get_faults(_: dict = Depends(need("admin"))) -> dict:
        return faults.as_dict()

    @app.put("/api/admin/faults")
    def set_faults(body: dict, _: dict = Depends(need("admin"))) -> dict:
        for k, v in body.items():
            if not hasattr(faults, k):
                raise HTTPException(422, f"Unknown fault {k!r}.")
            setattr(faults, k, bool(v))
        return faults.as_dict()

    # --- single-page app ------------------------------------------------------------------------------------------
    if settings.brand_dir.is_dir():
        app.mount("/branding", StaticFiles(directory=settings.brand_dir), name="branding")
    if DIST.exists():
        app.mount("/assets", StaticFiles(directory=DIST / "assets"), name="assets")

        @app.get("/{path:path}", include_in_schema=False)
        def spa(path: str) -> FileResponse:
            return FileResponse(DIST / "index.html")

    return app
