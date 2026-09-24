# policyengine-simulation-observability

Shared API v1 observability configuration for the simulation entry service,
Modal gateway, and Modal executor. Each process creates and owns a
`policyengine-observability` 3.x runtime with an explicit service name,
role, platform, and environment.

Cloud Run services emit one line of structured JSON to standard output. Modal
services add a bounded Cloud Logging destination when
`OBSERVABILITY_LOGGING_PROJECT_ID` and `OBSERVABILITY_LOG_NAME` are configured.
The log destination is independent from the trace and metric destination,
which uses the standard `OTEL_EXPORTER_OTLP_*` settings. Google authentication
is enabled only when the corresponding workload identity and audience values
are supplied.

The deployment workflow passes these settings from GitHub environment or
repository variables. This library contains no Google Cloud project, log name,
collector URL, service account, or identity provider default.

`X-PolicyEngine-Request-Id`, W3C trace headers, and the bounded internal
`_observability_context` object provide correlation across HTTP and Modal job
dispatch. Request bodies, reform definitions, household data, credentials, and
person-level values are excluded from telemetry.
