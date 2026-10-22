# PolicyEngine simulation persistence

SQLAlchemy Core tables and database connections for the Simulation
Entrypoint's own Postgres schema, `simulation`. The Simulation Entrypoint owns
every table in that schema; no other service creates, changes or writes them.

The temporary Stage 12 comparison tables are not part of this schema: they are
owned and migrated by the Public API, and `libs/policyengine-stage12-persistence`
writes them with DML only.

## Ownership rules

These rules are registered in `rules/registry.toml` and checked by
`tests/test_ownership_boundaries.py`:

- **One migration chain** (`sim.persistence.one-migration-chain`). Schema
  changes are made only by the Alembic chain in the repository's
  `migrations/` directory, configured by `migrations/alembic.ini`. It keeps its
  version table at `simulation.alembic_version`. No other Alembic
  configuration, environment or revision directory exists in the repository.
  Runtime code never changes a schema
  (`sim.boundaries.sqlalchemy-and-alembic-only`).
- **The executor does not depend on persistence**
  (`sim.persistence.executor-independent`). The Modal executor and gateway
  never declare, lock or import this library. Only the Simulation Entrypoint
  and the migration job install it.
- **No database credentials in Modal**
  (`sim.persistence.no-modal-database-credentials`). No Modal secret, Modal
  deployment or Modal application source receives or names a database URL:
  neither a variable named like `*DATABASE_URL*` nor a written-out
  `postgresql://` URL, whether in a step's, job's or workflow's environment
  or in a script such a step runs. The one exception is the Stage 12
  comparison's own `STAGE12_DATABASE_URL` (and the name of the secret that
  holds it), until the comparison path is removed. In particular,
  `SIMULATION_DATABASE_URL` and `SIMULATION_MIGRATION_DATABASE_URL` never
  reach Modal.

## Database roles

| Role | Used by | Privileges |
| --- | --- | --- |
| `simulation_migrator` | The deploy workflow's migration job, through `SIMULATION_MIGRATION_DATABASE_URL` | Creates the `simulation` schema if it is missing, and owns every object in it |
| `simulation_runtime` | The Simulation Entrypoint, through `SIMULATION_DATABASE_URL` | `USAGE` on the schema, `SELECT` on the version table, and `SELECT`, `INSERT` and `UPDATE` on the tables the migrations create |

The initial revision grants the runtime role its privileges, including default
privileges on tables the migrator creates later. Both roles must exist before
the first migration runs; an operator creates them. The migrator needs either
`CREATE` on the database or a `simulation` schema created in advance and owned
by it: the environment creates the schema only when it is missing, because
PostgreSQL checks `CREATE` on the database even for
`CREATE SCHEMA IF NOT EXISTS`.

## Deployment

On every deployment, the migration job (`migrate_simulation_database` in
`.github/workflows/simulation-deploy.reusable.yml`) runs
`alembic upgrade head` before the Simulation Entrypoint's candidate revision is
deployed (`sim.persistence.migrate-before-entrypoint`). It is the only job that
receives `SIMULATION_MIGRATION_DATABASE_URL`. If it fails, the entrypoint is not
deployed. The Simulation Entrypoint's readiness check (`GET /ready`) reports
the database's revision next to `HEAD_REVISION`, the revision its image
expects, and the candidate check requires the two to match.

## Connections

`create_simulation_engine` builds the runtime engine from the canonical
`postgresql://` URL the deployment stores, selecting the psycopg 3 driver. It
keeps a small pool, checks connections before use, and never prepares
server-side statements, so the same URL works through Supabase's session and
transaction poolers. A malformed URL raises `ValueError` without repeating the
URL, which holds a password.

`read_schema_revision` returns the migration revision the database has applied,
or `None` before the first migration. `HEAD_REVISION` is the newest revision
this code knows.

## Adding a revision

1. Change the tables in `src/policyengine_simulation_persistence/`.
2. Add a revision under `migrations/versions/` whose `down_revision` is the
   current head, and set `HEAD_REVISION` in `schema.py` to its revision ID.
3. Run the tests against a disposable PostgreSQL server (below). They run the
   chain's upgrade, `alembic check`, downgrade and upgrade cycle.

The schema creation and every pending revision apply in one transaction, so a
failed migration leaves nothing applied. The previous release keeps serving
traffic while a deployment migrates, and a rollback moves traffic back to an
older image without downgrading the schema, so every revision must work with
the code of the release before it: add columns and tables, and remove them
only in a later release once nothing reads them.

Only one migration PR in this chain may merge at a time; another one open at
the same time rebases onto the new head after the first merges.

## Tests

```bash
uv sync --extra test
uv run pytest tests/
```

Tests marked `postgres` need a disposable PostgreSQL server. Point
`SIMULATION_TEST_DATABASE_URL` at its superuser, for example
`postgresql://postgres:password@127.0.0.1:5432/postgres`; each test creates and
drops its own database. Without the variable those tests skip. CI's
`Simulation database` job runs them against `postgres:17` with
`SIMULATION_REQUIRE_DATABASE_TESTS=1`, which turns a skip into a failure.
