# For future development

Files here are not used by the running demo. They record the next steps the design already allows for:

- `otel-collector.yaml`: an OpenTelemetry Collector that receives the app's spans (OTLP/HTTP) and forwards them to a
  tracing backend, deleting prompt text on the way. The app exports to it when `OTEL_EXPORTER_OTLP_ENDPOINT` is set.
