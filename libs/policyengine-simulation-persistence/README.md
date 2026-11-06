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
must match. `test_service_writes` creates a row in every modelled table as
the service's role. Another test, outside the rule, requires the modelled
tables' foreign keys to the Public API's users, policies, dynamics,
households, datasets, regions and model catalog tables to be exactly the 20
in `tests/public_api_keys.py`, so a change to them shows even if the models
follow it. Last, the job requires the
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
to fail the run when Modal refuses it. The role holds `UPDATE` on those
columns only, never on a whole table, so the database refuses any other change
to a run. The Public API's privilege manifest grants them all.

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
