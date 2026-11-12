# Stage 12 Modal worker foundation

Companion change identifier: `stage-12-modal-worker-foundation`.

This change implements the simulation-service portion of the Stage 12 plan
defined in `PolicyEngine/policyengine-api`. It does not change the public
simulation submission or polling contracts.

## Service ownership

- The Cloud Run Simulation Entrypoint keeps its current production adapter,
  which forwards submissions and polls to the existing Modal HTTP routing
  service. Responses from that path remain authoritative.
- A separate comparison-run adapter reads only a separately stored v2 version
  manifest and invokes separately named v2 Modal functions directly.
- Invocation context carries both the logical deployment environment used for
  records and artifact paths (`staging` or `production`) and the Modal
  environment used for function lookup (`staging` or `main`). Production child
  simulations therefore resolve in Modal `main` without relabeling durable
  records as `main`.
- One v2 report-coordinator invocation starts baseline and reform as distinct
  single-simulation calls, then checks both calls with nonblocking result probes
  on every polling pass.
- An eligible US national single-simulation call starts 20 region-group
  calculation calls and merges their typed Parquet results. Because baseline
  and reform are separate and start before either is awaited, an eligible
  report starts 40 region-group calculation calls. These are invocations;
  Modal may reuse a warm container for more than one invocation.
- The report coordinator has a Modal `max_containers` value of 25: three
  budget windows at five years each, and ten other reports. Each
  country-specific single-simulation function has 50, both simulations of
  every coordinator call, and the separate US region-group function has
  1,000, every segment of every US simulation call, so calculation work
  cannot wait behind the parent calls that are collecting it. These are
  per-function limits, not one shared quota.
- Stage 12 owns its partition, request and result models, orchestration, merge,
  and cache implementation. It does not import the corresponding v1 modules.
- The v2 functions write canonical private artifacts and use a shared
  SQLAlchemy Core package to persist temporary comparison state directly.
  They do not create production simulations, reports, report runs, or user
  associations.

The existing Modal routing service, its versioned executor applications, and
its v1 routing manifest remain deployed and unchanged throughout Stage 12.

## Cross-repository ownership

`PolicyEngine/policyengine-api` is the only schema authority for the temporary
comparison tables. Their physical `stage12_evaluation_*` names and
`evaluation_id` column remain unchanged until Stage 14. This repository owns
the strict runtime request, artifact, and data-transfer models used by the
Stage 12 workers. Changes that affect both repositories require coordinated review;
no generated cross-repository contract file is checked in or consumed at
runtime. This repository must not define SQLModel tables or an Alembic
migration for the comparison records. It contains synchronized SQLAlchemy Core
table mappings and parameterized DML only; raw SQL, SQLAlchemy textual SQL,
DBAPI cursor execution, runtime DDL, metadata creation, and a competing
migration chain are prohibited. Deployment validation compares those mappings
with the live API-owned schema.

## Release and deployment constraints

- The selected PolicyEngine.py release's packaged `.py` bundle manifest is the
  sole source of country-package names and exact versions, data-package names
  and versions, default dataset identities and URIs, artifact revisions, and
  supported alternatives.
- Environment variables, workflow inputs, command-line values, and installed
  distribution inspection may only assert expected bundle-derived values.
- V2 application names, manifest storage, publication configuration, reader
  configuration, and in-process cached state must be distinct from v1.
- Publishing or rejecting either manifest must leave the other manifest and
  its selected applications unchanged.
- V2 worker object access uses the already-provisioned separate
  `stage12-evaluation-gcp-credentials` Modal secret. The worker application
  does not receive the existing general GCP credential secret. The credential's
  service account can read and write its environment-specific Stage 12 artifact
  bucket and simulation output bucket. A conditional IAM binding separately
  permits it to read
  `constituencies_2024.csv` from `policyengine-uk-data-private`; deployment
  validation downloads that lookup file using the worker credential. Stage 12
  local-authority output uses packaged metadata and does not require access to
  `local_authorities_2021.csv`.
- Stage 12 uses a dedicated environment-specific cache bucket named by
  `STAGE12_CACHE_BUCKET`. Its object keys and manifest namespace are separate
  from the v1 precompute store. The same restricted worker credential can
  create, read, and delete objects in this cache bucket.
- `STAGE12_DATABASE_URL` is delivered from the environment-specific Secret
  Manager resource named by `STAGE12_DATABASE_URL_SECRET_NAME`. It authenticates
  as the existing shared `policyengine_v2_runtime` account; Stage 12 does not
  create or require a separate PostgreSQL role. The stored URL uses the
  canonical `postgresql://` scheme, and the persistence package selects the
  psycopg 3 driver internally.
- `STAGE12_ENABLED` is the only Stage 12 execution setting. Missing or `0`
  disables automatic runs and rejects new direct Stage 12 submissions. `1`
  enables every supported newly accepted annual society-wide report, except
  US state requests (see "Initial flow" below), and the authenticated direct
  submission route. Any other value prevents service
  startup. Changing the setting requires a Cloud Run deployment. There is no
  percentage, request-bucket, shared runtime-control document, or
  partial-dataset mode.
- The separately named Modal application and v2 manifest remain deployed when
  `STAGE12_ENABLED=0`. The authenticated status route remains available for
  inspecting existing runs whenever the Stage 12 resources are configured.

Any genuinely one-time provisioning sequence must be implemented in a bounded
disposable script before execution and removed after the resulting state is
verified. Operations required for repeatable deployment, recreation, rollback,
or disaster recovery remain in maintained automation.

### Provisioning classification

Stage 12 currently requires no genuinely one-time provisioning operation. The
following operations are repeatable and therefore belong in maintained
automation or infrastructure configuration:

