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
tables; runtime code never creates or changes them. Their foreign keys stay
between these tables, because another service writes the policy, household
and catalog rows they name. Status and other enum columns use the `StrEnum`
classes beside them and store each member's value.

A report run names the two simulation runs it compares
(`baseline_simulation_run_id`, and `reform_simulation_run_id`, empty for a
household report with only a baseline). A report with a `budget_window_size`
is calculated as a `budget_window_runs` row with one report run per year; each
year's report run names that row and its year, and belongs to the same report.

## The role's privileges

`SERVICE_TABLE_PRIVILEGES` lists the table privileges the service's role
needs: `SELECT` and `INSERT` on the tables it creates rows in
(`simulations`, `reports`, `simulation_runs`, `report_runs`,
`budget_window_runs` and `household_jobs`), and `SELECT` on the policy,
dynamic, household and catalog tables a run reads. The Public API's privilege
manifest grants them. `missing_table_privileges` returns each one the
connected role lacks, as `"INSERT on reports"`; a table missing from the
`public` schema lacks all of its privileges, and a role without the `USAGE`
privilege on that schema is told so first (`"USAGE on schema public"`), since
it can use none of its table grants. Tables are found through PostgreSQL's
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

Tests marked `public_api_schema` run as the service's role in a database
the Public API's own v2 migration chain built, with the privileges its
manifest declares granted: set `PUBLIC_API_SCHEMA_DATABASE_URL` to that
database's superuser URL. They create a row in every modelled table and roll
it back. Without the variable they skip.

Tests marked `postgres` need a PostgreSQL server: set
`SIMULATION_TEST_DATABASE_URL` to its superuser URL, such as
`postgresql://postgres:password@127.0.0.1:5432/postgres`. Each test creates and
drops its own database. Without the variable they skip; the PR workflow's
`Simulation database` job sets `SIMULATION_REQUIRE_DATABASE_TESTS=1`, so a
missing server fails them there.
