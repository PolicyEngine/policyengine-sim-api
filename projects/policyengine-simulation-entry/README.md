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
| `POST /reports/{report_id}/runs` | `simulation:submit` |
| `POST /simulations` | `simulation:submit` |
| `GET /simulations/{simulation_id}` | `simulation:read` |
| `POST /simulations/{simulation_id}/runs` | `simulation:submit` |

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
can't be read and no job was written. When Modal refuses the call, or doesn't
confirm it within ten seconds, the job is failed at once and the 503
(`modal_call_refused`) names it; a job the household function claimed
meanwhile is answered with 202 as it stands, and a job whose state couldn't
then be recorded or read with 202 `pending`, to be read rather than
submitted again (the stuck-run sweep fails it if no call ever starts). Every
500 of either route is the JSON error with code `internal_error`: an
unexpected failure, a request the service built and the household contract
refuses, or a job whose commit failed after its row was inserted. That job
may exist, so the 500 never says nothing was created; no call is started for
it, the stuck-run sweep fails it if it was written, and the failure is
logged as `household_job_outcome_unknown` with the commit error's type.
`DEPLOYMENT.md` describes the deployment settings and the
staging check.

## Economy reports and simulations

`POST /reports` creates an economy report: a baseline and a reform
simulation of one region's data for one year. It names the `country`,
`report_type` (`economy`), `label`, `description`, `year`, `region_code`
(a region of the country's catalog version for the default worker's .py
version), each side's stored `policy_id` and `dynamic_id` (null for current
law), `include_cliffs`, `spm` (SPM settings; omit it, since null is
refused) and, for a UK-wide or nation report, `data_source` (see "Local
areas"). No request names a dataset: a report or simulation runs on the
dataset the catalog version gives its region, and `dataset_name` is refused
with 422. `POST /simulations` creates a standalone economy simulation from
the same fields for one policy and dynamic. Neither names a .py version or a
record ID: the service runs the v2 version manifest's default worker and
chooses every ID.

Identical requests, together or a minute apart, end on the same
simulation, report and run rows. The service reads the rows itself and, in
one transaction, finds or creates each record by a key of its definition
(`definition_key`, with `key_version`): a simulation's is its country,
policy, dynamic, dataset name, region code, year and SPM selection; a
report's is its two sides' policies and dynamics, the dataset, region, year
and SPM selection they share, and its budget window size, with no other
option; a report also stores a `dataset_independent_key`, the same without
the dataset. It then joins the report's run on the default worker's
versions that hasn't failed and wasn't forced, pending, running or
succeeded, or else finds or creates each simulation's run on those
versions, catalog version and year (any that hasn't failed and wasn't
forced, whichever report first calculated it) and creates a report run
naming them, `pending` with no Modal call ID. Current law against current
law names one simulation and run on both sides. A report stores only the
request's SPM selection in `inputs`; `include_cliffs` is never stored,
keyed or passed to the coordinator. Only for a run its own transaction
created does the service start a Modal call by name: the report run's
coordinator, given the report and its run IDs and no Stage 12 evaluation
record, or a standalone run's simulation function, given its run's ID. It
records the call's ID, and answers 201 with the record's ID, the run's ID
and status (as read, for a run it joined), and, for a report, each side's
simulation and run and whether it was reused, which is true for a side
whose run this request didn't create.

A report asking for cliffs (`include_cliffs: true`) gets them as each
simulation run's own calculation (C1 · 24's answer 4b): a run has one
cliff result, its cliff gap and cliff share, in API v1's
`cliff_impacts`, which every report naming the run reads. In the
transaction that ends on the report run, created or joined, the service
also finds or creates the cliff calculation of each simulation run that
report run names: a calculation that hasn't failed, pending, running or
succeeded, is joined, and a failed one never counts, so two identical
requests end on one calculation per run, and a request the database
refuses wrote neither. Only for a calculation it created does the service
start the run's simulation function, given the run's own input, as a
standalone run's, and a context naming the calculation (`cliff_result_id`),
and record the call's ID, before the report run's coordinator is started,
so they start even when Modal then refuses the coordinator. The
calculation runs beside its simulation run, whatever that run's state: it
recalculates the simulation from its dataset and policy. The 201 answer
then also has `cliffs`, each side's `cliff_result_id` and `status`
(`pending` or `running` while it is calculated, `succeeded` once it holds
the result; one run on both sides names one calculation twice); without
`include_cliffs` the field is left out. When Modal refuses a calculation's
call, or doesn't confirm it within ten seconds, the service fails that
calculation, answers its side `failed` and the report as usual, and the
next request asking for cliffs starts another.

