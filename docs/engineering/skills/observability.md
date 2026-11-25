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
| `database_table` | The API v1 table holding the row a v2 Modal function call, or a Simulation Entrypoint log record, is about: `household_jobs`, `report_runs` (a report run or a household report run), `simulation_runs`, `cliff_impacts`, `reports` or `simulations` | The code writing the record, as a fixed name | No: telemetry only |
| `database_row_id` | That row's UUID | The simulation service, which creates the row | Yes, as the row's ID |
| `modal_call_id` | One Modal function call of a v2 Modal function | Modal | Only where the row records the call that claimed it; a segment worker's call is recorded nowhere, and its `database_row_id` names the simulation run its simulation worker's call claimed |

Each v2 Modal function's operation (`src/modal/v2_app.py`) names its
`modal_call_id` and, when its call works on a detailed-save row, that row's
`database_table` and `database_row_id`; a US segment worker names the
simulation run its simulation worker records. A segment of a cliff calculation
names no row: it joins the simulation worker's trace, whose operation names the
cliff result, or, if it starts too late to join, links to that trace with a span
link, so its log records then carry another trace ID. The attributes go to the
operation's log records and span only, never to metrics, where each distinct ID
would make a separate series. The observability package drops every attribute whose key
contains `household`, `reform`, `payload` or `person`, among others, so no key
for these IDs names one. Do not use `job_id` for a v2 call: it names one
annual Modal invocation of the v1 executor.

`observability_id` is a canonical UUID string and has diagnostic meaning only.
Do not use it for idempotency, authorization, database identity, routing, or
business logic. Do not create one for health checks, version queries, invalid
requests, missing jobs, or ordinary status requests.

## Standard-library log fields

The observability runtime's standard-library log handler emits only a log
record's `policyengine_attributes`: any other key passed in `extra=` stays on
the Python `LogRecord` and never reaches Cloud Logging. In the Simulation
Entrypoint and in Stage 12 (the executor's `stage12_*` modules and the
Stage 12 and v2 Modal applications), pass every log call's fields through `log_extra(...)` from
`policyengine_simulation_observability.log_records`, which puts them in both
places (a field named like a `LogRecord` attribute, such as `name`, goes only
to `policyengine_attributes`, since `logging` refuses it), and name a row with `database_row_attributes(table, row_id)` (rule
`sim.observability.log-fields-through-log-extra`; its check reads only the
field names written as keywords in the `log_extra(...)` call, not keys unpacked
from a dictionary with `**`). Each field must be a
string, a number or a boolean; the runtime drops a list. The Entrypoint's
package logger and `stage12_runtime`'s are set to INFO: the root logger stays
at WARNING, which would discard INFO records before the handler sees them. The
v1 code's logs are left as they are, so `log_and_redact_exception` (whose
fields also reach telemetry as its `gateway_error_redacted` event) and the
contract library's `budget_window_state` still pass plain `extra=` fields.

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

Open a span with `runtime.span(plan.name(Stage.VALUE))`, and an operation with
`runtime.operation(...)`. policyengine-observability 3.0.4 and later type both
scopes correctly: their `__exit__` and `__aexit__` return `Literal[False]`, so
Pyright knows an exception raised in the block propagates; see
`docs/engineering/skills/type-checking.md`.

When adding a run configuration or runtime stage:

1. add it to `RunConfiguration` or `Stage`;
2. add it to the applicable plan in `RUN_STAGE_REGISTRY`;
3. import the plan in runtime code;
4. add focused tests for the new stage and identifier propagation.

The Stage 12 coordinator measures output preparation with these nested
operations:

| Stage | Work measured |
|---|---|
| `stage12_coordinator_preparation` | The complete interval between acquiring report execution ownership and starting child-state persistence |
| `stage12_output_planning` | The complete coordinator interval that builds the shared output plan |
| `stage12_country_model_load` | Loading the country model and its Python modules |
| `stage12_output_configuration` | Creating planning simulations and configuring requested output groups |
| `stage12_output_variable_resolution` | Resolving entity variables and constructing the output schema |
| `stage12_child_input_planning` | Attaching the output plan to one baseline or reform child input |

`stage12_child_input_planning` runs once for each simulation and includes a
`simulation_role` attribute. The other planning operations include the
`country` attribute.

### Annual job stage durations

`run_simulation` records client-visible job stages (see
`policyengine_simulation_contract.job_progress`). Before its operation ends,
it sets one `job_stage_seconds_<stage>` attribute for each ended stage, for
example `job_stage_seconds_baseline` and `job_stage_seconds_reform`. These
are the only main-path measurements that separate the baseline from the
reform calculation, because both run inside `economic_impact_analysis`. The
attribute set is bounded by the `JobStage` enum.

