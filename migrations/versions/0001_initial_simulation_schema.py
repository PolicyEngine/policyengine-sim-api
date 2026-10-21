"""Grant the Simulation Entrypoint runtime role access to the simulation schema.

The environment creates the ``simulation`` schema, owned by the migrator, and
Alembic creates ``simulation.alembic_version`` before this revision runs. This
revision lets the runtime role use the schema, read the applied revision for
the readiness check, and read and write every table the migrator creates in
later revisions. The runtime role never owns or changes schema objects.

Revision ID: 0001
Revises:
Create Date: 2026-10-22
"""

from __future__ import annotations

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

# The statements name the schema and role literally: a migration is a fixed
# historical record, so later renames must not change what it did.
UPGRADE_STATEMENTS = (
    "GRANT USAGE ON SCHEMA simulation TO simulation_runtime",
    "GRANT SELECT ON simulation.alembic_version TO simulation_runtime",
    "ALTER DEFAULT PRIVILEGES IN SCHEMA simulation "
    "GRANT SELECT, INSERT, UPDATE ON TABLES TO simulation_runtime",
    "ALTER DEFAULT PRIVILEGES IN SCHEMA simulation "
    "GRANT USAGE, SELECT ON SEQUENCES TO simulation_runtime",
)
DOWNGRADE_STATEMENTS = (
    "ALTER DEFAULT PRIVILEGES IN SCHEMA simulation "
    "REVOKE USAGE, SELECT ON SEQUENCES FROM simulation_runtime",
    "ALTER DEFAULT PRIVILEGES IN SCHEMA simulation "
    "REVOKE SELECT, INSERT, UPDATE ON TABLES FROM simulation_runtime",
    "REVOKE SELECT ON simulation.alembic_version FROM simulation_runtime",
    "REVOKE USAGE ON SCHEMA simulation FROM simulation_runtime",
)


def upgrade() -> None:
    """Grant schema usage, revision reads and default table privileges."""
    for statement in UPGRADE_STATEMENTS:
        op.execute(statement)


def downgrade() -> None:
    """Revoke everything ``upgrade`` granted."""
    for statement in DOWNGRADE_STATEMENTS:
        op.execute(statement)
