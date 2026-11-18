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
  `mapped_column`). Each derives from a `DeclarativeBase` in its library's
  `orm_base.py`, the only module that builds a `MetaData`, with its schema. A
  library has one base per `MetaData` it needs. A `MetaData` holds one table of
  each name, so `policyengine-simulation-persistence` has four: `ServiceBase`,
  `ReferencedBase`, `InputBase` and `ResultBase`, because its read-only models
  and its key targets describe some of the same tables. A key to another
  base's table names that class's attribute, such as `ReportRun.id`.
  `policyengine-stage12-persistence` has `ComparisonBase`. Constraints and
  indexes go in `__table_args__` under API v1's names. Use SQLAlchemy's own
  ORM, not SQLModel.
- **Sessions:** only a library's `engine.py` creates or holds an `Engine`, and
  it hands out a `sessionmaker` (`create_session_factory`, with
  `expire_on_commit=False`). Everything else receives a `sessionmaker` or a
  `Session`, and runs a transaction as `with session_factory.begin() as
  session:`, or as `with bounded_session(session_factory,
  statement_timeout_milliseconds=...) as session:` to limit its statements.
  The classes declare no relationships, so a flush doesn't order inserts by
  foreign key: flush the referenced row first.
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

- **While code is converted**, a table module keeps each table's old name as
  `household_jobs = HouseholdJob.__table__`, so unconverted statements keep
  working. That name is a Core table, so each of its uses stays a finding.

- **Schema inspection**, for the deployment check that compares the live
  schema with the models, reads the live schema as `inspect(engine)` in the
  library's `engine.py`, which needs no Connection, and the models' columns
  and keys from their mappers (`inspect(Model).columns`, `.primary_key`) and
  `__table_args__`, none of which is reported.

`projects/policyengine-simulation-entry/tests/test_orm_only.py` runs the checks
in `orm_access_support.py` over the same files as the guard below. They look
for Core rather than for the ORM, so a statement on mapped classes passes,
including one on expressions, labels, subqueries and aliases.

Names resolve within each module, through its own imports. Each file's module
name is its path below `src`, and two files with one module name are refused.
A Core table is, in one module:

- a name it binds to `Table(...)`, `table(...)` or `TableClause(...)`;
- a name it imports, under any name or through `*`, from a scanned module that
  binds or imports one at module level, and an attribute such as
  `tables.household_jobs` or `example.tables.household_jobs` of such a module;
- a parameter, variable or attribute annotated as SQLAlchemy's `Table`;
- a name or attribute it assigns from an expression rooted at a Core table,
  such as `previous = household_jobs.alias()` or
  `self.jobs = tables.household_jobs`, or from one that reads `__table__`,
  `local_table`, `persist_selectable` or `sorted_tables`, such as
  `household_jobs = HouseholdJob.__table__`, with a tuple assignment pairing
  each name with its value, nested tuples included;
- a class attribute it declares as a table, such as a dataclass field
  `table: Table`, wherever the module reads it. Another module's read of a
  class or instance attribute is reported when the reading module imports
  sqlalchemy and the attribute is named `table`, or when the name matches one
  of the reading module's own table attributes. Otherwise it isn't, and the
  reference that stored the table there is reported where it was made.

Each finding is one of:

- **Core table declaration:** a `Table(...)`, `Column(...)`, `table(...)`,
  `column(...)`, `TableClause(...)`, `ColumnClause(...)` or `MetaData(...)`
  call, apart from `MetaData` in an `orm_base.py`.
- **Core table access:** any reference to a Core table, which covers its
  statements, methods, column collections and joins; `__table__`,
  `local_table`, `persist_selectable` or `sorted_tables` anywhere; `tables` or
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
  and `.begin()` on an engine (a name annotated as `Engine`, bound to
  `create_engine(...)`, or named `engine` or ending in `_engine`). A
  library's `engine.py` opens sessions, not connections.
- **Raw SQL:** `text(...)`, `TextClause(...)` or `literal_column(...)`
  anywhere but a server default, an index's `postgresql_where` or a
  `CheckConstraint`'s condition, or `exec_driver_sql` or `.cursor()`.
- **sqlmodel import.**

Eight of these go by a method or attribute name alone, so they apply only in
modules that import sqlalchemy: any `.table` read, and the `.connect()`,
`.connection()`, `.raw_connection()`, `.get_bind()`, `.begin()`, `.cursor()`
and `.exec_driver_sql()` calls.

The checks read source, not types, and guard against ordinary Core use, not
against deliberate evasion. They miss imports through `importlib`, tables and
engines passed around without a `Table` or `Engine` annotation, tables held in
a container
such as a dict, a class or instance attribute read in another module (other
than the reads above), a `.table` read in a module that doesn't import
sqlalchemy, and a Session's `bind` attribute. A name derived from a Core table
counts as one throughout its module. Once a library's tables are mapped
classes and its declarations are gone, the ordinary routes to a Core table,
such as `__table__` and `.table`, are reported; the routes listed here are the
exceptions.

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