A request is refused before anything is written: with 400 and a `code` when
a row it names is missing (`unknown_region` and `unknown_dynamic` name the
caller's own choice, and `unknown_dataset` its region's dataset;
`unknown_catalog_version`, `unknown_policy`, `no_economy_worker`,
`dataset_not_in_bundle` and `missing_reference` are the service's state,
such as a catalog version missing while a release deploys), or, for a report
on the local-area dataset, when the default worker or the catalog can't
serve it (`local_areas_unsupported` and `local_area_dataset_mismatch`, also
the service's state; see "Local areas"); and with 422 and a `code` when a
policy or dynamic sets parameters the catalog version lacks
(`missing_parameters`), both set one (`overlapping_parameters`), or the
release has no local-area dataset for `data_source`
(`no_local_area_dataset`). The routes answer 503 with
`Retry-After` when the manifest or the database can't be read. When Modal
refuses the call, or doesn't confirm it within ten seconds, the run is
failed at once (a report run alone: its simulation runs may already be
another report run's, and the next coordinator naming them starts them)
and the 503 (`modal_call_refused`) names the record, as `report_id` or
`simulation_id`;
a run claimed meanwhile is answered with 201. Every 500 is the JSON error
with code `internal_error`, and may follow a write. No `detail` holds an
exception's text.

### Local areas

An economy report created with `data_source: "local_area_dataset"` runs on
the bundle's local-area dataset, the one the .py release routes
constituencies and local authorities to, in place of its region's dataset.
It is an ordinary report: it returns its full results and its constituency
and local-authority impacts, its simulation runs' cliffs are calculated when
asked, and it is re-run on the same dataset. `data_source` is left out unless given (null and any other
value are refused with 422), and is never stored as an option: the dataset
is stored with the report's simulations. Only a UK report whose region is
`uk` or one of the four `country/*` nations may ask; any other country or
region is refused with 422 at `data_source`.

The default worker's UK entry in the v2 version manifest decides the rest
(`local_area_reports`): the input version its coordinator accepts for a
local-area report run, and the dataset the .py release routes constituency
and local-authority regions to, which the publisher computes by the rule
Stage 12's national aggregation uses to leave the areas out. Before anything
is read:

- without that declaration, or with another input version, the request is
  refused with 400 `local_areas_unsupported`, the service's state;
- with no dataset, the release routes the areas to none of their own, and
  the request is refused with 422 `no_local_area_dataset` at `data_source`:
  that release's national reports calculate both area levels themselves.

With a dataset, the catalog version's constituency and local-authority
regions must all use it, or the request is refused with 400
`local_area_dataset_mismatch`, the service's state; the report is then
calculated on the catalog's row for that dataset.

### Budget windows

`POST /reports` with `report_type` `budget_window` finds or creates a
budget-window report: the same baseline and reform in each year of a window
that starts at `year` and lasts `budget_window_size` years (1 to 75, ending
by 2200). It names the same fields as an economy report, except
`include_cliffs`, which it refuses. Identical window requests end on the
same rows (C1 · 24's answer 8): in one transaction the service finds each
year's simulations by that year's key, which a single-year report of that
year shares, and the report by its key (an economy report whose
`budget_window_size` is set, in its key too); it joins the report's
`budget_window_runs` row on the default worker's catalog and .py versions
that hasn't failed and isn't a recalculation, or else creates one
(recording its .py version) and, for each year, a report run naming it and
the year, on each side's simulation run of that year, found or inserted.
Only for a window run it created does it then start up to the earliest
five years, as its own role, under the window run's row lock
(`BudgetWindowStore.advance_window`): the first year's coordinator input is
built as an economy report's, and each later year's is derived from it. A
joined window run is answered as it is, with no new call.
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

## Household reports

`POST /reports` with `report_type` `household` creates a household report:
one household the Public API stores, for its `default_year`, under a
baseline and, optionally, a reform. It names the `country`, `label`,
`description`, `household_id`, `baseline` and `reform` (each a stored
`policy_id`, null for current law; a null or omitted `reform` makes a report
of the baseline alone). It names no year, region, dataset, dynamic, .py
version or record ID, and has no options: each side adds every variable the
household lacks, as the Public API's stored-household route does, so the
report holds every value, and `reports.inputs` is `{}`. A household's saved
SPM selection is applied to both sides.

The service reads the catalog version for the default worker's .py version,
the household and each side's policy, checks each side's household contract
request, then finds or creates, in one transaction, as an economy report's
create does (C1 · 24's answer 8): each side's household simulation by a key
of its country, policy, household and year; the report (type `household`)
by a key of its sides, household and year; and its run on the current
versions that hasn't failed and wasn't forced, or else a pending report
run naming each side's simulation run on those versions, found or
inserted. A policy compared with itself names one simulation and one run on
both sides. For a report run it created, it starts one call of the
country's household function with the report run's ID
(`household_report_run_id`) and each side's household contract request,
whose calculation ID is the side's simulation run, and records the call's
ID; a run it joined gets no call. A simulation run another report run is
calculating is waited for by the household function (C1 · 26). The household function claims the report run and each
simulation run under its own call, writes each run's result or failure
document to `household_result`, and marks the report run succeeded only if
every side's run did; otherwise it fails with the first such side's reason.
The answer is 201 with the report's and its run's IDs, the run's status,
the `year` and each side's simulation and simulation run, for the run the
request ended on. When Modal refuses the call, only the report run is
failed: its simulation runs may be another report run's.

Refusals and outages answer as an economy report's do, with three more
codes: 400 `unknown_household` and `no_household_function` (the default
worker has no household function for the country), and 422
`household_not_calculable` when the household has no default year or
cannot be read, located at `household_id`, or when the household contract
refuses what the report would calculate, located at the field it traces to:
`household_id` or a side's `policy_id`.
A policy setting parameters the catalog version lacks is 422
`missing_parameters`, located at that side's `policy_id`.

## Re-running a report or simulation

`POST /reports/{report_id}/runs` calculates a stored economy report again,
as a new run on the default worker's versions, and keeps every earlier run.
Its body is required: `{}` or `{"force": false}` re-runs the report, and
`{"force": true}` forces a recalculation. The report keeps its simulations.
Without `force`, a re-run of a report created by key while the report has
a run on the default worker's versions that hasn't failed and wasn't
forced, pending, running or succeeded, joins that run instead and answers
it as it is, with both sides reused and no Modal call; a run still under
way on older versions is no run to join. Otherwise each side joins its
simulation's run on those versions, catalog version and year that hasn't
failed and wasn't forced, in any state, or gets a new one, under a new
report run. `force` calculates both sides again, as an extra run: the new
report run and both simulation runs are marked `forced`, so nothing joins
them and the Public API's one-unfailed-run indexes leave them out, and a
forced re-run is never refused while a run is under way. A report created
before keys (C1 · 27), whose simulations have none, is always re-run that
way, forced or not: its runs may have been calculated with plans that
depended on that report (C1 · 24's interim answer B). The body may also
send `include_cliffs`: an economy report's
re-run, joined or created, forced or not, then starts the cliff
calculation of each simulation run its run names that has none, as a
create does (see "Economy reports and simulations"), and answers them in
`cliffs`. A report stored before C1 · 27 asking for cliffs
(`include_cliffs: true` in its options) asks for them on every re-run too;
its stored option is only read, and never reaches the coordinator. A
budget-window or household report's re-run asking for cliffs is refused
with 422 `cliffs_not_available`, located at `include_cliffs`: neither
calculates them. The service reads the
report's region code and dataset name, finds the rows with that code and
name in the catalog version of the default worker's .py version, and reads
each simulation's stored policy and dynamic on it.
A budget-window report's re-run joins or creates a budget-window run with
a report run per year, as a single-year report's does: a window created by
key, without `force`, joins its window run on the default worker's versions
that hasn't failed and isn't a recalculation, with no new call, or else
gets a new window run whose years' sides join their simulations' runs or
get new ones. `force`, and every re-run of a window created before keys,
recalculates it: `recalculate` is set, and its year runs and new
simulation runs are marked `forced`. Each year's sides are runs of the
simulations that year's report run of the report's newest window run names
(a year that named another report's run names that report's simulation).
Only a window run the re-run created has its first years started. Up to five years start at once, as a new window's do (a later year
Modal doesn't confirm in time waits for an earlier year's coordinator); it
answers 201 with the budget-window create's fields, naming the window run
and every year.
A household report's re-run calculates its stored household again in the
report's own year (not the household's current `default_year`), under each
side's stored policy read on the current catalog version, and joins or
creates its run as an economy report's re-run does: a keyed report's
unforced re-run joins its run on the current versions, or each side joins
its own household simulation's run on them or gets a new one; `force`, and
every re-run of a household report created before keys, makes an extra run
on a new run of each side, all marked `forced`. A report run it created
is started by one household function call, as a new
household report's is, and the re-run answers 201 with the household report
create's fields.
`POST /simulations/{simulation_id}/runs`, with no body, joins a stored
economy simulation's run on the default worker's versions, catalog version
and year that hasn't failed and wasn't forced, pending, running or
succeeded, and answers it as it is, with no Modal call; only when there is
none does it create and start a new run. Each answers 201 with the same
fields as the create, naming the run.

A re-run is refused before anything is written: 404 (`unknown_report`,
`unknown_simulation`) for a record that doesn't exist; 409
`rerun_not_supported` for a household simulation (re-run only
through its report), a household report whose simulation names a dynamic (a
household calculation applies none), a budget window whose newest window run
lacks a year, or a record that names no region, dataset, year, simulation,
household or country the service calculates (a household report names no
region or dataset); and the create's 400 and 422 codes when the stored rows
can't be calculated on the current catalog version, worded for the stored
record (a 422 reason's `loc` starts with `report` or `simulation`), with a
household report's own codes: 400 `unknown_household` and
`no_household_function`, and 422 `household_not_calculable`, located at the
report's `household_id` or a side's `policy_id`. Without household reports
configured, a household report the service could re-run answers 503
`not_configured`. No re-run is refused while a run is under way, and the
409 `run_in_progress` is gone. An unforced re-run of a report created by
key looks for the run to join and inserts under a transaction-level
advisory lock on the report's ID: a second one waits, then joins the
first's run. A simulation's re-run takes no lock: the Public API's index on
runs lets only one run that hasn't failed and wasn't forced exist on a
version set, so concurrent re-runs end on it. A forced re-run takes none
either, as nothing joins it. A re-run's answer names
the run it ended on; `GET /reports/{report_id}` and
`GET /simulations/{simulation_id}` read the newest run, which may be a later
one, such as a forced re-run's. A report or simulation deleted after the
re-run read it answers 404. A report deleted with its simulations answers
400 `missing_reference`, naming a simulation, when a side gets a new run,
and 404 when both sides join a run; a deleted budget-window report always
answers 404, as its window run is created first. When Modal refuses the new
run's call, the run is failed, with a forced run's new simulation runs,
which only it names, and the 503 `modal_call_refused` names the record and
the run (`run_id`).

