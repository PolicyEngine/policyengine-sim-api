# policyengine-simulation-entry

The permanent Cloud Run entrypoint for PolicyEngine simulation submission and
polling.

During migration Stage 5 this service is a contract-compatible control-plane
proxy backed by the existing Modal-hosted simulation gateway. It authenticates
callers, uses its own machine identity for the Modal hop, and preserves existing
job identifiers. It does not yet own job persistence, version routing, or
compute dispatch.

## Local development

    uv sync --extra test
    uv run pytest tests/ -v

The production app is policyengine_simulation_entry.app:app. Required runtime
configuration is documented in policyengine_simulation_entry.config.Settings.
The service does not require its own public URL as runtime configuration.
Cloud Run's generated revision name is returned in a response header so
deployment checks can prove which revision a tagged or stable URL serves.

Live caller/backend authentication checks are isolated in `authenticated_tests`.
They qualify tagged Cloud Run candidates before an environment's stable traffic
changes and verify its stable endpoint immediately after promotion, in both
staging and production.

## Caller authentication and permissions

Protected routes accept Auth0 RS256 access tokens issued by
`SIMULATION_ENTRYPOINT_AUTH_ISSUER` for `SIMULATION_ENTRYPOINT_AUTH_AUDIENCE`.
A missing or invalid token returns 403. Each protected route also requires one
Auth0 permission:

| Route | Permission |
|---|---|
| `POST /simulate/economy/comparison` | `simulation:submit` |
| `POST /simulate/economy/budget-window` | `simulation:submit` |
| `GET /jobs/{job_id}` | `simulation:read` |
| `GET /budget-window-jobs/{batch_job_id}` | `simulation:read` |
| `POST /internal/stage12/reports` | `stage12:operate` |
| `GET /internal/stage12/reports/{evaluation_id}` | `stage12:operate` |
| `POST /household-calculations` | `simulation:submit` |
| `GET /household-calculations/{household_job_id}` | `simulation:read` |
| `POST /reports` | `simulation:submit` |
| `GET /reports/{report_id}` | `simulation:read` |
| `POST /simulations` | `simulation:submit` |
| `GET /simulations/{simulation_id}` | `simulation:read` |

`GET /health`, `GET /ready`, `GET /versions`, `GET /versions/{kind}` and
`POST /ping` stay public. The entrypoint answers `GET /health` and `POST /ping`
itself, without calling the old gateway: `/ping` returns the request's `value`
plus one as `incremented`, the same answer the old gateway gives. Their
responses carry no `X-PolicyEngine-Simulation-Backend` header and record no
old-gateway telemetry events.

A caller holds a permission when it appears in the token's `permissions` claim
(Auth0 RBAC). For a client-credentials (machine) token, an entry in its
space-delimited `scope` claim also counts. A user token's `scope` never does,
because without RBAC Auth0 puts any requested API scope there.
`SIMULATION_ENTRYPOINT_ENFORCE_PERMISSIONS` decides what happens when the
permission is missing:

- `0`, or unset: log only. The request proceeds, and the service emits a
  `simulation_entry_permission_missing` event with the `permission`,
  `enforced=false`, and the token's `azp` as `client_id`. Tokens and other
  claims are never recorded.
- `1`: the request is rejected with 403, and the same event is emitted with
  `enforced=true`.

Any other value stops the service from starting. When caller authentication is
disabled (`SIMULATION_ENTRYPOINT_AUTH_REQUIRED=0`, which production refuses),
there is no caller identity and every protected route is allowed. The Auth0
setup required before enforcement is described in `DEPLOYMENT.md`.

## Household calculations

