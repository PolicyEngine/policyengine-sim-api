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

### Annual job queue time

`run_simulation` also sets `job_queue_seconds` on its operation: the seconds
from the gateway's submission of the job to the job's start in the worker.

- **Submission time:** the gateway sends it with the spawned job under the
  `_job_submitted_at` parameter (`JOB_SUBMITTED_AT_PARAM` in
  `policyengine_simulation_contract.job_progress`), the same value it stores
  as the job metadata's `submitted_at`. The leading underscore keeps it out of
  the operation's logged attributes (`redact_params_for_logging`), and the
  worker removes it before the calculation, so segment children never get it.
- **Start time:** the stage record's `started_at`, which is the first
  attempt's start and carries over to later attempts. Every attempt's record,
  a preempted one's included, carries the same value, so the successful
  attempt's record carries it too. It never counts the time between a
  preemption and the next attempt.
- **When it is set:** when the operation opens, so a failed or preempted call
  carries it too.
- **Clocks:** the two times come from the gateway's and the worker's
  machines. A start before the submission can only mean those clocks differ,
  and is recorded as 0. Values are rounded to hundredths of a second.
- **Jobs without it:** the years that a budget-window batch spawns, whose
  batch payload can't take the key (`run_budget_window_batch` validates it
  with `extra="forbid"`). Records from worker apps deployed before this
  attribute, and from calls an older gateway spawned, carry none either.
- **A failed read:** a later attempt whose stage reporter couldn't read the
  earlier attempt's record starts a new one, so its value includes the
  earlier attempts' time.

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

Each call of the versioned app's workers (the `SimulationWorker.run_simulation`
and `SimulationSegmentWorker.run_simulation_segment` methods) and of
`run_budget_window_batch` sets two attributes on its operation, so on its
`operation.completed` log record and its span. Only the operation's span
carries them, not its child spans such as `country_module_load`.
`_set_modal_call_attributes` in `src/modal/app.py` sets them when the
operation opens, so a call that fails or is preempted carries them too.

| Attribute | Values |
|---|---|
| `container_start` | `restored_from_snapshot`: the container was restored from a memory snapshot, so it started with the country models loaded. `took_snapshot`: Modal took the snapshot from this container, after the snapshot hook loaded the models in it. `no_snapshot`: the container neither took a snapshot nor was restored from one. That is every container of the plain `run_budget_window_batch` function, which has no snapshot hook, and a worker class container that Modal started without snapshots, whose hook loaded the models before its first call. |
| `container_call_number` | The call's place among the container's calls that opened their operation: 1 for the first, 2 for the second, and so on. A call counts even when reading or setting these attributes fails. |

How a call tells them apart, in Modal 1.6.4:

- **Before the snapshot:** Modal runs the workers' snapshot hook
  (`load_country_models`) in every container that isn't restored. It takes a
  snapshot only when the Modal process that starts the container has set
  `MODAL_ENABLE_SNAP_RESTORE` to `1`, for a class with memory snapshots
  enabled (`maybe_snapshot` in `modal/_runtime/user_code_imports.py`). The
  hook records that flag and the container's `MODAL_TASK_ID`.
- **On restore:** a restored container starts with that record in memory, but
  Modal sets its `MODAL_TASK_ID` to its own task ID (`memory_restore` in
  `modal/_runtime/task_lifecycle_manager.py`). A call reports
  `restored_from_snapshot` when the two IDs differ, and `took_snapshot` when
  they match.
- **Not documented by Modal:** neither variable's behaviour is, so check the
  attribute against Modal's container logs, and again after a Modal upgrade.
  A restored container logs "Restoring Function from memory snapshot." and a
  snapshotting one "Creating CPU memory snapshot for Function.";
  `modal app logs <app name> --env <environment> --search "memory snapshot"
  --show-container-id` lists both. Match a log's container ID with the task
  ID at the start of a record's `service.instance.id`. Modal's `TaskGetInfo`
  call doesn't tell them apart: on 2026-11-25 its `snapshot_behavior` field
  was `UNSPECIFIED` for every production container, restored ones included.
- **Checked on Modal:** on 2026-11-25, in a measurement on Modal `testing`
  with Modal 1.6.4, every restored container reported a `MODAL_TASK_ID`
  different from the one its snapshot hook had recorded.
- **Never reported:** the recorded ID names the snapshotting container, not
  the one serving the call.

Neither the restore nor the snapshot hook runs inside a call, so no span
measures them. Modal's container records (its `TaskGetInfo` call) give the
times each container was queued and became ready (`enqueued_at` and
`started_at`), which include scheduling as well as the restore. A call's spans show the setup still left
in it:

- In apps deployed before the worker classes, whose workers load the models
  in each container's first call, most of that setup is in the
  `country_module_load` span, which imports policyengine.py's country model
  module, and, for a US run, in the time from the operation's start to that
  span, which includes the SPM capability check. Those apps' records carry
  neither attribute, so `container_call_number` can't pick out a container's
  first call. A segmented national run's `run_simulation` call opens no
  `country_module_load` span: its segment workers' calls do.
- In a container whose hook loaded the models, the first call's time there
  should be close to later calls' (`container_call_number` above 1).
- A container's first `calculation` has also been slower than its later ones;
  compare that span between first and later calls too.

Neither attribute reaches metrics: the package's operation metrics carry only
the service's identity and the operation's name, kind and outcome, and each
call number would make another series. `container_start` has three values.
Records from apps deployed before these attributes carry neither.

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