Modal can run the same call again after a preemption, under the same call
id. A later attempt continues the stored stage record, so the durations add
up over attempts, and `job_attempts` counts them.

### Preemptions

Modal delivers a preemption to a running synchronous function, which every
executor and Stage 12 Modal function is, as `KeyboardInterrupt`, then runs
the same input again. In an async app, such as the gateway's ASGI app, Modal
cancels the running task instead, so a request there ends in
`asyncio.CancelledError` and what follows doesn't apply to it.
policyengine-observability 3.0.7 and later
record an operation or request that ends in `KeyboardInterrupt` with the
outcome `interrupted` at WARNING and leave it out of the error count metric;
a span that ends in it keeps an unset status and gets the attribute
`policyengine.outcome` set to `interrupted`. The exception propagates
unchanged, so the preempted call's own records don't reach the daily
telemetry error report. Let `KeyboardInterrupt` leave the operation block as
it is: code that catches it and raises another exception instead reports the
attempt as a failure at ERROR. `except Exception` doesn't catch it.

Modal ends a call it cancels with `InputCancellation` instead, which is a
`BaseException` but not a `KeyboardInterrupt`, and policyengine-observability
records that operation with the outcome `error` at ERROR. A preempted Stage 12
segment runner cancels the segment calls it started, and a preempted Stage 12
coordinator with a report run cancels its unfinished simulation calls; a
segmented US simulation cancelled that way cancels its own started segment
calls in turn. Each of those calls that was running writes an ERROR record,
so one coordinator preemption can write one for each simulation and each of
its running segments.

### Container start and call number

Each call of the versioned app's workers (`run_simulation` and
`run_simulation_segment`, as class methods and as plain functions) and of
`run_budget_window_batch` sets two attributes on its operation, so on its
`operation.completed` log record and its span. `_set_modal_call_attributes`
in `src/modal/app.py` sets them when the operation opens, so a call that
fails or is preempted carries them too.

| Attribute | Values |
|---|---|
| `container_start` | `restored_from_snapshot`: the container was restored from a memory snapshot, so it started with the country models loaded. `took_snapshot`: the snapshot hook, which loads the models, ran in this container; Modal takes the snapshot from such a container. `no_snapshot`: the function has no snapshot hook: the plain `run_simulation` and `run_simulation_segment` functions, and `run_budget_window_batch`. |
| `container_call_number` | The call's place among the calls the container has served: 1 for its first call, 2 for its second, and so on. A call counts even when setting its attributes fails. |

The workers' snapshot hook (`load_country_models`) records `MODAL_TASK_ID`,
Modal's ID for the container, before Modal takes the snapshot, and each call
compares it with the current `MODAL_TASK_ID`. A restored container starts
with the recorded ID in memory, but Modal 1.6.4 sets its `MODAL_TASK_ID` to
its own task ID while restoring it (`memory_restore` in
`modal/_runtime/task_lifecycle_manager.py`). Modal doesn't document that, so
check the attribute against Modal's own container records (the
`snapshot_behavior` field of its `TaskGetInfo` call). A container that starts
without restoring for another reason, such as one Modal starts again after a
failed restore, runs the hook itself and reports `took_snapshot`. The
recorded ID names the snapshotting container, so it is never reported.

The restore itself happens before any worker code runs, so no span measures
it; only Modal's container records hold its duration. A restored container's
setup time shows in its first call's spans (`restored_from_snapshot` and
`container_call_number` 1), such as the time from the operation's start to its
`dataset_load` span, which a container that hasn't loaded the models spends
importing them. Compare those calls with first calls that report
`took_snapshot` or `no_snapshot`, which load the models themselves, and with
later calls (`container_call_number` above 1), which reuse what the container
has loaded.

Neither attribute reaches metrics: the package's metric attribute keys don't
include them, and each call number would make another series.
`container_start` has three values. Records from apps deployed before these
attributes carry neither.

## Metric resource identity

Every process that emits OpenTelemetry metrics must have a unique
`service.instance.id`. A Cloud Run revision name identifies deployed code and
a Modal task identifier identifies one task; neither value alone identifies a
Python worker process. Build the resource value with
`policyengine_observability.process_instance_id`, using the Cloud Run revision
or Modal task identifier as the platform prefix. The helper adds the process
ID and a random process value, and returns the same result for later calls in
that process.

Cloud Run deployments must also provide `CLOUD_RUN_REGION`. Modal provides
`MODAL_REGION` at runtime. These values populate each workload's
`cloud.region`. The collector separately adds the central Google Monitoring
location to metrics before export.

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