| Operation | Maintained owner |
| --- | --- |
| Build and deploy each versioned v2 Modal application | Simulation deployment workflow |
| Build and verify the Stage 12 dataset and baseline cache | Simulation deployment workflow |
| Create or update the separate v2 manifest storage object | V2 manifest publisher |
| Configure the v2 manifest reader and `STAGE12_ENABLED` | Cloud Run deployment workflow |
| Create the private report-artifact, simulation output and cache buckets and their policies | Infrastructure configuration |
| Ensure the existing API v2 runtime identity has comparison-row database access | API infrastructure configuration |
| Deliver the existing shared runtime database secret to Cloud Run and Modal | Simulation deployment workflow |
| Grant private-object access only to the Modal runtime | Infrastructure configuration |
| Attach named runtime secrets without exposing their values | Simulation deployment workflow |
| Disable or restore automatic Stage 12 invocation | Cloud Run deployment workflow |
| Recreate any Stage 12 service or storage resource after loss | The same deployment and infrastructure automation |

When a deployment requests the Stage 12 resources, the ordinary integration
and promotion jobs wait for storage configuration, v2 worker validation, and
separate v2 manifest publication to succeed. A failed Stage 12 deployment
therefore prevents promotion of a Cloud Run revision configured to use it.

Every merge to `main` requests the Stage 12 resources in both the `beta` and
`prod` jobs. Each job validates its pre-provisioned environment-specific
resources, deploys and validates the separate v2 Modal application, publishes
the separate v2 manifest, and deploys a Stage 12-configured Cloud Run revision.
The `prod` job runs only after `beta` succeeds. The `beta` and `prod` GitHub
environments each provide `STAGE12_ENABLED`; both values are initially `0`, so
deploying Stage 12 does not copy production-path submissions until an operator
changes the applicable value to `1` and deploys Cloud Run. Production
prerequisites must therefore be provisioned and verified before merging;
missing production prerequisites are a deployment failure, not a reason to
omit Stage 12 from the `prod` job.

This automatic production deployment creates or updates a dormant Stage 12
runtime: the separate Modal application, v2 manifest, runtime configuration,
and temporary authenticated direct endpoint are present for later updates and
qualification. With `STAGE12_ENABLED=0`, it does not send ordinary production
calculations to the Stage 12 report coordinator or single-simulation workers,
and it rejects new direct Stage 12 submissions.
The promoted Cloud Run revision continues forwarding those calculations to the
existing Modal HTTP routing service and existing executors.

Existing environment, domain, and production-service resources are inputs to
Stage 12 rather than resources this change provisions. If implementation later
reveals an operation that is genuinely required only once, work must pause
until its bounded disposable script, target checks, secret references, and
verification output have been reviewed. The script is removed only after the
created state is independently verified.

## Initial flow

The initial comparison flow is `POST /simulate/economy/comparison` for reviewed
annual society-wide comparison inputs. The production request is forwarded
unchanged. Only a newly accepted supported production job may create one
corresponding comparison run; polls, cached results, and repeated submissions
that resolve the same production job do not create another comparison.

US state requests (a `state/*` region, in any case, or a bare two-letter code,
which the runtime reads as a state) get no shadow comparison, even when the
adapter supports them. The app will run every state for a nationwide report's
congressional district view, so comparing them would start two simulations per
state, and Stage 12 is being replaced, so state results are no longer compared
with production. The entry logs
`stage12_comparison_run_skipped` with reason `excluded_us_state_region`, and
only for state requests the adapter would otherwise have compared. Knowing
that needs the worker bundle, so the check runs after the v2 manifest is read:
a state request whose manifest read fails or passes the five-second wait logs
that failure instead, as any other request would. US
national, congressional district and place requests, and every UK request
the adapter supports, are still compared. The operator direct-submission
route still accepts state regions.

The Simulation Entrypoint, Modal report coordinator, and single-simulation
functions use each environment's existing `policyengine_v2_runtime` PostgreSQL
credential. They share one SQLAlchemy Core persistence package for the two
temporary tables. `policyengine-api` remains the sole owner of the canonical
SQLModel definitions and Alembic chain; the simulation repository owns no DDL
and introduces no persistence HTTP routes.

Before deploying, the simulation workflow obtains the shared runtime URL from
Secret Manager, verifies the expected `policyengine_v2_runtime` identity and
permissions, inspects the complete temporary schema against the declared
SQLAlchemy mappings, and runs a rolled-back insert/read/update/delete canary. It
also verifies required Secret Manager access and performs separate
create/read/delete checks in the private report-artifact, simulation output and
cache buckets. The temporary objects are deleted in the normal path and by exit
cleanup after a failure.

The current Public API omits `data` for the certified default dataset. The v2
adapter therefore resolves an absent `data` field to the exact default dataset
in the selected `.py` bundle. An explicit `data` value is eligible only when it
identifies an artifact declared by that same bundle. The request model converts
the Public API's `_telemetry` field to `telemetry`; the adapter accepts that
correlation metadata but does not include it in either simulation input.

## Where output files are kept

| Files | Bucket, named by the GitHub environment variable | Object key | Retention |
| --- | --- | --- | --- |
| Every artifact of a comparison run; the planned inputs of a detailed-save call | `STAGE12_ARTIFACT_BUCKET`: `policyengine-stage12-staging-artifacts`, `policyengine-stage12-production-artifacts` | `<artifact_prefix>/...`; a comparison run's prefix is `stage-12-runs/<environment>/<YYYY>/<MM>/<evaluation_id>` | Deleted 30 days after it is written |
| The simulation output files of a detailed-save call | `V2_SIMULATION_OUTPUT_BUCKET`: `policyengine-v2-staging-simulation-outputs`, `policyengine-v2-production-simulation-outputs` | `simulation-runs/<environment>/<country>/<simulation_run_id>/output-<SHA-256>.parquet` | Never deleted; moved to Coldline 90 days after it is written |

A detailed-save call names the Public API's runs instead of evaluation records
(see "Detailed-save calls on the run tables"). Each of its workers writes its
simulation output file to the simulation output bucket, keyed by its
simulation run, and records that URI as its simulation run's `output_uri`. The
coordinator reads the file back from there to aggregate it, and a later report
run can reuse a succeeded simulation run's file. The key names the file's
SHA-256, so a worker Modal restarts can store other bytes beside its earlier
attempt's file, and identical bytes reuse the object. A report run writes no
aggregate file: its result is in the result tables.

