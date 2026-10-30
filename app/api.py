"""HTTP API: auth, dashboard data, the streamed pipeline (SSE), approvals, alerts, admin and work orders.

Roles (JWT from ``POST /api/login``): *viewer* reads; *planner* runs the pipeline, decides, acknowledges alerts;
*admin* does everything a planner does and manages alert recipients and fault injection. Validation errors are 422,
unknown ids 404, a decision on a run that is not waiting for one (or an approval of a draft with blocking issues) 409.
"""
from __future__ import annotations

import hmac
import json
import logging
import time
import uuid
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
from .config import Settings
from .db import Database
from .faults import Faults
from .llm import LLM, Tier, make_tiers
from .models import Decision, Login, Recipient, RunRequest
from .notify import Notifier, smtp_sender
from .observability import finished_spans, setup
from .pipeline import Pipeline
from .reconcile import has_blocking
from .routing import Policy
from .tools import Toolbox
from .trustplane import GovernanceSink

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("app.api")
DIST = Path(__file__).resolve().parent.parent / "frontend" / "dist"
RANKS = {"viewer": 0, "planner": 1, "admin": 2}


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
        log.info("seeded %s", db.seed(settings.data_dir))
    faults = Faults()
    notifier = Notifier(db, faults, settings.public_url, send if send is not None else smtp_sender(settings))
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
    pipeline = Pipeline(db, tiers, Toolbox(db, faults, settings.now), notifier, MockCMMS(faults, settings.cmms_timeout_s),
                        settings, saver, tracer, policy, gov)
    log.info("model ladder: %s -> planner", " -> ".join(f"{t.name}:{t.llm.model}" for t in tiers))

    app = FastAPI(title="Maintenance work-order assistant", version="0.2.0")
    app.state.settings, app.state.db, app.state.faults, app.state.pipeline = settings, db, faults, pipeline
    app.state.notifier, app.state.tiers, app.state.policy, app.state.gov = notifier, tiers, policy, gov

    def close() -> None:   # release both connection pools (tests create many apps; a server process exits instead)
        gov.close()
        saver_pool.close()
        db.close()
    app.state.close = close

    # --- auth -----------------------------------------------------------------------------------------------------
    def user(authorization: str | None = Header(default=None)) -> dict:
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(401, "Sign in first.")
        try:
            claims = jwt.decode(authorization[7:], settings.jwt_secret, algorithms=["HS256"])
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
                           settings.jwt_secret, algorithm="HS256")
        return {"token": token, "user": body.username, "role": entry[1]}

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
                "email": "enabled" if notifier.send is not None else "disabled"}

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

    @app.put("/api/admin/faults")
    def set_faults(body: dict, _: dict = Depends(need("admin"))) -> dict:
        for k, v in body.items():
            if not hasattr(faults, k):
                raise HTTPException(422, f"Unknown fault {k!r}.")
            setattr(faults, k, bool(v))
        return faults.as_dict()

    # --- single-page app ------------------------------------------------------------------------------------------
    if DIST.exists():
        app.mount("/assets", StaticFiles(directory=DIST / "assets"), name="assets")

        @app.get("/{path:path}", include_in_schema=False)
        def spa(path: str) -> FileResponse:
            return FileResponse(DIST / "index.html")

    return app
