"""Add the economy result tables.

Every economy result is stored as rows, each section of today's result model
in its own table. A simulation's own figures (poverty rates, inequality, cliff
exposure) belong to its simulation run; the comparison of a baseline and a
reform (budget, programs, deciles, labour supply, geographies) belongs to the
report run. Each report run also records the optional sections its result
holds and its SPM provenance. Rows are written in the transaction that marks a
run succeeded; the default privileges let the runtime role read and insert
them. Nothing writes them yet.

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-22
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create the result tables and the report run's result columns."""
    op.create_table(
        "cliff_impacts",
        sa.Column("simulation_run_id", sa.Uuid(), nullable=False),
        sa.Column("cliff_gap", sa.Double(), nullable=False),
        sa.Column("cliff_share", sa.Double(), nullable=False),
        sa.ForeignKeyConstraint(
            ["simulation_run_id"],
            ["simulation.simulation_runs.id"],
            name="fk_cliff_impacts_simulation_run",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("simulation_run_id", name=op.f("pk_cliff_impacts")),
        schema="simulation",
    )
    op.create_table(
        "inequality",
        sa.Column("simulation_run_id", sa.Uuid(), nullable=False),
        sa.Column("gini", sa.Double(), nullable=False),
        sa.Column("top_10_pct_share", sa.Double(), nullable=False),
        sa.Column("top_1_pct_share", sa.Double(), nullable=False),
        sa.ForeignKeyConstraint(
            ["simulation_run_id"],
            ["simulation.simulation_runs.id"],
            name="fk_inequality_simulation_run",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("simulation_run_id", name=op.f("pk_inequality")),
        schema="simulation",
    )
    op.create_table(
        "poverty_rates",
        sa.Column("simulation_run_id", sa.Uuid(), nullable=False),
        sa.Column("measure", sa.Text(), nullable=False),
        sa.Column("group_dimension", sa.Text(), nullable=False),
        sa.Column("group_value", sa.Text(), nullable=False),
        sa.Column("rate", sa.Double(), nullable=False),
        sa.CheckConstraint(
            "(group_dimension = 'age' "
            "AND group_value IN ('child', 'adult', 'senior', 'all')) "
            "OR (group_dimension = 'gender' AND group_value IN ('male', 'female')) "
            "OR (group_dimension = 'race' "
            "AND group_value IN ('white', 'black', 'hispanic', 'other'))",
            name=op.f("ck_poverty_rates_group"),
        ),
        sa.CheckConstraint(
            "measure IN ('poverty', 'deep_poverty')",
            name=op.f("ck_poverty_rates_measure"),
        ),
        sa.ForeignKeyConstraint(
            ["simulation_run_id"],
            ["simulation.simulation_runs.id"],
            name="fk_poverty_rates_simulation_run",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint(
            "simulation_run_id",
            "measure",
            "group_dimension",
            "group_value",
            name=op.f("pk_poverty_rates"),
        ),
        schema="simulation",
    )
    op.create_table(
        "budget_impacts",
        sa.Column("report_run_id", sa.Uuid(), nullable=False),
        sa.Column("budgetary_impact", sa.Double(), nullable=False),
        sa.Column("tax_revenue_impact", sa.Double(), nullable=False),
        sa.Column("state_tax_revenue_impact", sa.Double(), nullable=False),
        sa.Column("benefit_spending_impact", sa.Double(), nullable=False),
        sa.Column("households", sa.Double(), nullable=False),
        sa.Column("baseline_net_income", sa.Double(), nullable=False),
        sa.ForeignKeyConstraint(
            ["report_run_id"],
            ["simulation.report_runs.id"],
            name="fk_budget_impacts_report_run",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("report_run_id", name=op.f("pk_budget_impacts")),
        schema="simulation",
    )
    op.create_table(
        "congressional_district_impacts",
        sa.Column("report_run_id", sa.Uuid(), nullable=False),
        sa.Column("district", sa.Text(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("average_household_income_change", sa.Double(), nullable=False),
        sa.Column("relative_household_income_change", sa.Double(), nullable=False),
        sa.Column("winner_percentage", sa.Double(), nullable=False),
        sa.Column("loser_percentage", sa.Double(), nullable=False),
        sa.Column("no_change_percentage", sa.Double(), nullable=False),
        sa.Column("population", sa.Double(), nullable=False),
        sa.CheckConstraint(
            "position >= 0", name=op.f("ck_congressional_district_impacts_position")
        ),
        sa.ForeignKeyConstraint(
            ["report_run_id"],
            ["simulation.report_runs.id"],
            name="fk_congressional_district_impacts_report_run",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint(
            "report_run_id", "district", name=op.f("pk_congressional_district_impacts")
        ),
        sa.UniqueConstraint(
            "report_run_id",
            "position",
            name=op.f("uq_congressional_district_impacts_report_run_id_position"),
        ),
        schema="simulation",
    )
    op.create_table(
        "constituency_impacts",
        sa.Column("report_run_id", sa.Uuid(), nullable=False),
        sa.Column("constituency_code", sa.Text(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("average_household_income_change", sa.Double(), nullable=False),
        sa.Column("relative_household_income_change", sa.Double(), nullable=False),
        sa.Column("population", sa.Double(), nullable=False),
        sa.CheckConstraint(
            "position >= 0", name=op.f("ck_constituency_impacts_position")
        ),
        sa.ForeignKeyConstraint(
            ["report_run_id"],
            ["simulation.report_runs.id"],
            name="fk_constituency_impacts_report_run",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint(
            "report_run_id", "constituency_code", name=op.f("pk_constituency_impacts")
        ),
        sa.UniqueConstraint(
            "report_run_id",
            "position",
            name=op.f("uq_constituency_impacts_report_run_id_position"),
        ),
        schema="simulation",
    )
    op.create_table(
        "decile_impacts",
        sa.Column("report_run_id", sa.Uuid(), nullable=False),
        sa.Column("measure", sa.Text(), nullable=False),
        sa.Column("decile", sa.Text(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("average_change", sa.Double(), nullable=False),
        sa.Column("relative_change", sa.Double(), nullable=False),
        sa.CheckConstraint(
            "measure IN ('income', 'wealth')", name=op.f("ck_decile_impacts_measure")
        ),
        sa.CheckConstraint("position >= 0", name=op.f("ck_decile_impacts_position")),
        sa.ForeignKeyConstraint(
            ["report_run_id"],
            ["simulation.report_runs.id"],
            name="fk_decile_impacts_report_run",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint(
            "report_run_id", "measure", "decile", name=op.f("pk_decile_impacts")
        ),
        sa.UniqueConstraint(
            "report_run_id",
            "measure",
            "position",
            name=op.f("uq_decile_impacts_report_run_id_measure_position"),
        ),
        schema="simulation",
    )
    op.create_table(
        "intra_decile_impacts",
        sa.Column("report_run_id", sa.Uuid(), nullable=False),
        sa.Column("measure", sa.Text(), nullable=False),
        sa.Column("outcome", sa.Text(), nullable=False),
        sa.Column("outcome_position", sa.Integer(), nullable=False),
        sa.Column("decile", sa.SmallInteger(), nullable=True),
        sa.Column("share", sa.Double(), nullable=False),
        sa.CheckConstraint(
            "measure IN ('income', 'wealth')",
            name=op.f("ck_intra_decile_impacts_measure"),
        ),
        sa.CheckConstraint(
            "decile IS NULL OR decile >= 1", name=op.f("ck_intra_decile_impacts_decile")
        ),
        sa.CheckConstraint(
            "outcome_position >= 0",
            name=op.f("ck_intra_decile_impacts_outcome_position"),
        ),
        sa.ForeignKeyConstraint(
            ["report_run_id"],
            ["simulation.report_runs.id"],
            name="fk_intra_decile_impacts_report_run",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "report_run_id",
            "measure",
            "outcome",
            "decile",
            name=op.f("uq_intra_decile_impacts_report_run_id_measure_outcome_decile"),
            postgresql_nulls_not_distinct=True,
        ),
        schema="simulation",
    )
    op.create_table(
        "labor_supply_decile_impacts",
        sa.Column("report_run_id", sa.Uuid(), nullable=False),
        sa.Column("decile", sa.Integer(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("average_income_effect", sa.Double(), nullable=False),
        sa.Column("average_substitution_effect", sa.Double(), nullable=False),
        sa.Column("relative_income_effect", sa.Double(), nullable=False),
        sa.Column("relative_substitution_effect", sa.Double(), nullable=False),
        sa.CheckConstraint(
            "position >= 0", name=op.f("ck_labor_supply_decile_impacts_position")
        ),
        sa.ForeignKeyConstraint(
            ["report_run_id"],
            ["simulation.report_runs.id"],
            name="fk_labor_supply_decile_impacts_report_run",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint(
            "report_run_id", "decile", name=op.f("pk_labor_supply_decile_impacts")
        ),
        sa.UniqueConstraint(
            "report_run_id",
            "position",
            name=op.f("uq_labor_supply_decile_impacts_report_run_id_position"),
        ),
        schema="simulation",
    )
    op.create_table(
        "labor_supply_responses",
        sa.Column("report_run_id", sa.Uuid(), nullable=False),
        sa.Column("substitution_lsr", sa.Double(), nullable=False),
        sa.Column("income_lsr", sa.Double(), nullable=False),
        sa.Column("relative_income_response", sa.Double(), nullable=False),
        sa.Column("relative_substitution_response", sa.Double(), nullable=False),
        sa.Column("total_change", sa.Double(), nullable=False),
        sa.Column("revenue_change", sa.Double(), nullable=False),
        sa.Column("baseline_hours", sa.Double(), nullable=False),
        sa.Column("reform_hours", sa.Double(), nullable=False),
        sa.Column("hours_change", sa.Double(), nullable=False),
        sa.Column("hours_income_effect", sa.Double(), nullable=False),
        sa.Column("hours_substitution_effect", sa.Double(), nullable=False),
        sa.ForeignKeyConstraint(
            ["report_run_id"],
            ["simulation.report_runs.id"],
            name="fk_labor_supply_responses_report_run",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint(
            "report_run_id", name=op.f("pk_labor_supply_responses")
        ),
        schema="simulation",
    )
    op.create_table(
        "local_authority_impacts",
        sa.Column("report_run_id", sa.Uuid(), nullable=False),
        sa.Column("local_authority_code", sa.Text(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("average_household_income_change", sa.Double(), nullable=False),
        sa.Column("relative_household_income_change", sa.Double(), nullable=False),
        sa.Column("population", sa.Double(), nullable=False),
        sa.CheckConstraint(
            "position >= 0", name=op.f("ck_local_authority_impacts_position")
        ),
        sa.ForeignKeyConstraint(
            ["report_run_id"],
            ["simulation.report_runs.id"],
            name="fk_local_authority_impacts_report_run",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint(
            "report_run_id",
            "local_authority_code",
            name=op.f("pk_local_authority_impacts"),
        ),
        sa.UniqueConstraint(
            "report_run_id",
            "position",
            name=op.f("uq_local_authority_impacts_report_run_id_position"),
        ),
        schema="simulation",
    )
    op.create_table(
        "program_budget_impacts",
        sa.Column("report_run_id", sa.Uuid(), nullable=False),
        sa.Column("program", sa.Text(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("baseline", sa.Double(), nullable=False),
        sa.Column("reform", sa.Double(), nullable=False),
        sa.Column("difference", sa.Double(), nullable=False),
        sa.CheckConstraint(
            "position >= 0", name=op.f("ck_program_budget_impacts_position")
        ),
        sa.ForeignKeyConstraint(
            ["report_run_id"],
            ["simulation.report_runs.id"],
            name="fk_program_budget_impacts_report_run",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint(
            "report_run_id", "program", name=op.f("pk_program_budget_impacts")
        ),
        sa.UniqueConstraint(
            "report_run_id",
            "position",
            name=op.f("uq_program_budget_impacts_report_run_id_position"),
        ),
        schema="simulation",
    )
    op.add_column(
        "report_runs",
        sa.Column("result_sections", postgresql.ARRAY(sa.Text()), nullable=True),
        schema="simulation",
    )
    op.add_column(
        "report_runs",
        sa.Column(
            "spm_provenance", postgresql.JSONB(astext_type=sa.Text()), nullable=True
        ),
        schema="simulation",
    )
    op.create_check_constraint(
        op.f("ck_report_runs_spm_provenance"),
        "report_runs",
        "spm_provenance IS NULL OR jsonb_typeof(spm_provenance) = 'object'",
        schema="simulation",
    )


def downgrade() -> None:
    """Drop what ``upgrade`` created."""
    op.drop_constraint(
        op.f("ck_report_runs_spm_provenance"),
        "report_runs",
        schema="simulation",
        type_="check",
    )
    op.drop_column("report_runs", "spm_provenance", schema="simulation")
    op.drop_column("report_runs", "result_sections", schema="simulation")
    op.drop_table("program_budget_impacts", schema="simulation")
    op.drop_table("local_authority_impacts", schema="simulation")
    op.drop_table("labor_supply_responses", schema="simulation")
    op.drop_table("labor_supply_decile_impacts", schema="simulation")
    op.drop_table("intra_decile_impacts", schema="simulation")
    op.drop_table("decile_impacts", schema="simulation")
    op.drop_table("constituency_impacts", schema="simulation")
    op.drop_table("congressional_district_impacts", schema="simulation")
    op.drop_table("budget_impacts", schema="simulation")
    op.drop_table("poverty_rates", schema="simulation")
    op.drop_table("inequality", schema="simulation")
    op.drop_table("cliff_impacts", schema="simulation")