A detailed-save coordinator or worker without `V2_SIMULATION_OUTPUT_BUCKET`
fails with a `ValueError` naming the variable before it claims anything; it
never writes the file to the artifact bucket, which would delete it after 30
days, and the store refuses a simulation output bucket equal to the artifact
bucket. Comparison runs never read the variable.

The deployment's Stage 12 secret-sync step copies the environment's
`V2_SIMULATION_OUTPUT_BUCKET` GitHub variable into the
`stage12-evaluation-runtime` Modal secret, beside `STAGE12_ARTIFACT_BUCKET` and
`STAGE12_CACHE_BUCKET`, and every v2 function receives that secret. The
infrastructure validation requires the name to identify the environment and to
differ from the artifact bucket, and writes, reads back and removes a canary
object under `simulation-runs/_deployment-validation/<environment>/` with the
worker's credential. Worker validation fails when the secret lacks the
variable. The workers write both buckets with the same
`stage12-evaluation-gcp-credentials` service account
(`stage12-modal-staging@` or `stage12-modal-production@` in
`policyengine-simulation-entry`), which has `roles/storage.objectUser` on each.

### Deployments that change where output files go

A report run reaches the v2 application that the manifest's default version
names, or the one `STAGE12_V2_WORKER_VERSION` pins. An application's name comes
from its .py version alone, and a deployment runs `modal deploy` before its
checks, so it replaces an application of the same .py version in place before
any check. That gives these cases:

- **A deployment of this change with a newer .py version than the default's.**
  It adds a new application and moves the default to it only after every check.
  Until then report runs keep reaching the previous application, which writes
  their simulation output files to the artifact bucket, where they are deleted
  after 30 days.
- **A deployment of this change with the default's .py version.** Its
  `modal deploy` replaces the default's application before any check, so new
  calls write their files to the simulation output bucket from then on. The
  secret-sync step, which runs earlier in the same job, has already added
  `V2_SIMULATION_OUTPUT_BUCKET`.
- **A deployment from an earlier commit.** Its secret-sync step replaces the
  whole `stage12-evaluation-runtime` secret without `V2_SIMULATION_OUTPUT_BUCKET`.
  - At the .py version of an application from this change, its `modal deploy`
    puts the earlier code back in that application. Report runs reaching it
    then succeed and write their files to the artifact bucket. They fail with
    the `ValueError` only between the secret sync and the `modal deploy`, while
    the application still runs this change's code.
  - Applications of other .py versions from this change or later keep this
    change's code, so report runs reaching them, for example before the
    deployment moves the default, fail with the `ValueError` until a deployment
    from a later commit restores the variable.

  The rollback workflow does not change Stage 12, so only such a deployment
  does this.

Runs that succeeded before this change, and report runs in the cases above,
name files in the artifact bucket. So a succeeded simulation run can name a
file that no longer exists: reusing succeeded simulation runs' output files
must treat a run whose file is gone as not reusable.

## Detailed-save calls on the run tables

The detailed-save API's reports and simulations are calculated on the Public
API's run tables alone, as the Modal worker role: no detailed-save call
creates or claims a `stage12_evaluation_*` record. Only comparison-path calls,
which pass an evaluation record, write those, until the comparison path is
removed. The household functions work the same way on `household_jobs`.

**A report run.** The simulation service creates, in one transaction, the
`simulations` and `reports` rows, a `simulation_runs` row for the baseline and
one for the reform (pending, with no `modal_call_id`; or a run that already
succeeded, to reuse it), and the `report_runs` row naming both (pending, with no
`modal_call_id`). It then starts the manifest's `report_coordinator_callable`
(`coordinate_report`) with two arguments and no evaluation record:

- the report input, a `ReportExecutionInput` whose `evaluation_id` is the report
  run's ID and whose baseline and reform `simulation_execution_id`s are their
  simulation runs' IDs;
- a `Stage12InvocationContext` whose `report_run` names the report run and both
  simulation runs, whose `simulation_callable` is the country's
  `single_simulation_callable`, and whose `artifact_prefix` names the report run.

Once Modal accepts the call, the service records its call ID on the report run
while it is still pending with no call, and fails the run if Modal refuses the
call. The service never records a call on, or starts a worker for, a simulation
run that a pending or running report run names: those are the coordinator's.

The coordinator claims the report run under its own Modal call ID, then checks
the input against it: a report input or prefix naming other runs, or a report
run row naming other simulation runs, fails the report run with
`report_run_input_mismatch`. If a worker it starts finds its run held by a
worker an earlier attempt started, which Modal never cancelled, the coordinator
waits for that worker instead. It uses the stored output of each simulation run
that already succeeded (see the reuse of succeeded runs above), fails the
report run with the run's own recorded failure if a run it must calculate
already failed (`simulation_run_failed` if it recorded none), and starts a
worker for each other one with a context naming only that
simulation run (`simulation_run_id`). It aggregates both outputs and completes
the report run with its result rows. It returns
`{"report_run_id", "status"}`, or the same marked `deduplicated` without running
when another call holds the report run or it has finished.

**A simulation run on its own.** The service creates the `simulations` row and
its `simulation_runs` row (pending, no call), starts the country's
`single_simulation_callable` with a `SimulationExecutionInput` (role
`standalone`, no output plan, `evaluation_id` and `simulation_execution_id`
both the run's ID) and a context naming the run in `simulation_run_id`, with
an `artifact_prefix` that contains the run's ID (for example
`stage-12-runs/<environment>/<YYYY>/<MM>/<simulation_run_id>`), and records the
call as for a report run. A prefix without it fails the run. The worker plans the simulation's outputs
as for a report comparing it with itself, so a report whose plan needs more
columns, for example a reform that turns on labour supply responses, can't
reuse it and fails with `reused_simulation_output_incompatible`.

**A re-run.** The service re-runs a stored report or simulation with the same
two calls, by run ID. It creates the record's new run (a report run with
trigger `manual`) and, for a report, a new `simulation_runs` row of each side's
own simulation, or names that simulation's run that already succeeded on the
same versions, unless the re-run is forced. The new runs record the catalog
version, dataset and region of the current .py version's catalog; the
`simulations` and `reports` rows are unchanged. The coordinator and workers
claim and calculate them as they would a new record's: nothing on the Modal
side tells a re-run apart.

