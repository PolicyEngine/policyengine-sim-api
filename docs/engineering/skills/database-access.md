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
- `sim.persistence.one-migration-chain`: the repository's one Alembic chain,
  at `migrations/`, owns the Simulation Entrypoint's `simulation` schema;
  the Public API migrates the Stage 12 tables.
- `sim.stage12.persistence-typed-dml-only` and
  `sim.stage12.deployment-uses-api-schema`: the Stage 12 persistence
  library uses typed SQLAlchemy Core DML only, with no raw SQL text.

## Who owns the schema

- **The `simulation` schema** belongs to this repository's own Alembic chain
  at `migrations/` (`sim.persistence.one-migration-chain`). The tables are
  declared in `libs/policyengine-simulation-persistence`, whose README
  describes the chain, the database roles and how to add a revision.
- **The Stage 12 comparison tables** belong to the Public API's Alembic
  migrations. This repository's code reads and writes their rows, and
  deployment validation compares their live schema with its SQLAlchemy
  mappings; nothing here changes them.

Either way, only the chain that owns a table changes it.

## What the guard checks

`projects/policyengine-simulation-entry/tests/test_database_access_boundaries.py`
parses every Python file under `projects/*/src` (including the executor's
`src/modal`), `libs/*/src`, `.github/scripts` and the repository's
`migrations/` chain, and runs in the entry
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
assembled from separate literals. It doesn't read shell scripts or SQL files;
today no shell script calls a database client, and the repository has no SQL
files.

## Out of scope

The rule covers relational databases only. Non-relational stores are out of
scope: Modal Dicts, object storage such as the Google Cloud Storage artifact
and cache buckets, and in-process or on-disk caches.
