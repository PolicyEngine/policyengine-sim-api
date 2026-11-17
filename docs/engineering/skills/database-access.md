# Database Access Skill

Read this before adding code that connects to a relational database, declares
tables, or changes a database schema.

## Rules

`sim.boundaries.sqlalchemy-and-alembic-only` has two parts:

- **Connections go through SQLAlchemy.** Code connects to databases only
  through SQLAlchemy engines and connections, never by importing a database
  driver directly. Select the driver in the URL's driver name instead, as the
  Stage 12 store does with `postgresql+psycopg`.
- **Runtime code never changes the schema.** Schema changes happen only
  through Alembic migrations, never through `create_all`, `drop_all`,
  `Table.create`, `Table.drop`, SQLAlchemy DDL statements or DDL text in
  runtime code.

It builds on the Stage 12 rules:

- `sim.stage12.no-schema-ddl`: the simulation runtimes and the Stage 12
  persistence library contain no schema DDL. Its `DDL_PATTERN` regex is the
  one this rule matches against every source directory's string literals.
- `sim.stage12.no-migration-chain`: this repository has no migration chain
  of its own; policyengine-api migrates the Stage 12 tables.
- `sim.stage12.persistence-typed-dml-only` and
  `sim.stage12.deployment-uses-api-schema`: the Stage 12 persistence
  library uses typed SQLAlchemy statements only, with no raw SQL text.

## ORM

`sim.persistence.orm-only` requires the SQLAlchemy ORM, not Core: code that
reads or writes database rows declares tables only as mapped classes and runs
every statement through an ORM `Session` against them. The rule is ratcheted.
`rules/baselines/orm-only.txt` lists the findings in the Core code the
libraries had when it was added, and issue #1097 tracks converting them.

What the ORM means here:

- **Models** are SQLAlchemy 2.0 typed declarative classes (`Mapped[...]`,
  `mapped_column`) deriving from one `DeclarativeBase` per library. A
  library's `MetaData`, with its schema, is built only in its `orm_base.py`.
  Constraints and indexes go in `__table_args__` under the Public API's names.
  Use SQLAlchemy's own ORM, not SQLModel.
- **Sessions:** only a library's `engine.py` creates or holds an `Engine`, and
  it hands out a `sessionmaker`. Everything else receives a `sessionmaker` or a
  `Session`, and runs a transaction as `with session_factory.begin() as
  session:`.
- **Statements** name mapped classes or their attributes: `session.get`,
  `session.add`, `session.scalars(select(Model))`, and ORM-enabled
  `update(Model)`, `insert(Model)` or
  `postgresql.insert(Model).on_conflict_do_nothing()` run with
  `session.execute`. The single-statement forms keep conditional updates, such
  as claiming a pending job only while it has no Modal call ID, and
  find-or-create inserts atomic.
- **SQL text** appears only as a model's server default or a partial index's
  condition.

`projects/policyengine-simulation-entry/tests/test_orm_only.py` runs the checks
in `orm_access_support.py` over the same files as the guard below. Each
finding is one of:

- **Core table declaration:** a `Table(...)`, `Column(...)` or `MetaData(...)`
  call, apart from `MetaData` in an `orm_base.py`.
- **Core column collection:** a `.c` or `__table__` attribute, or `.columns`
  of a table.
- **Core engine or connection:** an import of `Engine`, `Connection` or
  `create_engine` outside a library's `engine.py`, or any `.connect()`,
  `.connection()` or `.raw_connection()` call.
- **Statement not on a mapped class:** `select`, `insert`, `update` or
  `delete` whose first argument isn't a mapped class, one of its attributes, an
  `aliased(...)` of one, or a SQLAlchemy expression such as `func.count()`.
- **Raw SQL:** `text(...)` or `literal_column(...)` anywhere else, or
  `exec_driver_sql` or `.cursor()`.
- **sqlmodel import.**

`test_every_table_is_mapped_by_a_class` also imports both persistence libraries
and reports every table they hold that no mapped class maps.

