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

## The role's privileges

`SERVICE_TABLE_PRIVILEGES` lists the table privileges the service's role
needs: `SELECT` and `INSERT` on the tables it creates rows in
(`simulations`, `reports`, `simulation_runs`, `report_runs`,
`report_run_simulation_runs` and `household_jobs`), and `SELECT` on the
policy, household and catalog tables a run reads. The Public API's privilege
manifest grants them. `missing_table_privileges` returns each one the
connected role lacks, as `"INSERT on reports"`; a table missing from the
`public` schema lacks all of its privileges. The Simulation Entrypoint's
readiness check reports the result.

## Tests

```bash
uv sync --extra test
uv run pytest tests/
```

Tests marked `postgres` need a PostgreSQL server: set
`SIMULATION_TEST_DATABASE_URL` to its superuser URL, such as
`postgresql://postgres:password@127.0.0.1:5432/postgres`. Each test creates and
drops its own database. Without the variable they skip; the PR workflow's
`Simulation database` job sets `SIMULATION_REQUIRE_DATABASE_TESTS=1`, so a
missing server fails them there.
