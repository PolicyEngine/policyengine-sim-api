"""Add the simulation and report definition tables.

``simulations`` records what each simulation runs, independent of model
versions: its kind, country, policy (null for current law), dynamic, and either
a household or a region code with an optional dataset name. Foreign keys into
the Public API's policies, households and dynamics in the same database are
``ON DELETE RESTRICT`` and need ``REFERENCES`` on those tables for the migrator
role. ``reports`` records which simulations each report compares, of the
report's own country and kind, with its year and options. Definitions never
change, so the runtime role may read and insert them but not update them.
Nothing writes the tables yet.

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-22
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

SCHEMA = "simulation"
PUBLIC_API_SCHEMA = "public"


def _public_api(table_and_column: str) -> str:
    return f"{PUBLIC_API_SCHEMA}.{table_and_column}"


def _create_simulations() -> None:
    op.create_table(
        "simulations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("country", sa.Text(), nullable=False),
        sa.Column("policy_id", sa.Uuid(), nullable=True),
        sa.Column("dynamic_id", sa.Uuid(), nullable=True),
        sa.Column("household_id", sa.Uuid(), nullable=True),
        sa.Column("region_code", sa.String(length=255), nullable=True),
        sa.Column("dataset_name", sa.String(length=255), nullable=True),
        sa.Column("legacy_simulation_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "kind IN ('economy', 'household')",
            name=op.f("ck_simulations_kind"),
        ),
        sa.CheckConstraint(
            "country ~ '^[a-z]{2}$'", name=op.f("ck_simulations_country")
        ),
        sa.CheckConstraint(
            "(kind = 'household' AND household_id IS NOT NULL "
            "AND region_code IS NULL AND dataset_name IS NULL) "
            "OR (kind = 'economy' AND household_id IS NULL "
            "AND region_code IS NOT NULL)",
            name=op.f("ck_simulations_population_matches_kind"),
        ),
        sa.CheckConstraint(
            "region_code IS NULL OR region_code ~ '^\\S+$'",
            name=op.f("ck_simulations_region_code"),
        ),
        sa.CheckConstraint(
            "dataset_name IS NULL OR dataset_name ~ '^\\S+$'",
            name=op.f("ck_simulations_dataset_name"),
        ),
        sa.CheckConstraint(
            "legacy_simulation_id IS NULL OR legacy_simulation_id > 0",
            name=op.f("ck_simulations_legacy_simulation_id"),
        ),
        sa.ForeignKeyConstraint(
            ["policy_id", "country"],
            [_public_api("policies.id"), _public_api("policies.country_id")],
            name="fk_simulations_policy_country",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["dynamic_id"],
            [_public_api("dynamics.id")],
            name="fk_simulations_dynamic",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["household_id", "country"],
            [_public_api("households.id"), _public_api("households.country_id")],
            name="fk_simulations_household_country",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_simulations"),
        sa.UniqueConstraint(
            "legacy_simulation_id", name="uq_simulations_legacy_simulation_id"
        ),
        sa.UniqueConstraint(
            "id", "country", "kind", name="uq_simulations_id_country_kind"
        ),
        schema=SCHEMA,
    )
    for column in ("policy_id", "household_id", "dynamic_id"):
        op.create_index(
            f"ix_simulations_{column}", "simulations", [column], schema=SCHEMA
        )


def _create_reports() -> None:
    simulation_key = [
        f"{SCHEMA}.simulations.id",
        f"{SCHEMA}.simulations.country",
        f"{SCHEMA}.simulations.kind",
    ]
    op.create_table(
        "reports",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("country", sa.Text(), nullable=False),
        sa.Column("baseline_simulation_id", sa.Uuid(), nullable=False),
        sa.Column("reform_simulation_id", sa.Uuid(), nullable=True),
        sa.Column("year", sa.Integer(), nullable=False),
        sa.Column(
            "options",
            postgresql.JSONB(),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("legacy_report_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "kind IN ('economy', 'household')", name=op.f("ck_reports_kind")
        ),
        sa.CheckConstraint("country ~ '^[a-z]{2}$'", name=op.f("ck_reports_country")),
        sa.CheckConstraint("year BETWEEN 1900 AND 2200", name=op.f("ck_reports_year")),
        sa.CheckConstraint(
            "jsonb_typeof(options) = 'object'", name=op.f("ck_reports_options")
        ),
        sa.CheckConstraint(
            "kind = 'household' OR reform_simulation_id IS NOT NULL",
            name=op.f("ck_reports_economy_has_reform"),
        ),
        sa.CheckConstraint(
            "legacy_report_id IS NULL OR legacy_report_id > 0",
            name=op.f("ck_reports_legacy_report_id"),
        ),
        sa.ForeignKeyConstraint(
            ["baseline_simulation_id", "country", "kind"],
            simulation_key,
            name="fk_reports_baseline_simulation",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["reform_simulation_id", "country", "kind"],
            simulation_key,
            name="fk_reports_reform_simulation",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_reports"),
        sa.UniqueConstraint("legacy_report_id", name="uq_reports_legacy_report_id"),
        schema=SCHEMA,
    )
    for column in ("baseline_simulation_id", "reform_simulation_id"):
        op.create_index(f"ix_reports_{column}", "reports", [column], schema=SCHEMA)


def upgrade() -> None:
    """Create both tables; the runtime role may not update either."""
    _create_simulations()
    _create_reports()
    op.execute(
        "REVOKE UPDATE ON simulation.simulations, simulation.reports "
        "FROM simulation_runtime"
    )


def downgrade() -> None:
    """Drop the tables ``upgrade`` created, with their indexes and privileges."""
    for column in ("reform_simulation_id", "baseline_simulation_id"):
        op.drop_index(f"ix_reports_{column}", table_name="reports", schema=SCHEMA)
    op.drop_table("reports", schema=SCHEMA)
    for column in ("dynamic_id", "household_id", "policy_id"):
        op.drop_index(
            f"ix_simulations_{column}", table_name="simulations", schema=SCHEMA
        )
    op.drop_table("simulations", schema=SCHEMA)
