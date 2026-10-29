"""HTTP API and the single-page UI.

Roles come from the ``X-API-Key`` header: a *viewer* can read assets and runs; only a *planner* can start a run and
approve or reject a draft. Validation errors are 422 (FastAPI/Pydantic), unknown ids 404, a decision on a run that is
not waiting for one, or an approval of a draft with blocking issues, 409.
"""
from __future__ import annotations

import logging
import sqlite3
import uuid
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from langgraph.checkpoint.sqlite import SqliteSaver

from .agent import build_graph, resume
from .cmms import MockCMMS
from .config import Settings
from .data_access import Repository
from .faults import Faults
from .llm import LLM, make_llm
from .models import Decision, RunRequest
from .tools import Toolbox
from .validation import has_blocking

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
STATIC = Path(__file__).parent / "static"


def create_app(settings: Settings | None = None, llm: LLM | None = None) -> FastAPI:
    settings = settings or Settings()
    repo = Repository.load(settings.data_dir)
    faults = Faults()
    cmms = MockCMMS(faults, settings.cmms_timeout_s)
    toolbox = Toolbox(repo, faults, settings.now, settings.stale_after_hours)
    llm = llm or make_llm(settings.llm, settings.model, settings.max_output_tokens)
    saver = SqliteSaver(sqlite3.connect(settings.db_path, check_same_thread=False))
    graph = build_graph(llm, toolbox, cmms, settings, saver)

    app = FastAPI(title="Maintenance work-order assistant", version="0.1.0")
    app.state.settings, app.state.repo, app.state.faults, app.state.cmms = settings, repo, faults, cmms

    def role(x_api_key: str | None = Header(default=None)) -> str:
        if not x_api_key or x_api_key not in settings.api_keys:
            raise HTTPException(401, "Missing or invalid API key.")
        return settings.api_keys[x_api_key]

    def planner(r: str = Depends(role)) -> str:
        if r != "planner":
            raise HTTPException(403, "Only a planner can do this.")
        return r

    def view(run_id: str) -> dict:
        snap = graph.get_state({"configurable": {"thread_id": run_id}})
        if not snap.values:
            raise HTTPException(404, f"No run {run_id}.")
        s = snap.values
        waiting = bool(snap.next) and snap.next[0] == "approval"
        return {
            "run_id": run_id, "asset_id": s["asset_id"], "request": s["request"],
            "status": "awaiting_approval" if waiting else s.get("status", "running"),
            "reason": s.get("reason", ""), "draft": s.get("draft"), "issues": s.get("issues", []),
            "can_approve": waiting and not has_blocking(s.get("issues", [])),
            "work_order": s.get("work_order"), "usage": s.get("usage", {}), "events": s.get("events", []),
        }

    @app.get("/health")
    def health() -> dict:
        return {"ok": True, "llm": llm.model, "rejected_rows": len(repo.quarantine)}

    @app.get("/api/assets")
    def assets(_: str = Depends(role)) -> dict:
        return {"assets": [repo.asset_summary(a, settings.now, settings.stale_after_hours) for a in sorted(repo.assets)],
                "rejected_rows": [{"source": q.source, "asset_id": q.row.get("asset_id"), "reason": q.reason}
                                  for q in repo.quarantine]}

    @app.post("/api/runs", status_code=201)
    def start_run(body: RunRequest, _: str = Depends(planner)) -> dict:
        if body.asset_id not in repo.assets:
            raise HTTPException(404, f"No asset {body.asset_id}.")
        run_id = uuid.uuid4().hex[:12]
        graph.invoke({"run_id": run_id, "asset_id": body.asset_id, "request": body.request, "events": []},
                     config={"configurable": {"thread_id": run_id}})
        return view(run_id)

    @app.get("/api/runs/{run_id}")
    def get_run(run_id: str, _: str = Depends(role)) -> dict:
        return view(run_id)

    @app.post("/api/runs/{run_id}/decision")
    def decide(run_id: str, body: Decision, who: str = Depends(planner)) -> dict:
        current = view(run_id)
        if current["status"] != "awaiting_approval":
            raise HTTPException(409, f"Run is {current['status']}; no decision is pending.")
        if body.decision == "approve" and not current["can_approve"]:
            raise HTTPException(409, "The draft has blocking issues; reject it or start a new run.")
        resume(graph, run_id, {**body.model_dump(exclude_none=True), "by": who})
        return view(run_id)

    @app.put("/api/faults")
    def set_faults(body: dict, _: str = Depends(planner)) -> dict:
        for k, v in body.items():
            if not hasattr(faults, k):
                raise HTTPException(422, f"Unknown fault {k!r}.")
            setattr(faults, k, bool(v))
        return faults.as_dict()

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(STATIC / "index.html")

    return app
