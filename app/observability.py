"""OpenTelemetry tracing.

One trace per run, one span per pipeline phase, and a child span per model call and tool call. Model spans carry the
OpenTelemetry GenAI semantic-convention attributes (``gen_ai.system``, ``gen_ai.request.model``,
``gen_ai.usage.input_tokens`` ...), so any OTLP backend (Jaeger, Grafana Tempo, Arize Phoenix, Langfuse) can show
cost and latency per step.

Export is opt-in. With ``TRUSTPLANE_URL`` and ``TRUSTPLANE_TOKEN`` set, spans go to TrustPlane's OTLP endpoint (and
governance events to its event API, see trustplane.py); with ``OTEL_EXPORTER_OTLP_ENDPOINT`` set, also to that
collector. Without either, spans are kept in memory only, and the trace id is still written into the run's
execution history so a run can be matched to its trace later.
``future/otel-collector.yaml`` is a starting collector config (not used by the demo).
"""
from __future__ import annotations

import os

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

_memory = InMemorySpanExporter()
_configured = False


def setup(trustplane_url: str = "", trustplane_token: str = "", deployment: str = "local") -> trace.Tracer:
    """With TRUSTPLANE_URL/TOKEN set, spans go to TrustPlane's OTLP endpoint; OTEL_EXPORTER_OTLP_ENDPOINT, if set,
    adds any other collector."""
    global _configured
    if not _configured:
        provider = TracerProvider(resource=Resource.create({"service.name": "workorder-assistant",
                                                            "deployment": deployment}))
        provider.add_span_processor(SimpleSpanProcessor(_memory))
        if trustplane_url and trustplane_token:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

            provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(
                endpoint=f"{trustplane_url}/v1/otlp/v1/traces", headers={"Authorization": f"Bearer {trustplane_token}"})))
        if os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"):
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

            provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
        trace.set_tracer_provider(provider)
        _configured = True
    return trace.get_tracer("workorder-assistant")


def finished_spans():
    """Spans kept in memory (used by tests and the diagnostics endpoint)."""
    return _memory.get_finished_spans()


def trace_id_hex(span) -> str:
    return format(span.get_span_context().trace_id, "032x")