**Each worker** claims its simulation run under its own Modal call ID: a pending
or running run that records no call or its own. It records its output file, or
its failure, only while it holds the run running. It returns the output's
`SimulationArtifactDescriptor`; for a run that already succeeded it returns the
stored output's descriptor without calculating, and for a run another call
holds, or one that failed, it returns `{"deduplicated": true,
"simulation_run_id", "status"}`.

**Pre-emption and cancellation.** Modal runs a pre-empted call again under the
same call ID, and each claim lets that call resume. A pre-empted coordinator
cancels its unfinished workers and records nothing; the restarted coordinator
asks each earlier worker that holds a simulation run to stop, releases that run
(pending, no call, keeping its first start) and starts a new worker on it. A
run that worker finished meanwhile is used as it finished, or fails the report
with its own cause. A failed or cancelled coordinator fails its report run and the
simulation runs it left unfinished, never one that succeeded. The stuck-run
sweep judges the run tables only, leaves a report run's simulation runs to its
coordinator while the report run is unfinished, and never fails a run whose
call Modal still reports pending, which includes a pre-empted one, before the
run's maximum age of 24 hours.

**Applications deployed before this change** reject both calls before writing
anything: their `coordinate_report` requires an evaluation record, and their
context model refuses `simulation_run_id`. Modal accepts the call, which fails
at once, and the run stays pending with its call recorded until the sweep fails
it (`modal_call_ended`), at least ten minutes after its creation. A call
reaches such an application only while the manifest's default, or
`STAGE12_V2_WORKER_VERSION`, names one: during the deployment of this change,
if it adds a newer .py version, until it moves the default to it, or while a
pinned version names an older application. The release validation calls the worker images' validation
functions and the household functions, not the coordinator or the economy
workers, so it does not catch such a mismatch; a staging economy report run on
each staging deployment would.

### Budget windows

A budget-window report compares the same baseline and reform in each year of a
window of 1 to 75 consecutive years. It is an economy report whose
`budget_window_size` is the window's length. Its calculation is one
`budget_window_runs` row, and each year is a report run naming that row and
the year, with its own simulation runs of that year, so a year can reuse a
single-year report's runs, and the reverse.

- **Creating and starting.** The simulation service creates every year's runs
  in one transaction, all pending with no call, then starts the earliest
  years within one ten-second wait for Modal, counted from the first start;
  a later year not confirmed within it is deferred (`YearStartDeferred`)
  and waits for a started year's coordinator. The contract's
  `year_calculation` derives each year's coordinator
  input and context from the first year's: only the year, its three runs, the
  artifact prefix (`report_run_artifact_prefix`) and the call's creation time
  differ.
- **At most five at once.** `BudgetWindowStore.advance_window` locks the
  window run's row, then starts the earliest waiting years (pending with no
  call) until five are under way (running, or started with a call recorded),
  recording each call while the year is still pending with no call. The lock
  makes advances that run at once start each year once; a year a duplicate
  call reaches is claimed by only one call.
- **Each year's coordinator.** `coordinate_report` runs every call through
  `coordinate_window_year`. It looks the year's window up once, marks the
  window running before the coordinator runs, and advances the window once the
  coordinator returns, fails or is cancelled. The advance starts the next
  years in the same application, by `ModalYearStarter`, or finishes the
  window. A pre-emption advances nothing; the restarted call advances when it
  ends, even when it finds its year already finished. A report run outside any
  window makes no window writes, and no window write ever changes the year's
  own outcome.
- **The window's outcome.** A failed year, or a start Modal refuses, fails the
  window, its waiting years and their unstarted simulation runs, and no
  further year starts; years already started run to their end. A start whose
  year its own call claimed meanwhile counts as started, and a deferred
  start fails nothing. Once every
  year has succeeded the window succeeds.
- **The stuck-run sweep.** It leaves a waiting year of an unfinished window to
  the window, counts a year started later from when its call was recorded,
  fails the window when it fails one of its years, and settles a window that
  nothing has changed for ten minutes with no year under way: from its years
  when they have all ended, or as stalled (`budget_window_stalled`) when years
  are still waiting.

The coordinator may run 25 calls at once (three windows at five years, and ten
other reports), so each simulation worker may run 50 and the US segment
function 1,000.

## Report output planning

Before starting either child simulation, the Modal report coordinator resolves
one immutable, strongly typed output plan for the complete Stage 12 report. The
plan records the requested aggregate profile, whether cliff analysis was
requested, whether either policy activates labor-supply responses, and the
required columns for every country-model entity.

The resolver constructs data-free planning `Simulation` objects and invokes
the output-configuration functions supplied by the pinned PolicyEngine 5.2.0
bundle. For US reports this includes the budgetary-impact variables; both
countries use PolicyEngine's conditional cliff and labor-supply configuration.
Planning never loads a dataset or executes a simulation. The former
`variables=("*",)` internal marker is not an output expansion mechanism and is
no longer used.

The coordinator includes the exact same output plan in the baseline and reform
child inputs. Each child adds the plan's additional variables before calling
`Simulation.ensure()`, then verifies that every planned entity and column is
present before writing its Parquet artifact. The artifact descriptor records a
digest of the plan. After both calls complete, the coordinator checks both plan
digests and independently validates both loaded Parquet schemas before it runs
aggregate calculations. Extra columns are permitted; missing planned columns
fail the temporary Stage 12 report without affecting the production result.

The plan distinguishes calculated variables from columns copied directly from
the source dataset. UK geographic reports require the household columns that
the constituency and local-authority outputs read (`UK_AREA_DATASET_COLUMNS`):
`constituency_code_oa`, `la_code_oa`, `region_code_oa` and
`source_household_id`. Workers do not request those columns as calculated
PolicyEngine variables, but they must be present in each materialized artifact;
otherwise validation fails before geographic aggregation begins.

