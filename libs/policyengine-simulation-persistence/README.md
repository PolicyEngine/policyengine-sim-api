# PolicyEngine simulation persistence

Database connections for the simulation service (the Simulation Entrypoint on
Cloud Run). The service connects to the Public API's Postgres database with
its own role, and writes rows in tables the Public API's migration chain
creates. This repository has no schema or migration chain of its own.

The temporary Stage 12 comparison tables are not part of this library:
`libs/policyengine-stage12-persistence` writes them with its own connection.

## Connections

`create_simulation_engine` builds the runtime engine from the canonical
`postgresql://` URL the deployment stores, selecting the psycopg 3 driver. It
keeps a small pool, checks connections before use, and never prepares
server-side statements, so the same URL works through Supabase's session and
transaction poolers. A malformed URL raises `ValueError` without repeating the
URL, which holds a password.

## Tables

`tables.py` models, with SQLAlchemy Core, the six tables the service creates
rows in: `simulations`, `reports`, `simulation_runs`, `report_runs`,
`budget_window_runs` and `household_jobs`, all in `service_metadata`,
which names the `public` schema so writes reach those tables whatever the
role's `search_path`.
Each model matches the table the Public API's v2 migration chain builds:
every column with its type, nullability and server default, and every
constraint and index under the chain's name. The models only describe the
tables; runtime code never creates or changes them. Status and other enum
columns use the `StrEnum` classes beside them and store each member's value.

Each service writes only its own tables, and the foreign keys between them
stay: 20 keys name rows the Public API writes in its users, policies,
dynamics, households, datasets, regions and model catalog tables. PostgreSQL
checks each of them on insert, as the referenced table's owner, so the
service's role needs no privilege on those tables for it, and a row naming
one that doesn't exist fails with SQLSTATE `23503`. Those tables are modelled
in `referenced_metadata` by their `id` alone, the column every key names, so
SQLAlchemy can declare the keys; nothing reads or writes through them. Code
that builds the models' tables in a test database creates
`referenced_metadata`'s tables first.

A report run names the two simulation runs it compares
(`baseline_simulation_run_id`, and `reform_simulation_run_id`, empty for a
household report with only a baseline). A report with a `budget_window_size`
is calculated as a `budget_window_runs` row with one report run per year; each
year's report run names that row and its year, and belongs to the same report.

## Input tables

`input_tables.py` holds read-only models of the Public API's tables a
calculation is read from: `households`, `policies`, `dynamics`, `parameters`,
`parameter_values`, `regions`, `datasets`, `tax_benefit_models` and
`tax_benefit_model_versions`, in `input_metadata`. The service only reads them, with `SELECT`. Each names only
the columns the service reads, so they are not models of the whole tables,
and they are kept apart from the models of the tables the service writes
(`service_metadata`) and from the tables those name by `id` alone
(`referenced_metadata`).

## Household jobs

`household_job_records.py` holds the simulation service's reads and writes
for a household calculation, one `household_jobs` row. The service finds the
catalog version by country and .py version (`read_catalog_version_id`) and
the stored household in its country (`read_stored_household`), creates the
job `pending` with no Modal call ID and no dynamic (`insert_household_job`),
starts the household function's call, then records the call's ID
(`record_household_call`), or fails the job at once when Modal refuses the
call (`fail_unstarted_household_job`). Both updates apply only while the job
is still `pending` with no call ID, and set `updated_at` themselves. A job
naming a household, policy or catalog version that does not exist is refused
by its foreign key with SQLSTATE `23503`; `HouseholdJobReferenceError` names
the column. Each function takes a connection, so the caller chooses the
transactions. `HouseholdJobStore` runs them as the Simulation Entrypoint
does, each step in a transaction of its own: `read_inputs` reads the catalog
version, the household and the policy's reform values together, and
`create_job`, `record_call`, `fail_unstarted` and `read` each write or read
the job. `HouseholdJobStore.from_engine` opens each with `bounded_transaction`,
which commits when its block ends and sets `statement_timeout` with
`set_config`, local to the transaction, so the database ends a statement that
runs too long and the limit never stays on a pooled connection.

## Economy simulations and reports

`economy_run_records.py` holds the simulation service's reads and writes for
an economy report, or a standalone economy simulation. It reads the
country's model (`read_model_id`) and a catalog version's region by code
(`read_region`) and dataset by name or ID (`read_dataset`,
`read_dataset_by_id`). `insert_economy_report` creates, in the caller's
transaction, each side's new simulation and first run, the report naming
both simulations and the reform's policy with its options in `inputs`, and
its first report run, trigger `initial`; every run is `pending` with no
Modal call ID. A side may instead name a run that already succeeded
(`ReportSimulation.reused`), for which nothing is created.
`insert_economy_simulation` creates a standalone simulation and its first
run. A row naming one that does not exist is refused by its foreign key with
SQLSTATE `23503`; `EconomyRecordReferenceError` names the table and column.