`POST /household-calculations` names a stored household, and optionally a
stored policy, of the Public API: `country`, `household_id`, `policy_id`,
`add_missing_variables`, `axes` (within the household contract's limits)
and `spm` (SPM settings as an object; omit it to use the household's saved
selection, since null is refused). The service reads the rows itself,
calculates the household's `default_year` on the v2 version manifest's
default worker, creates a `household_jobs` row and starts the household
function's call with `spawn`, answering 202 with the job's `job_id` and
`status`. Each submission creates a new job.
`GET /household-calculations/{household_job_id}` returns the job's status,
the household function's `result` document once it has one, and its
`error_message`.

A submission is refused before anything is written: with 400 and a `code`
when the household, policy or catalog version for the default worker's .py
version is missing, or the default worker has no household function for the
country; with 422, one reason per field, when the stored household has no
default year, the policy sets parameters that catalog version lacks, or the
household contract refuses the stored data. A job naming a row deleted
since the reads is refused by the database's foreign keys, also with 400.
The routes answer 503 with `Retry-After` when the manifest or the database
can't be read. When Modal refuses the call, or doesn't confirm it within ten
seconds, the job is failed at once and the 503 (`modal_call_refused`) names
it; a job the household function claimed meanwhile is answered with 202 as
it stands, and a job whose state couldn't then be recorded or read with 202
`pending`, to be read rather than submitted again (the restart check starts
its call once more 3 to 10 minutes after its creation, and the stuck-run
sweep fails it if no call ever starts). Every 500 of either route, from an unexpected
failure or a request the service built and the household contract refuses,
is the JSON error with code `internal_error`. `DEPLOYMENT.md` describes the deployment settings and the
staging check.

## Restarting a run whose call never started

When the service stops between creating a household job, a report run or a
standalone simulation run and starting its Modal call, nothing records a
call for it. The restart check (`run_restarts`, with `run_restart_calls` and
`run_restart_check`) starts such a row's call once more. It doesn't run in
this service: the stuck-run sweep's Modal application
(`projects/policyengine-simulation-executor/src/modal/run_sweep_app.py`)
runs it every two minutes as the Modal worker role.

- It takes the rows created 3 to 10 minutes ago that are still `pending`
  with no call and that nothing has changed since their insert, at most 20
  a check, oldest first. A budget window's report runs and household
  simulations' runs are left out. After three minutes a check marks no more
  rows and leaves the rest for the next.
- It rebuilds each call with this service's own builders
  (`report_run_calculation`, `simulation_run_calculation`,
  `prepare_household_job`) from the stored rows, on the manifest's worker
  for the .py version the row records, even when that worker is no longer
  the default. The context's request ID is the check's own
  (`run-restart-<uuid>`), and its creation time is the row's.
- It sets the row's `updated_at` before starting the call, so a row is
  restarted at most once, then records the call's ID with the submission's
  own conditional write. The restart counts as done when the row then
  records its call; a row recording another call was claimed by that call
  first.
- A row whose call can't be rebuilt, or whose restart Modal refuses or
  doesn't confirm within ten seconds, is failed with
  `run_not_started: The run stayed pending with no Modal call recorded for
  it, and …`; a report run is failed with its simulation runs still pending
  with no call. A row is left unmarked for the next check when its rows or
  manifest can't be read now, its rebuild raises an unexpected error
  (logged with its traceback), its mark can't be written, or its call can't
  be rebuilt and its failure can't be written.
- Each check, each row and each outcome are telemetry; see
  `run_restart_check`'s docstring for the operations and event.

## Economy reports and simulations

`POST /reports` creates an economy report: a baseline and a reform
simulation of one region's data for one year. It names the `country`,
`report_type` (`economy`), `label`, `description`, `year`, `region_code`
and `dataset_name` (a region and dataset of the country's catalog version
for the default worker's .py version; null for the region's default
dataset), each side's stored `policy_id` and `dynamic_id` (null for current
law), `include_cliffs`, and `spm` (SPM settings; omit it, since null is
refused). `POST /simulations` creates a standalone economy simulation from
the same fields for one policy and dynamic. Neither names a .py version or a
record ID: the service runs the v2 version manifest's default worker and
chooses every ID.

The service reads the rows itself and, in one transaction, creates the
records and their first runs, `pending` with no Modal call ID. A report
side reuses a simulation run that already succeeded instead of creating
one, when it is the newest run of the same policy, dynamic, catalog
version, dataset, region and year on the same country package and .py
versions, its output file is in the output bucket
(`V2_SIMULATION_OUTPUT_BUCKET`), a succeeded report run with the same
options used it, and no report run has refused to reuse it; the two sides
never name the same run. The service then starts the run's Modal call by
name: the report run's coordinator, given the report and its run IDs and no
Stage 12 evaluation record, or a standalone run's simulation function,
given its run's ID. It records the call's ID, and answers 201 with the
record's ID, its run's ID and status, and, for a report, each side's
simulation and run and whether it was reused.

A request is refused before anything is written: with 400 and a `code` when
a row it names is missing (`unknown_region`, `unknown_dataset` and
`unknown_dynamic` name the caller's own choice; `unknown_catalog_version`,
`unknown_policy`, `no_economy_worker`, `dataset_not_in_bundle` and
`missing_reference` are the service's state, such as a catalog version
missing while a release deploys), and with 422 and a `code` when a policy or
dynamic sets parameters the catalog version lacks (`missing_parameters`) or
both set one (`overlapping_parameters`). The routes answer 503 with
`Retry-After` when the manifest or the database can't be read. When Modal
refuses the call, or doesn't confirm it within ten seconds, the run (and a
report run's new simulation runs) is failed at once and the 503
(`modal_call_refused`) names the record, as `report_id` or `simulation_id`;
a run claimed meanwhile is answered with 201. Every 500 is the JSON error
with code `internal_error`, and may follow a write. No `detail` holds an
exception's text.

### Budget windows

`POST /reports` with `report_type` `budget_window` creates a budget-window
report: the same baseline and reform in each year of a window that starts
at `year` and lasts `budget_window_size` years (1 to 75, ending by 2200).
It names the same fields as an economy report, except `include_cliffs`,
which it refuses; it stores the options of a report without cliffs, so its
years and single-year reports reuse each other's runs. In one transaction
the service creates the report (an economy report whose
`budget_window_size` is set), one `budget_window_runs` row, and for each
year a report run naming it and the year, with that year's own simulations
unless a side reuses a run of the same year. It then starts up to the
earliest five years, as its own role, under the window run's row lock
(`BudgetWindowStore.advance_window`): the first year's coordinator input is
built as an economy report's, and each later year's is derived from it.
Each year's coordinator starts the next years when its year ends. The 201
names the report, the first year's report run, the window run, its status
and every year's runs. A year Modal refuses fails the window, its waiting
years and their new simulation runs, and so does the first year when Modal
doesn't confirm it within ten seconds; the 503 (`modal_call_refused`) names
the report. The window run's
status needs the update grant on `budget_window_runs` that the Public API
gives the service (PolicyEngine/policyengine-api#4267); `/ready` checks it.
The first years share that one ten-second wait for Modal, counted from the
first start: a later year not confirmed within it waits, with the years
after it, for a started year's coordinator to start it. A body with no
`report_type` is refused for that alone, before its other fields are
checked.
`GET /reports/{report_id}` answers a window with one of its years' runs;
the window's own status is in `budget_window_runs`, which the Public API
reads.

`GET /reports/{report_id}` and `GET /simulations/{simulation_id}` return the
record and its newest run (created last, ties to the larger ID): its status,
versions, error message and times, and a report run's simulation runs. The
Public API reads results from the tables itself.

