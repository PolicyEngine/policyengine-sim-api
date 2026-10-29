# PolicyEngine simulation persistence

SQLAlchemy Core tables and database connections for the Simulation
Entrypoint's own Postgres schema, `simulation`. The Simulation Entrypoint owns
every table in that schema; no other service creates, changes or writes them.

The temporary Stage 12 comparison tables are not part of this schema: they are
owned and migrated by the Public API, and `libs/policyengine-stage12-persistence`
writes them with DML only.

## Tables

| Table | Holds |
| --- | --- |
| `simulations` | What to run, independent of model versions: kind (`economy` or `household`), country, policy (null for current law), dynamic, and either a household or a region code (`state/ca`) with an optional dataset name (null means the region's default dataset in each catalog version) |
| `reports` | Which two simulations a report compares (an economy report needs a reform; a household report may have only a baseline), both of the report's own country and kind, with its year and options, and, for a budget-window report, its window size (1 to 75 years from its year) |
| `simulation_runs` | One execution of a simulation for a catalog version and year: the region code and dataset row it resolved in that version (economy runs only), its status, Modal call ID, timings, error, trigger (`initial`, `manual` or `system`) and attempt number |
| `report_runs` | One calculation of a report: a baseline and a reform simulation run of the same catalog version and year (no reform run for a single-simulation household report), the trigger, and, for an economy report, the comparison step's own status and Modal call; a budget-window report's report run names the budget-window run it is a year of |
| `budget_window_runs` | One calculation of a budget-window report on a catalog version: its trigger, whether its years calculate their simulations again, how many years it calculates at once, its status and error, when the safety check next looks at it, and the lease of the caller starting its next years |

`simulations` and `reports` are definitions: the runtime role may read and
insert them but not update or delete them. Rows migrated from the Public API's v1 database keep their v1
IDs in the unique `legacy_simulation_id` and `legacy_report_id` columns, and
the stores' `create_migrated` methods return the existing row when the same v1
ID is migrated again.

Foreign keys into the Public API's tables in the same database (`policies` and
`households`, each together with its country, `dynamics`,
`tax_benefit_model_versions`, and `regions` and `datasets` each together with
its catalog version) are `ON DELETE RESTRICT`. Creating them needs
`REFERENCES` on those tables for the migrator role; the runtime role needs no
privilege on them.

### Runs

A simulation run's setup is its catalog version (one policyengine.py bundle
version for one country, which pins the country package, data release and
datasets) and its year. Its region and dataset keys include its catalog
version, through the Public API's unique constraints
`uq_regions_model_version_code` and `uq_datasets_id_model_version`, so a run
can use only its own version's rows; the caller resolves them in that version
(the simulation's region code, and the dataset it names or the region's
default dataset). A report run's simulation run keys include its catalog
version and year, so both of its runs share them.

`RunStore.start_simulation_run` reuses the newest attempt for the same
simulation, catalog version and year while it is unfinished, or when it
succeeded within the last 28 days (output files are kept 30); a failed run is
never reused. Otherwise, and always when forced, it adds the next attempt.
Concurrent starts meet at the unique attempt number: an unforced start that
loses the insert returns the other caller's run, and a forced start adds the
attempt after it, so a forced caller always starts a run of its own (it gives
up after 10 lost inserts in a row).
`RunStore.create_report_run` joins one run of each of the report's simulations
and takes the catalog version and year from them; a report's status is its
newest report run's. `RunStore.create_first_report_run` adds a report run only
while the report has none, and otherwise returns its newest: simultaneous
callers wait for each other on a transaction-level advisory lock on the report
(`pg_advisory_xact_lock`), so a report migrated without a run gets one first
run however many readers open it at once. The runtime role may read, insert
and update runs but not delete them.

`RunTransitions` makes every status change one conditional update on the
expected status, so a second writer changes nothing: only the caller that
moves runs from `pending` to `dispatching` starts their Modal call, the runs
one call computes change together, a safety-check pass claims a due call by
moving its next check time (a call's own report needs no claim, since its
outcome is written conditionally too), results are written in the
transaction that marks a run succeeded, and dispatches left `dispatching` (and comparison
steps whose simulation run failed) are failed so the work can be submitted
again. A comparison step can start only once both of its simulation runs
have succeeded; starting it is left to the stage 2 report path.

### Budget-window runs

