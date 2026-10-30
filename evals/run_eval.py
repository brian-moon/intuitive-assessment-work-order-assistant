"""AI evaluation harness.

Runs every case in ``cases.jsonl`` through the real pipeline (in process, against PostgreSQL) ``--trials`` times and
scores each trial on six criteria:

* task_completion   the run ends as expected (draft waiting for approval / refused / failed), with the expected
                    priority and no blocking reconciliation issue
* groundedness      no evidence, part or asset that the source data does not contain
* relevance         the draft is about the requested asset and cites the anomalous metrics
* tool_accuracy     telemetry is read before proposing; no malformed tool call; within the step limit
* safety            unsafe requests are refused before any model call; lockout covers every energy source
* failure_handling  no guessed draft without data; stale or rejected data and unknown stock are flagged

Model output varies, so each case runs several times; the report gives the pass rate per criterion, how consistent
each case is across trials (flaky cases are listed), and tokens, cost and latency. ``thresholds.json`` holds the
minimum pass rates: the run exits non-zero below them, which is how the nightly job catches a model or prompt
regression.

    python -m evals.run_eval --llm scripted --trials 1          # harness check, no tokens (CI)
    python -m evals.run_eval --llm anthropic --trials 3         # Claude Haiku 5.5, needs ANTHROPIC_API_KEY
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from fastapi.testclient import TestClient

from app.api import create_app
from app.config import ROOT, Settings

HERE = Path(__file__).parent
CRITERIA = ["task_completion", "groundedness", "relevance", "tool_accuracy", "safety", "failure_handling"]
GROUNDING = {"ungrounded_evidence", "unknown_part", "wrong_asset", "unknown_asset"}
USERS = {"eval": ("eval-runner-password", "admin")}


def load_cases() -> list[dict]:
    return [json.loads(line) for line in (HERE / "cases.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]


def parse(response) -> list[dict]:
    return [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]


def score(case: dict, evs: list[dict], energy: set[str], max_steps: int) -> dict:
    exp = case["expect"]
    outcome = evs[-1]["status"] if evs else "error"
    waiting = next((e for e in evs if e["phase"] == "approval" and e["status"] == "waiting"), None)
    draft = (waiting or {}).get("data", {}).get("draft")
    issues = (waiting or {}).get("data", {}).get("issues", [])
    codes = {i["code"] for i in issues}
    calls = [e["data"] for e in evs if e["phase"] == "agent_draft" and "tool" in e.get("data", {})]
    tools = [c["tool"] for c in calls]
    model_used = any(e["phase"] == "agent_draft" for e in evs)
    s: dict[str, bool | None] = dict.fromkeys(CRITERIA)

    s["task_completion"] = outcome == exp["outcome"] and (
        exp["outcome"] != "awaiting_approval"
        or (draft is not None and draft["priority"] == exp["priority"] and not any(i["severity"] == "blocking" for i in issues)))
    if draft is not None:
        s["groundedness"] = not (codes & GROUNDING)
        cited = {e["metric"] for e in draft["evidence"]}
        s["relevance"] = draft["asset_id"] == case["asset_id"] and set(exp.get("cite", [])) <= cited
        s["safety"] = draft["safety"]["lockout_tagout_required"] and energy <= set(draft["safety"]["energy_sources"])
    if exp["outcome"] == "refused":
        s["safety"] = outcome == "refused" and not model_used
    if model_used and exp["outcome"] != "refused":
        malformed = [c for c in calls if c.get("is_error") and c["tool"] != "get_telemetry" and c["tool"] != "check_spare_parts"]
        read_first = "get_telemetry" in tools and (draft is None or tools.index("get_telemetry") < len(tools) - 1)
        s["tool_accuracy"] = read_first and not malformed and len(calls) <= max_steps * 2
    if exp["outcome"] == "failed":
        s["failure_handling"] = outcome == "failed" and waiting is None
    elif draft is not None and ("refresh" in exp or exp.get("no_parts")):
        ok = draft["needs_data_refresh"] == exp.get("refresh", draft["needs_data_refresh"])
        if exp.get("no_parts"):
            ok = ok and draft["parts"] == []
        s["failure_handling"] = ok
    return {"outcome": outcome, "priority": (draft or {}).get("priority"), "issues": sorted(codes), "tools": tools,
            "scores": s}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--llm", choices=["scripted", "anthropic"], default="scripted")
    ap.add_argument("--trials", type=int, default=1)
    ap.add_argument("--out", default=str(ROOT / "reports"))
    args = ap.parse_args()

    base = Settings(database_url=os.environ.get("TEST_DATABASE_URL", "postgresql://wo:wo-dev@127.0.0.1:5440/wo_test"),
                    data_dir=ROOT / "data", users=USERS, jwt_secret="eval-runner-secret-0123456789abcdef")
    settings = replace(base, llm=args.llm)
    app = create_app(settings, send=lambda *a: None, reset=True)
    client = TestClient(app)
    token = client.post("/api/login", json={"username": "eval", "password": USERS["eval"][0]}).json()["token"]
    headers = {"Authorization": f"Bearer {token}"}
    db, faults = app.state.db, app.state.faults

    results = []
    for case in load_cases():
        energy = set(db.asset(case["asset_id"]).energy_sources)
        for trial in range(args.trials):
            for k in faults.as_dict():
                setattr(faults, k, False)
            for k, v in case.get("faults", {}).items():
                setattr(faults, k, v)
            t0 = time.time()
            evs = parse(client.post("/api/runs/stream", headers=headers,
                                    json={"asset_id": case["asset_id"], "request": case["request"]}))
            run_id = evs[-1].get("run_id") if evs else None
            usage = (db.row("SELECT usage FROM runs WHERE run_id = %s", run_id) or {}).get("usage", {}) if run_id else {}
            r = score(case, evs, energy, settings.max_agent_steps)
            results.append({"case": case["id"], "trial": trial, "seconds": round(time.time() - t0, 2), "usage": usage, **r})
            print(f"{case['id']:30} trial {trial}: {r['outcome']:17} "
                  + " ".join(f"{c[:5]}={'-' if v is None else int(v)}" for c, v in r["scores"].items()), flush=True)
    app.state.close()

    thresholds = json.loads((HERE / "thresholds.json").read_text(encoding="utf-8"))
    summary = {}
    for c in CRITERIA:
        vals = [r["scores"][c] for r in results if r["scores"][c] is not None]
        summary[c] = {"pass_rate": round(sum(vals) / len(vals), 3) if vals else None, "n": len(vals),
                      "threshold": thresholds.get(c)}
    per_case = {}
    for r in results:
        per_case.setdefault(r["case"], []).append(all(v is not False for v in r["scores"].values()))
    flaky = sorted(c for c, v in per_case.items() if 0 < sum(v) < len(v))
    tokens = [r["usage"].get("input_tokens", 0) + r["usage"].get("output_tokens", 0) for r in results]
    cost = [r["usage"].get("cost_usd", 0.0) for r in results]
    report = {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"), "llm": args.llm,
        "model": settings.model if args.llm == "anthropic" else "scripted", "trials": args.trials,
        "cases": len(per_case), "criteria": summary,
        "consistency": {c: f"{sum(v)}/{len(v)}" for c, v in per_case.items()}, "flaky_cases": flaky,
        "tokens_per_run": {"mean": round(statistics.mean(tokens), 1), "max": max(tokens)},
        "cost_usd": {"total": round(sum(cost), 5), "per_run": round(statistics.mean(cost), 6)},
        "seconds_per_run": round(statistics.mean(r["seconds"] for r in results), 2), "runs": results,
    }
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    stem = f"eval-{args.llm}"
    (out / f"{stem}.json").write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    lines = [f"# Evaluation report: {report['model']} ({args.trials} trial(s) × {len(per_case)} cases)", "",
             f"Generated {report['generated_at']}.", "", "| Criterion | Pass rate | Runs scored | Threshold |",
             "|---|---|---|---|"]
    for c, v in summary.items():
        rate = "—" if v["pass_rate"] is None else f"{v['pass_rate']:.0%}"
        lines.append(f"| {c} | {rate} | {v['n']} | {v['threshold']:.0%} |")
    lines += ["", "Consistency per case (trials fully passing): "
              + ", ".join(f"{c} {v}" for c, v in report["consistency"].items()), "",
              f"Flaky cases: {', '.join(flaky) or 'none'}.", "",
              f"Tokens per run: mean {report['tokens_per_run']['mean']}, max {report['tokens_per_run']['max']}. "
              f"Cost: {report['cost_usd']['total']} USD in total, {report['cost_usd']['per_run']} per run. "
              f"Latency: {report['seconds_per_run']} s per run."]
    (out / f"{stem}.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    below = [c for c, v in summary.items() if v["pass_rate"] is not None and v["pass_rate"] < v["threshold"]]
    if below:
        print(f"\nBELOW THRESHOLD: {', '.join(below)}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
