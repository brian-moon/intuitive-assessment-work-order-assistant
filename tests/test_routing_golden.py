"""Routing golden set: every branch of the model ladder (local → frontier → planner), driven by stand-in models that
fail in one specific way each. Cases: ``evals/routing_golden.jsonl``. Deterministic, no tokens; the live evaluation
measures how often the real models take each branch."""
from __future__ import annotations

import json

import pytest
from conftest import run

from app.config import ROOT
from app.llm import ScriptedLLM, Tier
from app.trustplane import GovernanceSink

CASES = [json.loads(line) for line in (ROOT / "evals" / "routing_golden.jsonl").read_text(encoding="utf-8").splitlines()
         if line.strip()]


class RecordingSink(GovernanceSink):
    def __init__(self):
        super().__init__()
        self.events: list[dict] = []

    def emit(self, event_type, trace_id, attributes, actor=None):
        ev = super().emit(event_type, trace_id, attributes, actor)
        self.events.append(ev)
        return ev


@pytest.mark.parametrize("case", CASES, ids=[c["id"] for c in CASES])
def test_routing_golden(make_client, case):
    models = {name: ScriptedLLM(case.get(name), model=f"{name}-stand-in") for name in case["ladder"]}
    gov = RecordingSink()
    client = make_client(tiers=[Tier(n, models[n]) for n in case["ladder"]], gov=gov)
    for k, v in case.get("faults", {}).items():
        setattr(client.app.state.faults, k, v)
    run_id, evs = run(client, case["asset_id"], case.get("request", "Investigate and draft a work order."))

    path = [f"{e['data']['tier']}:{e['data']['action']}" for e in evs if e["phase"] == "route"]
    assert path == case["path"]
    assert evs[-1]["status"] == case["status"]
    row = client.app.state.db.row("SELECT route, attempts FROM runs WHERE run_id = %s", run_id)
    assert row["route"] == case["route"]
    assert len(row["attempts"]) == len(case["path"])
    if "can_approve" in case:
        waiting = [e for e in evs if e["phase"] == "approval" and e["status"] == "waiting"]
        assert waiting and waiting[-1]["data"]["can_approve"] is case["can_approve"]
        assert waiting[-1]["data"]["route"] == case["route"]
    for name in ("local", "frontier"):
        if f"{name}_calls" in case and name in models:
            assert models[name].calls == case[f"{name}_calls"]

    # every ladder decision is a governance event, joined to the run's trace
    routes = [e for e in gov.events if e["event_type"] == "confidence.route"]
    assert [f"{e['attributes']['gov.tier']}" for e in routes] == [p.split(":")[0] for p in case["path"]]
    assert all(e["attributes"]["run.id"] == run_id and e["trace_id"] != "untraced" for e in routes)
    escalations = [e for e in routes if e["attributes"]["gov.route"] == "escalate_model"]
    assert all(e["attributes"]["gov.model_escalation"]["to"] == "frontier-stand-in" for e in escalations)
    if case["status"] == "refused":
        assert [e["event_type"] for e in gov.events] == ["guardrail.trigger"]


def test_every_flaw_and_ladder_is_covered():
    flaws = {c.get(t) for c in CASES for t in ("local", "frontier")} - {None}
    assert flaws == set(ScriptedLLM.FLAWS)
    assert {tuple(c["ladder"]) for c in CASES} == {("local", "frontier"), ("frontier",), ("local",), ("demo",)}
    assert {c["route"] for c in CASES} == {"local", "frontier", "human", "demo", None}
