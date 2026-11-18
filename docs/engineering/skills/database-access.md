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
every statement through an ORM `Session` against them. The rule is enforced
with no baseline, so any finding fails.

What the ORM means here:

- **Models** are SQLAlchemy 2.0 typed declarative classes (`Mapped[...]`,
  `mapped_column`). Each derives from a `DeclarativeBase` in its library's
  `orm_base.py`, the only module that builds a `MetaData`, with its schema. A
  library has one base per `MetaData` it needs. A `MetaData` holds one table of
  each name, so `policyengine-simulation-persistence` has `ServiceBase`,
  `ReferencedBase`, `InputBase` and `ResultBase`, because its read-only models
  and its key targets describe some of the same tables, and `CatalogBase` for
  the PostgreSQL catalog tables the privilege check reads. A key to another
  base's table names that class's attribute, such as `ReportRun.id`.
  `policyengine-stage12-persistence` has `ComparisonBase`. Constraints and
  indexes go in `__table_args__` under API v1's names. Use SQLAlchemy's own
  ORM, not SQLModel.
- **Sessions:** only a library's `engine.py` creates or holds an `Engine`, and
  no public function there returns one or a `Connection`. It hands out a
  `sessionmaker` with `expire_on_commit=False`:
  `create_simulation_session_factory` for the simulation service's connection
  pool and `create_unpooled_session_factory` for the v2 Modal calls, both from
  a database URL. `dispose_session_factory` closes a factory's connections.
  Everything else receives a `sessionmaker` or a `Session`, and runs a
  transaction as `with session_factory.begin() as session:`, or as `with
  bounded_session(session_factory, statement_timeout_milliseconds=...) as
  session:` to limit its statements.
  The classes declare no relationships, so a flush doesn't order inserts by
  foreign key: flush the referenced row first. An attribute set to a SQL
  expression, such as `func.now()`, must be refreshed before the session closes
  if it will be read afterwards. To retry a refused connection without any risk
  of repeating a write, retry only a first statement that changes nothing, which
  makes the session check out its connection, as `StoreSessions` in
  `policyengine_stage12_persistence.engine` does.
- **Statements** name mapped classes or their attributes: `session.get`,
  `session.add`, `session.scalars(select(Model))`, and ORM-enabled
  `update(Model)`, `insert(Model)` or
  `postgresql.insert(Model).on_conflict_do_nothing()` run with
  `session.execute`. The single-statement forms keep conditional updates, such
  as claiming a pending job only while it has no Modal call ID, and
  find-or-create inserts atomic.
- **SQL text** appears only as a model's server default, a partial index's
  condition or a check constraint's condition.
- **Names:** an attribute or field that holds a mapped class is named after
  what it holds, such as `model`, never `table`: every `.table` read in a
  module that imports sqlalchemy is reported, because that is how a column
  reaches its Core table.
- **PostgreSQL's catalog** is read through mapped classes too: `PgClass`,
  `PgNamespace` and `PgAttribute` in `catalog_tables.py`.
- **Schema inspection**, for the deployment check that compares the live
  schema with the models, reads the live schema through
  `inspect_live_schema(session_factory)` in the Stage 12 library's `engine.py`,
  which needs no Connection. An inspector on an engine takes a connection for
  each read, so the check's factory has a one-connection pool
  (`create_single_connection_session_factory`). It reads the models' columns
  and keys from their mappers (`inspect(Model).columns`, `.primary_key`) and
  `__table_args__`, none of which is reported.

`projects/policyengine-simulation-entry/tests/test_orm_only.py` runs the checks
in `orm_access_support.py` over the same files as the guard below. They look
for Core rather than for the ORM, so a statement on mapped classes passes,
including one on expressions, labels, subqueries and aliases.

Each file is checked on its own, through its own imports. A Core table is
reported where it starts, not at each later use, because the first finding
already fails the rule: where it is declared, where it is read from a mapped
class, mapper or `MetaData` or as the bare name `__table__`, and where an
annotation names `Table` or `TableClause`.