A budget-window run groups one report run per year of its report's window,
each of the same report and catalog version (a composite foreign key) and at
most one per year (`uq_report_runs_budget_window_run_id_year`).
`BudgetWindowRunStore` creates runs (`create_first_run` adds one only while
the report has none, under a transaction-level advisory lock, as first report
runs are), reads each year's report run with both of its simulation runs, and
reads each year's `budget_impacts` row; nothing stores a total, so readers sum
the years. A caller starting a run's next years first claims it
(`claim_scheduling`, a lease on `scheduling_claimed_until`), and releasing the
claim sets the run's next safety-check time. A run is finished once, with a
conditional write, as `succeeded` or `failed`.

### Results

Every economy result is stored as rows (`result_tables`), one table per
section of today's result model, `SingleYearMacroOutput`:

| Belongs to | Tables |
| --- | --- |
| A simulation run (its own figures, as each report run's comparison computed them) | `poverty_rates`, `inequality`, `cliff_impacts` |
| A report run (the comparison) | `budget_impacts`, `program_budget_impacts`, `decile_impacts` (income and wealth), `intra_decile_impacts` (income and wealth), `labor_supply_responses`, `labor_supply_decile_impacts`, `congressional_district_impacts`, `constituency_impacts`, `local_authority_impacts` |

A reused simulation run's own figures are keyed by the report run and the
side (baseline or reform) too, because they can differ between comparisons
(US poverty rates follow each comparison's SPM selection) and one run can be
both sides of a report run. Labour supply decile rows keep the average and
the relative maps' orders separately, since the average maps may name a
decile the relative maps leave out (-1, for households with negative
income). Lists and maps keep their order in a `position`
column. Result rows are written once and never changed: the runtime role may
read and insert them but not update or delete them. The report run also
records which optional sections its result holds (`result_sections`), its SPM
provenance, and the model and data versions the result reports (a US state
run reports the revision of the dataset it loaded, which is not the release's
default data version). `write_economy_comparison` checks the result can be
held exactly (`check_storable`) and writes its rows in the transaction that
marks the comparison step succeeded (`RunTransitions.complete_comparison`);
`EconomyComparisonRecorder` pairs that writer with validation against the
result model. `read_economy_comparison` rebuilds the result exactly from the
rows and from labels the region catalog supplies (`EconomyResultLabels`):
constituency and local-authority names and map positions by code. Each UK
area's disclosure tier and the 90% interval bounds of its income changes are
stored with its figures (null for a result computed before disclosure
control).

A computed household is one JSON value (`household_outputs`, PostgreSQL
`json`, so its text, key order included, is kept as written) on its
simulation run, with its schema version. `write_household_output` stores it
in the transaction that marks the run succeeded
(`RunTransitions.complete_simulation_call`); a household report's two runs
share one Modal call and each gets its own household. `read_household_output`
returns it unchanged.

`output_files` links each stored file (its role, URI and SHA-256) to the one
simulation run or report run that wrote it. `available_output_files` offers a
link for 30 days after its file was written (`OUTPUT_FILE_RETENTION`), as long
as the artifact bucket keeps it; result rows stay for good.

### Catalog reads

A v2 economy run uses the region, dataset and policy rows of its catalog
version, which the Public API owns in the same database. `CatalogReader`
reads them with typed SQLAlchemy Core selects:

- `catalog_version`: a country's catalog version for a policyengine.py
  version (`tax_benefit_models`, `tax_benefit_model_versions`);
- `population`: the run's region, its default dataset or the one the
  simulation names, and the household column and value that select the
  region (a row filter; a region selected any other way cannot run), with
  each state's filter for a US national run (`regions`, `datasets`);
- `parameter_changes`: the values the run's policy, and its dynamic, set, by
  parameter name and period (a date, or an inclusive `start.end` range); a
  parameter the run's catalog version does not have refuses the run
  (`parameters`, `parameter_values`);
- `area_labels`: UK constituency and local-authority names by code, which a
  stored result is read back with. The catalog has no map positions, so
  they are read back empty.

The runtime role needs `SELECT` on these tables (`CATALOG_READ_TABLES`).
The owner of the Public API's tables grants it, as an operator step:
`GRANT SELECT ON public.tax_benefit_models, public.tax_benefit_model_versions,
public.regions, public.datasets, public.parameters, public.parameter_values
TO simulation_runtime;`

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
| `simulation_migrator` | The deploy workflow's migration job, through `SIMULATION_MIGRATION_DATABASE_URL` | Creates the `simulation` schema if it is missing, and owns every object in it; `REFERENCES` on the Public API tables its foreign keys point to |
| `simulation_runtime` | The Simulation Entrypoint, through `SIMULATION_DATABASE_URL` | `USAGE` on the schema, `SELECT` on the version table, and `SELECT`, `INSERT` and `UPDATE` on the tables the migrations create, except `UPDATE` on the definition tables (`simulations`, `reports`) and the result tables; never `DELETE`; and `SELECT` only on the Public API tables in `CATALOG_READ_TABLES` (see "Catalog reads") |

The initial revision grants the runtime role its privileges, including default
privileges on tables the migrator creates later; revision 0002 revokes
`UPDATE` on the definition tables, which never change, and revision 0004 on
the result tables, whose rows are written once; revision 0005's
`budget_window_runs` takes the default privileges. Both roles must exist
before the first migration runs; an operator creates them. Before revision
0002 is deployed, the owner of the Public API's tables must also grant the
migrator `REFERENCES` on `policies`, `households` and `dynamics`, and
before revision 0003 on `tax_benefit_model_versions`, `datasets` and
`regions`; without it the migration fails and applies nothing. The migrator needs either
`CREATE` on the database or a `simulation` schema created in advance and owned
by it: the environment creates the schema only when it is missing, because
PostgreSQL checks `CREATE` on the database even for
`CREATE SCHEMA IF NOT EXISTS`.

## Deployment

On every deployment, the migration job (`migrate_simulation_database` in
`.github/workflows/simulation-deploy.reusable.yml`) runs
`.github/scripts/migrate-simulation-database.sh` before the Simulation
Entrypoint's candidate revision is deployed
(`sim.persistence.migrate-before-entrypoint`). From `main` it runs
`alembic upgrade head` and then `alembic current --check-heads`; from any other
branch it runs only the head check, so a deployment dispatched from an unmerged
branch fails instead of applying that branch's revisions. It is the only job
that receives `SIMULATION_MIGRATION_DATABASE_URL`. If it fails, the entrypoint
is not deployed. The Simulation Entrypoint's readiness check (`GET /ready`) reports
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
failed migration leaves nothing applied. A statement that waits more than ten
seconds for a lock fails the migration instead of waiting longer, because
adding a foreign key locks the Public API table it refers to and the Public
API's writes would queue behind it; the next deployment tries again. The previous release keeps serving
traffic while a deployment migrates, and a rollback moves traffic back to an
older image without downgrading the schema, so every revision must work with
the code of the release before it: add columns and tables, and remove them
only in a later release once nothing reads them.

Only one migration PR in this chain may merge at a time; another one open at
the same time rebases onto the new head after the first merges.

## Tests

```bash
uv sync --frozen --extra test
uv run --frozen pytest tests/
```

Tests marked `postgres` need a disposable PostgreSQL server. Point
`SIMULATION_TEST_DATABASE_URL` at its superuser, for example
`postgresql://postgres:password@127.0.0.1:5432/postgres`; each test creates and
drops its own database. Without the variable those tests skip. CI's
`Simulation database` job runs them against `postgres:17` with
`SIMULATION_REQUIRE_DATABASE_TESTS=1`, which turns a skip into a failure.

### Copies of the Public API's tables

The tests create copies of the Public API tables sim reads and refers to
(`tests/public_api_support.py`), with only the columns sim uses. Rule
`sim.persistence.public-api-copies-match` keeps them faithful: for every table
the copies create, each column they define has the real column's type (an enum
by name and values) and nullability, and each primary key and unique
constraint they define exists under the same name with the same columns
(`tests/public_api_schema.py`). A table or column added to the copies is
checked with no other change.

The real schema comes from the Public API itself. CI's `Public API schema`
job checks out `PolicyEngine/policyengine-api` (public, so no secret) at its
default branch, builds its tables with its own v2 migration chain
(`.github/scripts/public-api-schema-reference.sh`), and runs the tests
marked `public_api_schema` with `PUBLIC_API_SCHEMA_DATABASE_URL` naming that
database. The job follows the default branch rather than a pinned commit, so
a change there to a column sim reads fails sim's next CI run, instead of
waiting for someone to move a pin. The script prints the commit it migrated,
and a failed comparison names it.

To run it locally:

1. Check out the Public API at the commit to compare with (the one a failed
   CI run names, to reproduce it).
2. Create an empty `policyengine_v2_alembic_test` database on a local
   server; the Public API's disposable migration mode accepts no other.
3. Set `PUBLIC_API_SCHEMA_DATABASE_URL` to it, for example
   `postgresql://postgres:password@127.0.0.1:5432/policyengine_v2_alembic_test`,
   and run `.github/scripts/public-api-schema-reference.sh <checkout>`.
4. With `SIMULATION_TEST_DATABASE_URL` naming the same server's superuser,
   run `uv run --frozen pytest tests/ -m public_api_schema` here.
