# Observability standard

**Goal:** when something breaks, the owning team knows before customers do and can find the cause in minutes.

## Must

| # | Practice | Why |
|---|---|---|
| O1 | Instrument with OpenTelemetry (traces, metrics, logs) and export through a collector | One vendor-neutral standard across every stack and company |
| O2 | Every service emits the golden signals: latency, traffic, errors, saturation | The minimum needed to know whether a service is healthy |
| O3 | Structured JSON logs carrying `trace_id`, service, version and environment; no personal data | Logs you can join to traces and safely retain |
| O4 | Each customer-facing service has SLOs with an error budget, owned by a named team | Turns "is it reliable enough?" into a number and a decision |
| O5 | Alerts page a human only for SLO burn or user-visible impact, and each links to a runbook | Pages that aren't actionable train people to ignore pages |
| O6 | Blameless postmortem (`templates/postmortem.md`) for every customer-impacting incident | Learning, not blame, is what prevents the next one |

## Baseline resource attributes

```text
service.name, service.version, deployment.environment, service.namespace (team)
```

## For AI and agent features

Also trace each model call: model and version, prompt template version, token counts, latency, tool calls, and the evaluation outcome where one exists. Never log raw prompts or responses that may contain personal data unless they are redacted.
