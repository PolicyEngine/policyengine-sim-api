# Observability engineering rules

Read this file before changing request correlation, asynchronous dispatch,
logging, traces, metrics, or runtime stage names.

## Identifier ownership

Use each identifier for its defined scope:

| Identifier | Scope | Created by | Durable |
|---|---|---|---|
| `request_id` | One HTTP request | HTTP instrumentation | No |
| `observability_id` | One complete calculation or report | The first service that accepts the calculation submission | Yes |
| `job_id` | One annual Modal invocation | Modal | Yes, as functional job state |
| `batch_job_id` | One budget window Modal invocation | Modal | Yes, as functional batch state |
| `evaluation_id` | One Stage 12 report | Stage 12 report construction | Yes, as functional report state |
| `simulation_execution_id` | One Stage 12 baseline or reform simulation | Stage 12 coordinator | Yes, as functional simulation state |
| `submission_claim_id` | One API v1 attempt to acquire ownership of a simulation submission | API v1 | Only where the simulation request is stored |

`observability_id` is a canonical UUID string and has diagnostic meaning only.
Do not use it for idempotency, authorization, database identity, routing, or
business logic. Do not create one for health checks, version queries, invalid
requests, missing jobs, or ordinary status requests.

## HTTP transport

Transport `observability_id` only in
`X-PolicyEngine-Observability-Id`. A submission endpoint accepts a valid
incoming value or creates a new value. It binds that value to the active
observability runtime and returns it in the response header.

A status endpoint reads functional state first. If that state has a persisted
`observability_id`, bind and return it. The persisted value takes precedence
over a value supplied on the status request. If an older record has no value,
leave it absent. Never invent an identifier while polling.

Do not read an `observability_id`, `run_id`, `request_id`, or `traceparent`
from a simulation JSON body. The body telemetry model discards these fields.

## Modal transport

Pass captured observability context as the keyword-only
`observability_context` argument to a Modal function:

```python
context = runtime.capture_context()
call = function.spawn(payload, observability_context=context)
```

Keep `payload` limited to calculation inputs and functional metadata. Do not
add `_observability_context` to it.

At the receiving function, validate the separate argument with
`normalize_observability_context` and pass the result to
`runtime.operation(..., remote_context=context)`. The operation scope must
cover the worker call. Child dispatches made inside that scope call
`runtime.capture_context()` again and pass the resulting context through their
own keyword-only argument.

Old Modal function signatures are intentionally unsupported. Deploy the
simulation API before API v1 and stop old workers during the deployment.

## Persistence

Persist `observability_id` beside the functional record used by later status
requests:

- annual runs: job metadata keyed by `job_id`;
- budget windows: seed and current batch state keyed by `batch_job_id`;
- Stage 12 reports: the comparison report row keyed by `evaluation_id`.

Keep this field nullable so records created before this implementation remain
readable. Do not create replacement identifiers for those records.

## Runtime stages

All span names for supported run configurations are defined in
`policyengine_simulation_observability.stages`. Runtime code must import the
appropriate `StagePlan` and call `plan.name(Stage.VALUE)`. Do not add span name
string literals in services or workers.

When adding a run configuration or runtime stage:

1. add it to `RunConfiguration` or `Stage`;
2. add it to the applicable plan in `RUN_STAGE_REGISTRY`;
3. import the plan in runtime code;
4. add focused tests for the new stage and identifier propagation.

## Traces and polling

HTTP instrumentation carries W3C trace context across synchronous service
calls. `capture_context` and `remote_context` carry that context across Modal
dispatch, so worker spans can continue the submission trace.

A later status request is a new trace. Its spans and logs share the persisted
`observability_id` with the original calculation. Queries that measure a full
report must select all telemetry with that identifier and use registered stage
names to break down elapsed time. Do not assume every status request belongs to
the original trace tree.

Stage 12 authoritative and shadow reports use the same `observability_id` as
the production calculation that caused their dispatch. Their functional
`evaluation_id` and simulation execution identifiers remain distinct.

## Failure behavior

Invalid observability configuration must fail during build or deployment
validation. Once a service is serving application traffic, logging, tracing,
metrics, context capture, context restoration, and export failures must not
change a calculation result or HTTP status.

Application boundary helpers must normalize untrusted context and treat
malformed values as absent. Calls that bind context or emit a diagnostic event
must rely on the package's nonthrowing runtime contract or use a local exception
boundary where a package failure could otherwise escape into application code.

Tests must cover:

- creation at each submission boundary;
- propagation through HTTP and Modal keyword arguments;
- persistence and restoration during polling;
- persisted values taking precedence over status request headers;
- old records with null identifiers;
- observability failures leaving application results unchanged.