The exception is a UK national or nation-level report whose bundle routes
constituency and local-authority regions to another dataset. Its national
dataset may carry no area codes, so the plan copies none, and its aggregate
leaves the constituency and local-authority breakdowns out. The production
national run leaves them out too: both decide with
`uk_area_breakdowns_omitted`, so their results still compare. Production
computes those breakdowns in a separate `breakdown: "local_areas"` request on
the routed dataset. The entry adapts no such request into a Stage 12 report
(skip reason `unsupported_options`), and Stage 12 has no local-area report
yet, so the shadow comparison does not cover the breakdown numbers.

Aggregation uses the retained output tables without rerunning either model.
The precomputed baseline and reform simulations retain their original policy
metadata so PolicyEngine can correctly recognize conditional labor-supply
analysis. `include_cliffs=true` is supported by this path and is carried through
both output planning and aggregation.

For US SPM calculations, each child artifact stores one compact
`spm_provenance` receipt inside its calculation metadata. The coordinator
validates the requested selection against the forecast, scenario, geography,
county-vintage, and date fields in that receipt, then publishes only the
combined `spm_provenance` object in the completed report. Completed results do
not contain a sibling `spm_config`; the receipt is the sole source of resolved
SPM configuration. Request payloads still accept the partial `spm` selection
and resolve omitted options from the certified bundle before execution.

### Temporary UK local-authority display metadata

Stage 12 detects the local-authority boundary configuration from the complete
source dataset before applying any requested regional scope. It examines the
dataset's `la_code_oa` values: the 17 predecessor English authorities identify
LAD22, while `E06000063` through `E06000066` identify LAD23. A mixed boundary
configuration, an unknown code, or a dataset without a distinguishing code
fails the Stage 12 run. Dataset names and release labels do not select a
boundary version, and there is no fallback to the newest known configuration.

The executor temporarily packages three resources under its expandable
`static_runtime_files/uk_local_authorities/` directory. The v2 Modal image
copies that directory explicitly because Modal's Python-source layer does not
include non-Python files by default:

- one common `code,name` file, `names.csv`, containing the union of supported
  authority codes;
- one `code` file, `codes_lad22.csv`, listing the 374-authority LAD22
  configuration; and
- one `code` file, `codes_lad23.csv`, listing the 361-authority LAD23
  configuration.

The two code lists replaced `coordinates_lad22.csv` and
`coordinates_lad23.csv`, which also gave each authority an integer map
position (`x` and `y`). Results no longer carry a map position, so the
executor no longer ships one; the code lists keep exactly the codes those
files listed.

The Enhanced FRS geography is LAD22: its 2021 output areas are crosswalked to
2022 local-authority boundaries. The output-area vintage does not make the
local-authority boundary version LAD21. Names are attached only after numeric
local-authority impacts have been calculated; these resources never assign a
household to an authority.

The detected boundary version is stored as typed metadata in both child Parquet
artifacts. The coordinator requires baseline and reform to agree before it
enriches the aggregate output. Existing non-Stage-12 calculations continue to
use the existing GCS lookup and are not changed by this path.

This resource ownership is temporary. A future boundary version must add its
codes to the common names file, add a distinct code file, and register a typed
detector definition. Results carry no map positions, which the app draws from
its own layout files, and Stage 14 or later work should remove these temporary
executor resources when the authoritative v2 report architecture supersedes
Stage 12.

## US segmentation and baseline cache

Segmentation applies by default when both simulation inputs describe a US
national society-wide calculation and the shared output plan does not require
cliff analysis or labor-supply responses. `segmented=false` selects the
single-container calculation explicitly. UK calculations, subnational US
calculations, cliff analysis, and labor-supply-response calculations also use
the single-container path. `segmented=true` is accepted, but it does not
override those numerical eligibility conditions. A US national simulation run
started on its own (role `standalone`) is segmented by the same rule. It never
uses the precomputed baseline files, which serve only a report's current-law
baseline under that report's output plan, so it calculates all 20 region
groups.

The Stage 12 coordinator still persists only two logical simulation records:
one baseline and one reform. Each eligible logical simulation call performs
the following work independently:

```text
logical baseline or reform simulation
        |
        +---- start 20 US region-group calculation calls
        |
        +---- collect and validate all 20 results
        |
        +---- concatenate entity rows and reject duplicate identifiers
        |
        +---- write one logical simulation artifact and update one record
```

Region-group calls do not write temporary database rows or private report
artifacts. They return compressed, dtype-preserving data to their logical
simulation parent. The parent validates the result role, partition index,
region membership, content digest, entity schemas, column dtypes, and unique
entity identifiers before merging. A missing or failed region-group call fails
that logical simulation. The parent makes a best-effort cancellation request
for every calculation call that it already started.

The report coordinator observes baseline and reform together rather than
blocking on one role first. On the first failure, it copies the failed
simulation's `error_code` and safe `error_summary` to the report, records the
unresolved peer as `failed` with `simulation_cancelled` and the summary
"Cancelled after the baseline simulation failed." (or reform), and completes
the report as failed without waiting for the peer calculation. An unsegmented
peer is also cancelled through Modal. A segmented peer checks its persisted
simulation status during bounded waits; after seeing the cancellation, it
cancels all region-group calls it owns and exits. Modal does not cancel
spawned calls recursively, so the coordinator cancels a segmented peer through
Modal only if it could not write the cancellation record. Only a `running`
record changes, so a simulation that already succeeded or failed keeps its
outcome, and atomic status replacement prevents a late calculation result from
replacing the cancellation record.

Both simulations share one 3,900-second wait window, the longest simulation
worker timeout. It opens when the coordinator starts, so the coordinator's own
setup, including loading the country model and dispatching both simulations,
comes out of that window and not out of the time it needs afterwards. When the
window closes, the report fails with `simulation_wait_timeout`, and each
unfinished simulation is cancelled in the same way. The coordinator's
5,400-second Modal timeout is that window plus up to 900 seconds for the
production-result wait plus 600 seconds to aggregate, write artifacts, and
record the outcome.

If the coordinator itself fails after starting a simulation, for example while
recording a later dispatch or attaching a started call's invocation
identifier, it cancels every started, unfinished simulation in the same way,
with the summary "Cancelled after the coordinator failed.". The report keeps
the coordinator's original failure.