`find_reusable_run` returns the run a new report's side may reuse: the
newest, by completion, of the same economy simulation (policy, dynamic,
catalog version, dataset, region and year) that succeeded on the same
country package and .py versions, whose output file is under the output
bucket's `gs://<bucket>/`, that a succeeded report run with the same options
used, and that no report run refused to reuse (an error message starting
`reused_simulation_`). So a submission after a refused reuse calculates the
simulation again.

`record_report_run_call` and `record_simulation_run_call` record a Modal
call's ID, and `fail_unstarted_report_run` and
`fail_unstarted_simulation_run` fail a run at once, each only while it is
`pending` with no call ID; failing a report run also fails the simulation
runs created for it. `economy_run_reads.py` reads a report, or a simulation,
with its newest run's state; a report run's state includes the Modal call
recorded on it.

`EconomyRunStore` runs them as the Simulation Entrypoint does, each step in
a transaction of its own: `read_report_inputs` reads the catalog version,
model, region and dataset, each side's policy and dynamic values, and each
side's reusable run together, never giving both sides the same run;
`read_simulation_inputs` does the same for one simulation; the writes and
reads each take their own. A missing region or dataset raises
`UnknownRegionError` or `UnknownDatasetError`, and a report side's refused
policy or dynamic raises `ReportSideError`, naming the side. A create whose
commit fails after its rows were inserted raises
`EconomyRecordOutcomeUnknownError`: PostgreSQL may have applied the commit,
so the rows may exist.

## Household reports

`household_report_records.py` holds the simulation service's writes for a
household report: one stored household, for one year, under a baseline and,
optionally, a reform. `insert_household_report` creates, in the caller's
transaction, a household simulation (no dataset or region) and its first
run for each side, never reusing one; the report, of type `household`,
naming the household, both simulations and the reform's policy, with its
options in `inputs`; and its first report run, trigger `initial`, naming no
reform run for a report of the baseline alone. Every run is `pending` with
no Modal call ID, and a row naming one that does not exist is refused as
`EconomyRecordReferenceError`.

`HouseholdReportStore` runs the steps in transactions of their own:
`read_inputs` reads the catalog version for the .py version, the country's
model, the stored household and each side's policy values together,
raising `UnknownCatalogVersionError`, `UnknownHouseholdError` or
`ReportSideError` for the side whose policy is missing or sets parameters
the catalog version lacks; `create_report` raises
`EconomyRecordOutcomeUnknownError` when the commit fails after the rows were
inserted; and `record_report_call` and `fail_unstarted_report` use the
economy report run writes.

## Reading a policy

`read_policy_reform` (`policy_reform.py`) reads a stored policy as the reform
values a calculation applies, `{parameter name: {period: value}}`: the shape
of the Public API's policy JSON, which the household functions' contract and
the economy requests take. The Public API stores a policy's values against
the parameters of the catalog version it was saved on, so a calculation on
any catalog version applies them by name; a parameter the calculation's
version lacks raises `MissingPolicyParametersError`, naming each one, and a
policy the country doesn't have raises `UnknownPolicyError`.
`read_dynamic_reform` reads a stored dynamic the same way (`UnknownDynamicError`
for none), and `combine_reforms` joins a simulation's policy and dynamic
values, refusing a parameter both set (`OverlappingReformError`).

Each period is a range, `YYYY-MM-DD.YYYY-MM-DD`, with the stored dates read
in UTC. A value with no end date follows .py's own rule
(`reform_dict_from_parameter_values`): it stops the day before the same
parameter's next later value starts, so once a later bounded value ends
current law applies, and otherwise runs to 31 December 2200, the latest year
a household job or simulation run can be for, where .py stops at 2100. A
start date alone would not do for that last value: policyengine-core reads it
as "from that day on", but policyengine-uk's reform dictionary reads it as
that one day. The executor's
`test_reform_periods_apply_in_2200_only_through_a_stop_in_2200` shows how
both countries' household functions apply each form in 2200, and its
`test_py_reform_dicts.py` pins .py's own periods for the cases the reader's
tests check. The reader gives .py's periods, in .py's order, for the values
in start order, with one difference besides 2200: it leaves out an
open-ended value whose range would end before it starts (one followed the
same day by a later value, or one starting after 2200). .py gives such a
value a range that ends before it starts too. policyengine-core applies that
range on no day, but policyengine-uk refuses it (its `interval_bounds`
raises "ends before it starts"), so a UK calculation given .py's dict fails
where one given the reader's runs. Bounded values starting the same day are
all kept, in start order, as .py keeps them.

## Checking the models against the Public API

Rule `sim.persistence.models-match-public-api`. The PR workflow's
`Public API schema` job checks out the Public API at the commit pinned in
`public-api-commit`, builds its tables with its own v2 migration chain and
grants the privileges its manifest declares
(`.github/scripts/public-api-schema-reference.sh`), then runs the tests
marked `public_api_schema`. `test_public_api_schema` builds the models' tables
from `service_metadata` in a disposable database, after the Public API's
tables their keys name (`referenced_metadata`, by `id` alone), and compares
the models' tables with the Public API's through PostgreSQL's catalog
(`tests/schema_comparison.py`): every column's type, nullability and default,
every constraint and index by name and definition, and every enum's values
must match. `test_the_input_models_match_the_public_api_columns` builds
the read-only input models' tables in another disposable database and
requires every column they name to exist in the Public API's table with the
same type and nullability; their other columns, defaults, constraints and
indexes are the Public API's alone. `test_service_writes` creates a row in every modelled
table as the service's role. Another test, outside the rule, requires the modelled
tables' foreign keys to the Public API's users, policies, dynamics,
households, datasets, regions and model catalog tables to be exactly the 20
in `tests/public_api_keys.py`, each by its own table, its name and the table
it names, so a key dropped, added, renamed, moved or pointed at another table
shows even if the models follow it. Last, the job requires the
pinned commit to be on the Public API's `master` branch, so the models only
ever describe a chain the Public API has merged.