Each finding is one of:

- **Core table declaration:** a `Table(...)`, `Column(...)`, `table(...)`,
  `column(...)`, `TableClause(...)`, `ColumnClause(...)` or `MetaData(...)`
  call, apart from `MetaData` in an `orm_base.py`.
- **Core table access:** a parameter, variable, attribute or return annotated
  `Table` or `TableClause`, unquoted or wholly quoted, apart from a class's bare
  `__table__` annotation in an `orm_base.py`;
  `__table__`, `local_table`, `persist_selectable` or `sorted_tables` anywhere,
  and a read of the bare name `__table__`;
  `tables` or
  `selectable` of a mapper (`__mapper__`, `.mapper`, or `inspect(...)`,
  `class_mapper(...)` or `object_mapper(...)`, called or held in a name, or a
  name assigned `__mapper__` or `.mapper`) or of a MetaData (a name bound to
  `MetaData(...)` or ending in `metadata`); and any `.table` read, which is
  how a column or mapped attribute reaches its table, as in
  `HouseholdJob.id.table`. A called `.table`, such as `sa.table(...)`, isn't
  a read.
- **Core engine or connection:** `Connection` or `AsyncConnection` from
  SQLAlchemy in any module; `Engine`, `create_engine` and the like outside a
  library's `engine.py`, imported or referenced as `sa.Engine`; any
  `.connect()`, `.connection()`, `.raw_connection()` or `.get_bind()` call;
  `.begin()` on an engine (a name annotated as `Engine`, bound to
  `create_engine(...)`, or named `engine` or ending in `_engine`); `.execute`
  on anything not named `session` or ending in `_session`; `.scalar`,
  `.scalars`, `.stream` or `.stream_scalars` on a name that is `connection` or
  `conn` or ends in `_connection` or `_conn` (a query result has `.scalar` and
  `.scalars` too); a public function of a library's `engine.py` whose return annotation
  names an `Engine` or `Connection`; and `factory.kw["bind"]`, the engine
  behind a `sessionmaker`, outside a library's `engine.py`. A library's
  `engine.py` opens sessions, not connections.
- **Raw SQL:** `text(...)`, `TextClause(...)` or `literal_column(...)`
  anywhere but a server default, an index's `postgresql_where` or a
  `CheckConstraint`'s condition, or `exec_driver_sql` or `.cursor()`.
- **sqlmodel import.**

Those that go by a method or attribute name alone apply only in modules that
import sqlalchemy: any `.table` read, `kw["bind"]`, the statement methods above,
and the `.connect()`, `.connection()`, `.raw_connection()`,
`.get_bind()`, `.begin()`, `.cursor()` and `.exec_driver_sql()` calls.

The checks read source, not types, and guard against ordinary Core use, not
against deliberate evasion. They miss imports through `importlib`; engines and
connections passed around without an `Engine` or `Connection` annotation, so a
connection held under a name such as `session` isn't reported; a statement method
passed without being called, such as `run(connection.execute, statement)`; a
function that returns an engine with no return annotation; the engine an
`Inspector` holds in `bind` or `engine`; a quoted name inside an annotation, such
as `Optional["Table"]`; `Table` under another local name, such as `Jobs = Table`;
names from `from sqlalchemy import *`; a `.table` read in a module that doesn't
import sqlalchemy; and a Session's `bind` attribute. `.tables` of a `MetaData`
isn't reported when the name holding it doesn't end in `metadata` and wasn't
assigned `MetaData(...)`.

`test_every_table_is_mapped_by_a_class` also imports both persistence libraries
and reports every table they hold that no mapped class maps, and
`test_no_project_installs_sqlmodel` fails if the `uv.lock` of any project with
a `pyproject.toml` holds SQLModel.

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
  columns the service reads and each table's primary key, apart from the
  models of the tables it writes.
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