The test fails on a finding the baseline doesn't list, and on a baseline entry
that no longer matches a finding, so the list only shrinks. Each entry is
`file: check: source`, without a line number, repeated once for each identical
finding in its file. A change that converts code removes its entries in the
same pull request; one that adds a finding fixes it rather than adding an
entry.

## Who owns the schema

policyengine-api's Alembic migrations own every table this repository uses,
and this repository has no migration chain of its own
(`sim.stage12.no-migration-chain`):

- the Stage 12 comparison tables, whose live schema deployment validation
  compares with the Stage 12 library's SQLAlchemy mappings.
- the tables the simulation service writes (`simulations`, `reports`,
  `simulation_runs`, `report_runs`, `budget_window_runs` and
  `household_jobs`), modelled in `libs/policyengine-simulation-persistence`.
  The service connects as its own role, which policyengine-api's privilege
  manifest grants.
- the policyengine-api tables a calculation is read from (`households`,
  `policies`, `parameters`, `parameter_values`, `tax_benefit_models` and
  `tax_benefit_model_versions`), which the simulation service only reads.
  Their read-only models in that library's `input_tables.py` name only the
  columns the service reads, apart from the models of the tables it writes.
- the run tables the v2 Modal coordinator, workers, household functions and
  stuck-run sweep write as the Modal worker role: `report_runs`,
  `simulation_runs`, `budget_window_runs`, the result tables and
  `household_jobs`. They use the same models, in
  `libs/policyengine-simulation-persistence` (`tables.py` and
  `result_tables.py`, which `sim.persistence.models-match-public-api`
  compares with the Public API's tables), through the stores in
  `libs/policyengine-stage12-persistence`. The v1 `run_simulation` code
  imports neither library.

This repository's code reads and writes their rows; nothing here changes
them. A change to one of those tables starts in policyengine-api, and the
models here follow it.

## What the guard checks

`projects/policyengine-simulation-entry/tests/test_database_access_boundaries.py`
parses every Python file under `projects/*/src` (including the executor's
`src/modal`), `libs/*/src` and `.github/scripts`, and runs in the entry
project's `Test` job. The checks live in `database_access_support.py` next to
it. It fails on:

- an import of a database driver: `psycopg`, `psycopg2`, `pymysql`,
  `MySQLdb`, `mysql.connector`, `sqlite3`, `asyncpg`, `pg8000`, `aiosqlite`
  or `aiomysql`, or any of their submodules;
- an import of `alembic` outside a `migrations` directory;
- a `create_all` or `drop_all` call;
- a `create()` or `drop()` call on a table: a `Table(...)` call, a name or
  attribute bound to one or annotated as `Table` in any scanned file, a
  `__table__` attribute or an item of `.tables`, or any `create()` or `drop()`
  call passing `checkfirst=`. Other `drop()` calls, such as pandas'
  `frame.drop(columns=...)`, are not schema changes;
- SQLAlchemy's DDL statements (`CreateTable`, `DropTable`, `CreateSchema`,
  `DDL` and the rest);
- DDL text in a string literal: `CREATE`, `ALTER`, `DROP` or `TRUNCATE` of a
  table, type, schema or database. Docstrings and comments don't count.

Files under a `migrations` directory may import alembic and change the schema,
but still connect through SQLAlchemy.

The guard reads source, not types. It misses imports through `importlib` or
`__import__`, a table reached through some other local variable, and SQL text
assembled from separate literals. It doesn't read shell scripts or SQL files.
The one shell script that reaches a database,
`.github/scripts/public-api-schema-reference.sh`, runs the Public API's own
migration and privilege commands against a disposable CI database for
`sim.persistence.models-match-public-api`; the repository has no SQL files.

## Out of scope

The rule covers relational databases only. Non-relational stores are out of
scope: Modal Dicts, object storage such as the Google Cloud Storage artifact
and cache buckets, and in-process or on-disk caches.