Errors while reading or updating simulation records never replace the failure
that caused them. If the coordinator cannot read or update the failed
simulation's own record, or record a peer's cancellation, it logs that error
with the report and simulation identifiers and the error type, but not the
error's message. The report still records the simulation's failure, the
failed simulation is never recorded as cancelled, and its peers are still
cancelled with the reason "Cancelled after the baseline simulation failed."
(or reform).

Typed input failures, including invalid policy periods and SPM selection
errors, retain their code and bounded message through the segment, simulation,
and report layers. Unexpected exceptions are logged with their complete stack
trace and persisted as their redacted cause followed by the correlation ID,
for example `OperationalError in stage12_child_state_create: ...
(correlation_id=...)`. Redaction is described in the executor's
`stage12_runtime.failure_causes` module; the cause is shortened, never the
correlation ID, to fit the 512-character summary. The coordinator and
simulation workers report each failure once, before any database write, and
that report carries the same correlation ID.

Before each v2 deployment, the independent Stage 12 precompute application
builds three annual US dataset files and 60 current-law baseline files: one for
each of 20 region groups in 2025, 2026, and 2027. Cache identities include the
bundle manifest digest, package and data versions, source-data digest, year,
region membership, scoping identity, output-plan digest, and the resolved SPM
selection the worker builds the simulation with. A baseline file has the name
.py's `Simulation.storage_id` gives the file it saves and loads: the identity's
simulation ID followed by the SPM selection's digest. Objects are
content-addressed and written with a create-only condition; an existing object
must have the expected digest and size. A stored file is never replaced, so
precompute computes only missing files and has no force option; the v1
`force_recompute` input does not reach it. To replace a bad file, delete it and
deploy again: the run recomputes it and repeats the determinism check.

Before publishing, precompute loads one stored baseline from the cache and
compares it with a fresh calculation. The check runs whenever a run computed
baselines, and also whenever no earlier run recorded a pass for the planned
baselines, so running again after a failed check repeats the check rather than
skipping it. A pass is recorded in the cache bucket, and the manifest is
published only when that record exists.

Precompute also builds per-state datasets. For each US state region whose
requests load a dataset other than the bundle default (the ACS-local dataset
since policyengine 6.7), it prepares that dataset's year file for each
precomputed year once, keeps one state's households with the region's own row
filter, and stores each state's file under the year file's own name. It
refuses data in which a household's district code names another state's
district, because district runs read their state's file.

Precompute publishes a canonical manifest containing the 63 national files and
the state datasets. The deployment passes that manifest's SHA-256 digest into
the US image build. The image build downloads and verifies every listed file,
placing each state dataset in `state-datasets/<dataset>/<state>/` under the
data folder, then records the exact cache release in worker-validation
metadata and the v2 version manifest. A state or district calculation reads
its state's file when the image holds it for the requested year and .py would
reuse it, and prepares the dataset during the calculation otherwise. The UK
and coordinator images do not contain these files. Deployment validation runs
one real 2026 region-group baseline and requires it to load the precomputed
file without recalculation.

Manifest schema version 2 records, for each country a worker application
serves, whether it is economy-capable (it has a single-simulation worker, so
its reports may be given to the report coordinator) and which function
calculates its households. The Simulation Entrypoint's comparison adapter
skips any country the manifest does not mark as economy-capable, with the
reason `unsupported_country`, so the coordinator receives only those
countries' reports.

Readers released before a schema change reject fields they do not know, and
the manifest is published before the Cloud Run revision that reads it is
promoted. The publisher therefore writes the whole schema version 2 manifest
under the `active-schema-2` key and, in schema version 1 form (without the
capability and household fields), the same manifest under the
`active-with-region-datasets` key, without each country bundle's
`region_datasets` under the `active-with-cache` key, and without cache releases
either under the `active` key; readers released before each field keep reading
the key that lacks it. A schema version 1 document marks every listed country
economy-capable, with no household function.

Current readers use the `active-schema-2` document while each older key holds
its schema version 1 form. Schema version 1 cannot describe a country that is
not economy-capable, so a manifest holding one is written only under
`active-schema-2` and the older keys keep the last manifest written there; such
a document is used unless the document under the older keys was validated
later. Otherwise readers use the `active-with-region-datasets` copy only while
it, with its region datasets removed, equals the manifest under
`active-with-cache`; otherwise they read `active-with-cache`, falling back to
`active` before the first publish that writes it. Code released before schema
version 2 or before region datasets republishes only the older keys it knows, so
after a rollback the out-of-date copies stop matching and are no longer read,
and the next publish starts from what the older code wrote. That document has
no household functions, so the manifest names none for versions published
before the rollback until each is published again, and it lacks whatever only
`active-schema-2` held, such as a version with a household-only country. Code
released before cache releases writes only `active`, which current readers do
not read while `active-with-cache` exists; a rollback that far needs the newer
keys deleted from the Modal Dict first. Removing the older copies needs a later
change, once no deployed or rollback revision reads them.

A rollback to code released before region datasets has one window: the older
worker is deployed before its publish job runs, and until that publish the
region dataset copy still matches, so updated entry services keep sending
regional provenance for state and district runs, which the older worker refuses.
Only Stage 12 comparison runs fail in that window, never production responses;
if the older deploy's validation fails, the window lasts until a later publish.
To avoid it, delete the `active-schema-2` and `active-with-region-datasets` keys
from the `simulation-api-v2-version-manifest` Modal Dict before deploying the
older code.

`region_datasets` names the dataset each region type loads. The entry service
records that dataset as a state or district run's dataset provenance, and the
worker refuses a calculation whose provenance names another dataset than the one
it loads, except that it still accepts the default dataset from entry services
released before regional provenance. The bundle digest leaves `region_datasets`
out, so adding it did not change the digest of a running application.

Only an empty-policy baseline using the bundle's default dataset and an exact
region-group scope can read a baseline cache file. Reform calculations always
compute. Missing cache files fall back to calculation; a loaded file that does
not satisfy the requested output plan is recalculated. Each region-group log
records whether it loaded a complete file, recalculated an incomplete file, or
computed because no file existed.

## v2 household functions

