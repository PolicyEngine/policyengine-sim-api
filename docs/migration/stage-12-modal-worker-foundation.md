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
- Country-specific single-simulation functions and the report coordinator have
  a Modal `max_containers` value of 10. The separate US region-group function
  has a value of 300 so calculation work cannot wait behind the parent calls
  that are collecting it. These are per-function limits, not one shared quota.
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
  bucket. A conditional IAM binding separately permits it to read
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
  enables every supported newly accepted annual society-wide report and the
  authenticated direct submission route. Any other value prevents service
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
| Create the private report-artifact and cache buckets and their policies | Infrastructure configuration |
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
create/read/delete checks in the private report-artifact and cache buckets. The
temporary objects are deleted in the normal path and by exit cleanup after a
failure.

The current Public API omits `data` for the certified default dataset. The v2
adapter therefore resolves an absent `data` field to the exact default dataset
in the selected `.py` bundle. An explicit `data` value is eligible only when it
identifies an artifact declared by that same bundle. The request model converts
the Public API's `_telemetry` field to `telemetry`; the adapter accepts that
correlation metadata but does not include it in either simulation input.

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

The executor temporarily packages three display-only resources under its
expandable `static_runtime_files/uk_local_authorities/` directory. The v2 Modal
image copies that directory explicitly because Modal's Python-source layer does
not include non-Python files by default:

- one common `code,name` file containing the union of supported authority
  codes;
- one `code,x,y` file for the 374-authority LAD22 configuration; and
- one `code,x,y` file for the 361-authority LAD23 configuration.

The Enhanced FRS geography is LAD22: its 2021 output areas are crosswalked to
2022 local-authority boundaries. The output-area vintage does not make the
local-authority boundary version LAD21. Names and coordinates are attached only
after numeric local-authority impacts have been calculated; these resources
never assign a household to an authority.

The detected boundary version is stored as typed metadata in both child Parquet
artifacts. The coordinator requires baseline and reform to agree before it
enriches the aggregate output. Existing non-Stage-12 calculations continue to
use the existing GCS lookup and are not changed by this path.

This resource ownership is temporary. A future boundary version must add its
codes to the common names file, add a distinct coordinate file, and register a
typed detector definition. Longer term, display-coordinate rendering should
move to the front end, and Stage 14 or later work should remove these temporary
executor resources when the authoritative v2 report architecture supersedes
Stage 12.

## US segmentation and baseline cache

Segmentation applies by default when both simulation inputs describe a US
national society-wide calculation and the shared output plan does not require
cliff analysis or labor-supply responses. `segmented=false` selects the
single-container calculation explicitly. UK calculations, subnational US
calculations, cliff analysis, and labor-supply-response calculations also use
the single-container path. `segmented=true` is accepted, but it does not
override those numerical eligibility conditions.

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
optional axes, under one to ten policies, so a baseline and its reform are
calculated in one call. It takes a `HouseholdExecutionRequest` and returns a
`HouseholdExecutionResult` or a typed `HouseholdExecutionFailure` from
`policyengine_simulation_contract.household_execution`, as JSON. The
calculation follows the Public API's household calculation, including its
input errors, warnings and SPM receipt. A function refuses a request for
another country, and returns a `bundle_mismatch` failure for a request that
names another bundle. Nothing calls these functions until the Simulation
Entrypoint has household routes.

- Image: the CA store, the frozen `modal-simulation-image` dependency group
  and the source packages, without the bundle's datasets or the Stage 12
  cache. Building it needs no secret.
- Secrets: none. A household calculation reads no private data, Cloud
  Storage or database. Telemetry authenticates with the identity token Modal
  gives every container (`MODAL_IDENTITY_TOKEN`) and the workload identity
  settings baked into the image at deployment.
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
starting child simulations. Unsupported automatic inputs are logged with a
bounded reason and are not persisted.

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

The artifact bucket deletes private Stage 12 objects after 30 days. This
application registers no periodic cleanup function and does not delete the
temporary PostgreSQL rows. Those rows remain restricted operational history
until Stage 14 removes the temporary schema.

Household computation, budget-window computation, public response fields,
public identifiers, and production persistence are outside this companion
change.

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
