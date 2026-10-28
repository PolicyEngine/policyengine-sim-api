"""Add what household calculations need.

A simulation gains ``options``: whatever else changes what it calculates,
such as a household's axes, so a run is reused only for the same options.
Existing simulations get empty options. A computed household gains its
warnings (an empty list by default) and three documents, each null when it
does not apply: the SPM selection the calculation applied, its SPM receipt,
and the PolicyEngine.py bundle it ran with. A failed simulation run, or
comparison step, may carry a detail document beyond its error code and
message, such as the inputs a household calculation rejected.

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-27
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

HOUSEHOLD_OUTPUT_DOCUMENTS = ("spm_config", "spm_provenance", "bundle")
# Each run table's status column and the prefix of its error columns.
ERROR_DETAIL_COLUMNS = (
    ("simulation_runs", "status", ""),
    ("report_runs", "comparison_status", "comparison_"),
)

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add the simulation options, the household notes and the error details."""
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
    op.add_column(
        "household_outputs",
        sa.Column(
            "warnings",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        schema="simulation",
    )
    op.create_check_constraint(
        op.f("ck_household_outputs_warnings"),
        "household_outputs",
        "jsonb_typeof(warnings) = 'array'",
        schema="simulation",
    )
    for document in HOUSEHOLD_OUTPUT_DOCUMENTS:
        op.add_column(
            "household_outputs",
            sa.Column(document, postgresql.JSONB(astext_type=sa.Text()), nullable=True),
            schema="simulation",
        )
        op.create_check_constraint(
            op.f(f"ck_household_outputs_{document}"),
            "household_outputs",
            f"{document} IS NULL OR jsonb_typeof({document}) = 'object'",
            schema="simulation",
        )
    for table, status, prefix in ERROR_DETAIL_COLUMNS:
        op.add_column(
            table,
            sa.Column(
                f"{prefix}error_detail",
                postgresql.JSONB(astext_type=sa.Text()),
                nullable=True,
            ),
            schema="simulation",
        )
        op.create_check_constraint(
            op.f(f"ck_{table}_{prefix}error_detail"),
            table,
            f"{prefix}error_detail IS NULL OR ({status} = 'failed' "
            f"AND jsonb_typeof({prefix}error_detail) = 'object')",
            schema="simulation",
        )


def downgrade() -> None:
    """Drop what ``upgrade`` added."""
    for table, _status, prefix in ERROR_DETAIL_COLUMNS:
        op.drop_constraint(
            op.f(f"ck_{table}_{prefix}error_detail"),
            table,
            schema="simulation",
            type_="check",
        )
        op.drop_column(table, f"{prefix}error_detail", schema="simulation")
    for document in (*HOUSEHOLD_OUTPUT_DOCUMENTS, "warnings"):
        op.drop_constraint(
            op.f(f"ck_household_outputs_{document}"),
            "household_outputs",
            schema="simulation",
            type_="check",
        )
        op.drop_column("household_outputs", document, schema="simulation")
    op.drop_constraint(
        op.f("ck_simulations_options"),
        "simulations",
        schema="simulation",
        type_="check",
    )
    op.drop_column("simulations", "options", schema="simulation")