**Choosing the pin.** Pin the Public API commit whose migration chain the
models describe: the merge commit of the Public API change that last
altered a modelled table, or, while that change is still a pull request, its
head commit. Until that pull request merges, the job's last step fails and
says so. A merge commit keeps the pull request's head on `master`; if the
Public API squashes or rebases the pull request instead, its head never
reaches `master`, so move the pin to the commit that landed.

**Keeping it current.** A sim pull request that changes the models moves the
pin in the same change. The `Public API schema drift` workflow runs the same
tests every Monday, and on demand, against the head of the Public API's
`master`. It fails when the Public API has merged a change to a modelled
table, or to the service role's grants, that the models and the pin haven't
followed yet, and also when the Public API's tables can't be built at that
head; its log says which. When it fails, a second job opens an issue labelled
`public-api-schema-drift`, or comments on the one already open, with the
workflow's own token; the comparison job's token stays read-only. The pin
still moves only in a pull request.

To run the comparison locally, build the reference with the script (it
needs an empty `policyengine_v2_alembic_test` database and a checkout of the
Public API at the pinned commit), then:

```bash
SIMULATION_TEST_DATABASE_URL=postgresql://postgres:password@127.0.0.1:5432/postgres \
PUBLIC_API_SCHEMA_DATABASE_URL=postgresql://postgres:password@127.0.0.1:5432/policyengine_v2_alembic_test \
uv run pytest tests/ -m public_api_schema
```

## The role's privileges

`SERVICE_TABLE_PRIVILEGES` lists the table privileges the service's role
needs: `SELECT` and `INSERT` on the tables it creates rows in
(`simulations`, `reports`, `simulation_runs`, `report_runs`,
`budget_window_runs` and `household_jobs`), and `SELECT` on the policy,
dynamic, household and catalog tables a run reads. `SERVICE_UPDATABLE_COLUMNS`
lists the columns it may `UPDATE` on `simulation_runs`, `report_runs` and
`household_jobs`: `status`, `modal_call_id`, `error_message`, `completed_at`
and `updated_at`, to record a Modal call's ID once Modal accepts the call, or
to fail the run when Modal refuses it. On `budget_window_runs` it lists
`status`, `error_message`, `completed_at` and `updated_at`, to fail a window
when Modal refuses one of the first years the service starts. The role holds
`UPDATE` on those columns only, never on a whole table, so the database
refuses any other change to a run. The Public API's privilege manifest grants
them all.

`missing_table_privileges` returns each one the connected role lacks, ordered
by table, as `"INSERT on reports"` or `"UPDATE (modal_call_id) on
report_runs"`; it asks PostgreSQL with `has_table_privilege` and
`has_column_privilege`. A table missing from the `public` schema lacks all of
its privileges, a missing column lacks its own, and a role without the
`USAGE` privilege on that schema is told so first (`"USAGE on schema
public"`), since it can use none of its grants. The check doesn't look for
privileges the role holds beyond these: `UPDATE` on a whole table satisfies
each column's check, and the Public API's own privilege verification fails
when the role holds it. Tables and columns are found through PostgreSQL's
catalog, which needs no privilege. `read_missing_table_privileges` runs that
check in a transaction whose `statement_timeout` it sets with `set_config`,
local to the transaction, so the database ends any statement of the check
that runs too long;
the Simulation Entrypoint's readiness check uses it, on a thread of its own,
reports whether any privilege is missing, and logs the names.

## Tests

```bash
uv sync --extra test
uv run pytest tests/
```

Tests marked `public_api_schema` run in the `Public API schema` jobs; the
rule's proof among them needs no database, and the others need one the
Public API's own v2 migration chain built, with the privileges its manifest
declares granted:
set `PUBLIC_API_SCHEMA_DATABASE_URL` to that database's superuser URL.
`test_service_writes` creates a row in every modelled table as the service's
role and rolls it back; the comparison in `test_public_api_schema` also needs
`SIMULATION_TEST_DATABASE_URL`, to build the models' own tables. Without the
variables they skip.

Tests marked `postgres` need a PostgreSQL server: set
`SIMULATION_TEST_DATABASE_URL` to its superuser URL, such as
`postgresql://postgres:password@127.0.0.1:5432/postgres`. Each test creates and
drops its own database. Without the variable they skip; the PR workflow's
`Simulation database` job sets `SIMULATION_REQUIRE_DATABASE_TESTS=1`, so a
missing server fails them there.