The versioned v2 application also defines a household function for each
country, `calculate_household_us` and `calculate_household_uk`. Each call
calculates one household, in the Public API's situation shape and with
optional axes, for one year under one policy, as a household job or a
household simulation run is one household, one year and one policy; a
baseline and its reform are two calls. It takes a `HouseholdExecutionRequest`
and returns a `HouseholdExecutionResult`, which holds the calculated
household with its warnings and SPM receipt, or a typed
`HouseholdExecutionFailure`, from
`policyengine_simulation_contract.household_execution`, as JSON. The request
names the run's year, such as a stored household's default year. A
calculate-full request adds the household's missing variables for that year,
and an axis or a variable value given without a period is for that year too;
every other value is calculated for the period the household names.
The Public API instead takes the year for added variables from a person named
`you`, or the current year (v2 households name people by record ID), and
leaves values and axes without a period to the country model's own default
year. Otherwise the
calculation follows the Public API's household calculation, including its
input errors, warnings and SPM receipt. A function refuses a request for
another country, and returns a `bundle_mismatch` failure for a request that
names another bundle. Nothing calls these functions until the Simulation
Entrypoint has household routes.

A call can name a household job, a `household_jobs` row the Simulation
Entrypoint inserts pending, with the `household_job_id` keyword argument. As
the Modal worker role, the function then claims the job under its own Modal
call ID, while the job is pending or running and records no call or this
call, and records the outcome on it: a `HouseholdExecutionResult` as the
job's `result`, with status `succeeded`, or a `HouseholdExecutionFailure` as
its `result`, with status `failed` and a one-line summary (the error code and
message, at most 500 characters) as its `error_message`. A job another call
holds, or one already finished, returns `deduplicated`, the job's ID and its
status, without calculating. An error outside the calculation, or Modal's
cancellation of the call, fails the job with a summary naming only the
error's type and no `result`, then propagates; a pre-emption records nothing,
because Modal runs the call again under the same call ID. Each write sets
only columns the role may update: `status`, `modal_call_id`,
`error_message`, `result`, `started_at`, `completed_at` and `updated_at`. A
call that names no job, as deployment validation makes, touches no database.

The stuck-run sweep (`src/modal/run_sweep_app.py`) fails household jobs
nothing will finish by the rule it applies to report runs and simulation
runs, counting a job's age from its creation: a pending job with no call
recorded ten minutes after its creation, a pending or running job once
Modal reports its recorded call ended, and any job still unfinished 24 hours
after its creation, whatever Modal reports.

- Image: the CA store, the frozen `modal-simulation-image` dependency group
  and the source packages, without the bundle's datasets or the Stage 12
  cache. Building it needs no secret.
- Secrets: only `policyengine-modal-worker-database`, whose
  `MODAL_WORKER_DATABASE_URL` connects as the Modal worker role, to record
  the household job a call names. Like the coordinator and the simulation
  workers, the application cannot deploy to a Modal environment without it.
  A household calculation reads no private data or Cloud Storage. Telemetry
  authenticates with the identity token Modal gives every container
  (`MODAL_IDENTITY_TOKEN`) and the workload identity settings baked into the
  image at deployment.
- Limits: 2 CPUs, 4,096 MiB of reserved and 16,384 MiB of maximum memory, a
  900-second timeout, no retries, and a Modal `max_containers` value of 20.
- Each container loads a country model the first time it calculates that
  country and keeps it for later calls.

Deployment validation calls each household function with a one-adult,
current-law household and requires a successful calculation with the deployed
bundle, reported by that function, with a number for the household's net
income. The validation record keeps each invocation ID, and the manifest
publisher records a country's `household_callable` only when the record shows
its function was validated. A failed household validation fails v2 worker
validation like any other validation failure: the manifest is not published,
and, as described under "Release and deployment constraints", the deployment
does not promote the new Simulation Entrypoint revision.

## Temporary direct runner endpoint

> **Temporary Stage 12 interface:** The authenticated routes in this section
> exist only for controlled direct invocation and inspection while the v2
> computation path is non-serving. Remove them during Stage 14 when
> authoritative v2 report submission and polling use the production
> `simulations`, `reports`, and `report_runs` records. They are intentionally
> excluded from the generated OpenAPI document and clients.

The existing Cloud Run Simulation Entrypoint exposes these operator-only routes:

- `POST /internal/stage12/reports` creates one direct Stage 12 report execution;
- `GET /internal/stage12/reports/{evaluation_id}` reads its temporary parent and
  child execution metadata.

Both routes enforce the Simulation Entrypoint's existing bearer authentication
and require the `stage12:operate` permission. Missing permissions are only
logged until `SIMULATION_ENTRYPOINT_ENFORCE_PERMISSIONS=1`; see the
entrypoint's `DEPLOYMENT.md`.
`POST /internal/stage12/reports` accepts new work only when
`STAGE12_ENABLED=1`; with a missing or zero value it returns HTTP 503 without
creating a parent record or invoking Modal. The status route remains available
whenever the Stage 12 resources are configured so authorized operators can
inspect runs that were already submitted.

The direct submission does not call the existing production computation and
does not keep a Cloud Run request open while calculations run:

```text
authenticated caller
        |
        | POST /internal/stage12/reports
        v
Cloud Run Simulation Entrypoint
        | validate request
        | resolve separate v2 manifest
        | prepare parent metadata in memory
        | request a Modal coordinator invocation
        | wait at most five seconds for Modal's acknowledgement
        |
        +------> return the temporary evaluation_id
        |
        v
Modal report coordinator
        | atomically create or resolve the temporary parent row
        | stop if another invocation already owns the same logical run
        |
        +------> baseline logical simulation call ------> 20 region-group calls
        |
        +------> reform logical simulation call --------> 20 region-group calls
        |
        +------> write aggregate artifact and durable state

authenticated caller
        |
        | GET /internal/stage12/reports/{evaluation_id}
        v
Cloud Run Simulation Entrypoint
        |
        | typed SQLAlchemy Core read
        v
API-owned temporary PostgreSQL tables
```

