# PolicyEngine simulation persistence

SQLAlchemy Core tables and database connections for the Simulation
Entrypoint's own Postgres schema, `simulation`. The Simulation Entrypoint owns
every table in that schema; no other service creates, changes or writes them.

The temporary Stage 12 comparison tables are not part of this schema: they are
owned and migrated by the Public API, and `libs/policyengine-stage12-persistence`
writes them with DML only.

## Connections

`create_simulation_engine` builds the runtime engine from the canonical
`postgresql://` URL the deployment stores, selecting the psycopg 3 driver. It
keeps a small pool, checks connections before use, and never prepares
server-side statements, so the same URL works through Supabase's session and
transaction poolers. A malformed URL raises `ValueError` without repeating the
URL, which holds a password.

`read_schema_revision` returns the migration revision the database has applied,
or `None` before the first migration.

## Tests

```bash
uv sync --extra test
uv run pytest tests/
```
