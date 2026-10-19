# PolicyEngine simulation persistence

SQLAlchemy Core tables and database connections for the Simulation
Entrypoint's own Postgres schema, `simulation`. The Simulation Entrypoint owns
every table in that schema; no other service creates, changes or writes them.

## Ownership rules

These rules are registered in `rules/registry.toml` and checked by
`tests/test_ownership_boundaries.py`:

- **One migration chain** (`sim.persistence.one-migration-chain`). Schema
  changes are made only by the Alembic chain in the repository's
  `migrations/` directory, configured by `migrations/alembic.ini`. It keeps its
  version table at `simulation.alembic_version`. No other Alembic
  configuration, environment or revision directory exists in the repository,
  and runtime source code contains no schema DDL.
- **The executor does not depend on persistence**
  (`sim.persistence.executor-independent`). The Modal executor and gateway
  never declare, lock or import this library. Only the Simulation Entrypoint
  and the migration job install it.
- **No database credentials in Modal**
  (`sim.persistence.no-modal-database-credentials`). No Modal secret or Modal
  deployment receives `SIMULATION_DATABASE_URL` or
  `SIMULATION_MIGRATION_DATABASE_URL`. The Stage 12 comparison's own database
  secret is the only database credential Modal holds, until Stage 12 is
  removed.

The temporary Stage 12 comparison tables are not part of this schema: they are
owned and migrated by the Public API, and `libs/policyengine-stage12-persistence`
writes them with DML only.

## The job store

`simulation.jobs` has one row per calculation the Simulation Entrypoint sends
to Modal: the submitter, a SHA-256 of the job kind, policyengine.py bundle
version and request (`input_sha256`), the Modal application, function and
call ID, the status, timings, the number of collection checks and any error.
`simulation.job_results` holds the validated result of each succeeded job that
is not a report.

A job moves `pending` -> `dispatching` -> `running` -> `succeeded` or
`failed`. Every transition in `jobs.py` is one conditional
`UPDATE ... WHERE status = <expected>` (`sim.persistence.conditional-job-writes`):

- **Submission** inserts with `ON CONFLICT DO NOTHING` on a unique index over
  `input_sha256` that excludes failed jobs, so identical requests share one
  job and a failed job never blocks a new attempt.
- **Dispatch** happens only after the conditional `pending` -> `dispatching`
  update succeeds, so one Modal call starts per job.
- **Collection** writes a result (in the same transaction as the
  `running` -> `succeeded` update) or a failure only while the job is
  `running`. When two collectors race, PostgreSQL makes the second wait for the
  first's row lock, re-check the condition and change nothing. No lease is
  taken. `tests/test_job_store_concurrency.py` forces that interleaving and
  races many threads.

The Simulation Entrypoint collects results in the background: Cloud Tasks
calls its collection route, which checks the Modal call without waiting and
either stores the outcome or schedules the next check with backoff; a Cloud
Scheduler sweep re-enqueues running jobs nobody has checked for five minutes
and fails jobs whose dispatch stalled for ten. Status reads use only these
tables (`sim.persistence.status-reads-never-advance-work`).

## Database roles

| Role | Used by | Privileges |
| --- | --- | --- |
| `simulation_migrator` | The deploy workflow's migration job, through `SIMULATION_MIGRATION_DATABASE_URL` | Creates the `simulation` schema if it is missing, and owns every object in it |
| `simulation_runtime` | The Simulation Entrypoint, through `SIMULATION_DATABASE_URL` | `USAGE` on the schema, `SELECT` on the version table, and `SELECT`, `INSERT` and `UPDATE` on the tables the migrations create |

The initial revision grants the runtime role its privileges, including default
privileges on tables the migrator creates later. Both roles must exist before
the first migration runs; an operator creates them.

## Deployment

On every deployment, the migration job (`migrate_simulation_database` in
`.github/workflows/simulation-deploy.reusable.yml`) runs
`alembic upgrade head` before the Simulation Entrypoint's candidate revision is
deployed. The Simulation Entrypoint's readiness check (`GET /ready`) reports
the database's revision next to `HEAD_REVISION`, the revision its image
expects.

The previous revision keeps serving traffic while the migration runs, and a
rollback moves traffic back to an older image without downgrading the schema.
Every revision must therefore work with code from the release before it:
add columns and tables, and remove them only in a later release once nothing
reads them.

## Adding a revision

1. Change the tables in `src/policyengine_simulation_persistence/`.
2. Add a revision under `migrations/versions/` whose `down_revision` is the
   current head, and set `HEAD_REVISION` in `schema.py` to its revision ID.
3. Run the tests against a disposable PostgreSQL server (below). They run the
   chain's upgrade, `alembic check`, downgrade and upgrade cycle.

Only one migration PR in this chain may be open at a time; a second one rebases
onto the new head after the first merges.

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