The Modal report coordinator waits for the child Modal calls. Cloud Run does
not poll a Modal `FunctionCall`; the client polls Cloud Run, and Cloud Run reads
the durable state directly from the API-owned temporary tables through the
shared typed persistence package.

Example submission:

```bash
curl -X POST "${SIMULATION_ENTRYPOINT_URL}/internal/stage12/reports" \
  -H "Authorization: Bearer ${SIMULATION_ENTRYPOINT_TOKEN}" \
  -H "Content-Type: application/json" \
  -H "X-PolicyEngine-Request-ID: stage12-manual-example" \
  --data '{
    "country": "us",
    "scope": "macro",
    "region": "us",
    "time_period": "2026",
    "segmented": true,
    "baseline": {},
    "reform": {
      "gov.irs.credits.ctc.amount.base[0].amount": {
        "2026-01-01.2100-12-31": 3000
      }
    }
  }'
```

Policy values may be scalars, ISO effective-date mappings, or frontend interval
mappings in `YYYY-MM-DD.YYYY-MM-DD` form. A bare year such as `"2026"` is not
a valid effective-period key.

The `segmented` field is optional. Omitting it uses segmentation when the
request meets the eligibility conditions above. `false` explicitly selects the
single-container path. `true` requests segmentation but still falls back to
the single-container path when the output requirements are not eligible.

An accepted request returns `202` and `Retry-After: 1` without waiting for the
calculations or for the coordinator's first database write:

```json
{
  "evaluation_id": "00000000-0000-0000-0000-000000000001",
  "status": "running",
  "poll_url": "/internal/stage12/reports/00000000-0000-0000-0000-000000000001"
}
```

Each POST intentionally creates a new execution. After receiving the
`evaluation_id`, callers use the GET route for all subsequent status checks.
An immediate first GET can return `404` with `Retry-After: 1` while the Modal
coordinator is starting and has not created the parent row; callers must bound
retries because the same status also represents an identifier that does not
exist.
While the report is pending or running, GET returns `202` and `Retry-After: 5`;
completed, failed, incomplete, or skipped states return `200`. The response
contains the complete temporary parent and child execution metadata, including
private artifact references and digests, but never returns artifact contents.
For a failed report, the parent record contains the originating safe failure;
the failed simulation contains the same values, and a peer stopped because of
that failure is `failed` with `simulation_cancelled`; its `error_summary`
gives the reason.
Cloud Run therefore requires no additional object-storage permission for this
interface.

## Automatic execution, result comparison, and rollback

The deployment workflow passes the environment-specific `STAGE12_ENABLED`
GitHub variable to Cloud Run. Set the `beta` value to `1` and deploy to enable
staging after qualification. Set it back to `0` and deploy to stop new
automatic and direct staging runs. Production uses the same explicit
deployment sequence with the `prod` variable. Changing this value does not
deploy or remove Modal workers, does not make existing run metadata
unavailable, and does not change the existing production forwarding
configuration.

When enabled, a successful production submission causes the Simulation
Entrypoint to derive a deterministic temporary evaluation identifier from the
production job and selected v2 release, prepare the parent metadata in memory,
and request an invocation of the already-deployed Modal report coordinator.
Cloud Run performs no Stage 12 persistence write on this submission path. It waits
only for Modal to acknowledge the invocation, for at most five seconds. There
is no process-local queue. An acknowledgement failure or timeout is logged, and
the unchanged production response is returned.

The Modal report coordinator asks the API's canonical service to atomically
create or resolve the parent record before starting child work. Repeated
production submissions can request more than one coordinator invocation, but
the deterministic evaluation identifier and database uniqueness constraint
make them one logical Stage 12 run. A later
coordinator that finds the run already active or complete exits without
starting child simulations. Unsupported automatic inputs, and US state
requests (see "Initial flow"), are logged with a bounded reason and are not
persisted.

The report coordinator starts baseline and reform as independent Modal calls,
observes both until they succeed, and derives the Stage 12 aggregate. It then restores the
production Modal function call by the retained production job identifier and
waits up to 15 minutes for that production call to finish. It compares both
complete aggregate result objects exactly. The private
`reports/comparison.json` receipt contains:

- the SHA-256 digest of each complete result object;
- every differing scalar leaf, addressed by JSON Pointer;
- presence and both values for each difference; and
- absolute and relative deltas when both values are numeric.

The receipt does not duplicate either complete result object. Its URI, digest,
schema version, completion time, and status are written to the temporary parent
row. Comparison retrieval, calculation, artifact, or persistence failure cannot
change the already-successful Stage 12 aggregate and cannot affect the existing
production result. The comparison is attempted once for each Stage 12
execution. A failed comparison is final for that execution, and ordinary or
repeated production submissions do not retry it. Any future operator-initiated
comparison retry requires a separately reviewed entry point and state
transition. Direct Stage 12-only runs have no production result and keep
comparison status `not_requested`.

The artifact bucket deletes private Stage 12 objects after 30 days. The
simulation output bucket deletes nothing (see "Where output files are kept").
This application registers no periodic cleanup function and does not delete the
temporary PostgreSQL rows. Those rows remain restricted operational history
until Stage 14 removes the temporary schema.

Household computation, public response fields, public identifiers, and
production persistence are outside this companion change; budget windows on
the detailed-save path are described under "Budget windows" above.

## Numerical qualification

The executor provides `src.modal.utils.qualify_stage12_parity` for a controlled
calculation. It accepts the existing combined request and the corresponding
canonical report input. The command runs the existing combined executor with
its private microdata export, starts the two v2 single-simulation calculations,
compares every entity, row identity, column, dtype, and value, rebuilds the v2
aggregate, and compares every aggregate field. Differences fail qualification
with only a bounded code and field path; calculation values are not included in
the diagnostic or receipt. Numerical tolerances default to zero and can be
introduced only for explicit field paths through a reviewed JSON file.
For an eligible US national input, the Stage 12 side of qualification executes
and merges all 20 region groups separately for both baseline and reform. This
checks the partition and merge numerically in addition to comparing the final
entity tables and aggregate report with the existing combined executor. Inputs
that are not eligible for segmentation continue through the single-container
Stage 12 calculation.
