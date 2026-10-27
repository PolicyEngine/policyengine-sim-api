"""Add what household calculations need.

A simulation gains ``options``: whatever else changes what it calculates,
such as a household's axes, so a run is reused only for the same options.
Existing simulations get empty options.

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-27
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add the simulation options."""
    op.add_column(
        "simulations",
        sa.Column(
            "options",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        schema="simulation",
    )
    op.create_check_constraint(
        op.f("ck_simulations_options"),
        "simulations",
        "jsonb_typeof(options) = 'object'",
        schema="simulation",
    )


def downgrade() -> None:
    """Drop what ``upgrade`` added."""
    op.drop_constraint(
        op.f("ck_simulations_options"),
        "simulations",
        schema="simulation",
        type_="check",
    )
    op.drop_column("simulations", "options", schema="simulation")
