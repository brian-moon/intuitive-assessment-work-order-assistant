"""Governance events for TrustPlane (``TRUSTPLANE_URL`` + ``TRUSTPLANE_TOKEN``; unset = counted locally, not sent).

Spans already go to TrustPlane over OTLP (observability.py). These are the decisions a span cannot express, in
TrustPlane's schema v0.1, joined to the run's trace by ``trace_id``:

* ``guardrail.trigger``  layer 1: an unsafe request refused before any model call
* ``confidence.route``   layer 5a: each ladder decision. Accepted draft -> ``review_queue`` (planner sign-off),
                         next model -> ``escalate_model`` with ``gov.model_escalation``, planner takes over -> ``reject``
* ``hitl.decision``      layer 5b: the planner's approve / reject / correct (priority changed)
* ``eval.run``, ``eval.case`` from the evaluation harness (``--trustplane``)

Fail-open by design: a background thread posts batches; if TrustPlane is down the run never waits or fails, and lost
events are counted (``status()``, shown on the governance screen).
"""
from __future__ import annotations

import logging
import queue
import threading
import time
import uuid
from datetime import UTC, datetime

import httpx

log = logging.getLogger("app.trustplane")


class GovernanceSink:
    def __init__(self, url: str = "", token: str = "", producer: str = "workorder-assistant", deployment: str = "local",
                 max_queue: int = 5000):
        self.url, self.token, self.producer, self.deployment = url, token, producer, deployment
        self.counts = {"emitted": 0, "sent": 0, "failed_batches": 0, "dropped": 0}
        self.last_error = ""
        self._q: queue.Queue = queue.Queue(maxsize=max_queue)
        self._stop = threading.Event()
        self._thread = None
        if self.enabled:
            self._thread = threading.Thread(target=self._loop, name="trustplane-sink", daemon=True)
            self._thread.start()

    @property
    def enabled(self) -> bool:
        return bool(self.url and self.token)

    def emit(self, event_type: str, trace_id: str | None, attributes: dict, actor: dict | None = None) -> dict:
        ev = {"event_id": str(uuid.uuid4()), "event_type": event_type, "trace_id": trace_id or "untraced",
              "producer": self.producer, "ts": datetime.now(UTC).isoformat(timespec="milliseconds"),
              "attributes": {"deployment": self.deployment, **attributes}, **({"actor": actor} if actor else {})}
        self.counts["emitted"] += 1
        if self.enabled:
            try:
                self._q.put_nowait(ev)
            except queue.Full:
                self.counts["dropped"] += 1
        return ev

    def _post(self, batch: list[dict]) -> None:
        r = httpx.post(f"{self.url}/v1/events", json={"events": batch}, timeout=5.0,
                       headers={"Authorization": f"Bearer {self.token}"})
        r.raise_for_status()

    def _loop(self) -> None:
        while not self._stop.is_set():
            batch = self._drain(wait=2.0)
            if batch:
                self._send(batch)

    def _drain(self, wait: float) -> list[dict]:
        batch = []
        try:
            batch.append(self._q.get(timeout=wait))
            while len(batch) < 200:
                batch.append(self._q.get_nowait())
        except queue.Empty:
            pass
        return batch

    def _send(self, batch: list[dict]) -> None:
        for attempt in range(3):
            try:
                self._post(batch)
                self.counts["sent"] += len(batch)
                return
            except (httpx.HTTPError, OSError) as e:
                self.last_error = f"{type(e).__name__}: {e}"[:200]
                time.sleep(0.5 * (attempt + 1))
        self.counts["failed_batches"] += 1
        self.counts["dropped"] += len(batch)
        log.warning("TrustPlane unreachable; %d governance events dropped (%s)", len(batch), self.last_error)

    def flush(self) -> None:
        """Send what is queued now (eval harness, shutdown)."""
        if self.enabled:
            while batch := self._drain(wait=0.0):
                self._send(batch)

    def close(self) -> None:
        self.flush()
        self._stop.set()

    def status(self) -> dict:
        return {"enabled": self.enabled, "url": self.url or None, "producer": self.producer,
                "deployment": self.deployment, **self.counts, "queued": self._q.qsize(),
                "last_error": self.last_error or None}
