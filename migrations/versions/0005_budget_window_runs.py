"""Add budget-window reports and their runs.

A report gains ``budget_window_size``: an economy report with one compares its
two simulations in each of that many years, from its year on (at most 75, and
none past the last year a run may have). ``budget_window_runs`` records each
calculation of such a report on a catalog version: its trigger, whether its
years calculate their simulations again, how many years it calculates at
once, its status and error, when the safety check next looks at it, and the
lease of the caller starting its next years. A report run gains
``budget_window_run_id``: the budget-window run it is a year of, of the same
report and catalog version (a composite foreign key), with at most one report
run per year. The default privileges let the runtime role read, insert and
update the new table, and not delete from it.

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-29
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None

SCHEMA = "simulation"
TRIGGERS = "'initial', 'manual', 'system'"
STATUSES = "'running', 'succeeded', 'failed'"
MAX_BUDGET_WINDOW_YEARS = 75
LAST_YEAR = 2200


def _add_budget_window_size() -> None:
    op.add_column(
        "reports",
        sa.Column("budget_window_size", sa.Integer(), nullable=True),
        schema=SCHEMA,
    )
    op.create_check_constraint(
        op.f("ck_reports_budget_window_size"),
        "reports",
        "budget_window_size IS NULL OR (kind = 'economy' "
        f"AND budget_window_size BETWEEN 1 AND {MAX_BUDGET_WINDOW_YEARS} "
        f"AND year + budget_window_size - 1 <= {LAST_YEAR})",
        schema=SCHEMA,
    )


def _create_budget_window_runs() -> None:
    op.create_table(
        "budget_window_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("report_id", sa.Uuid(), nullable=False),
        sa.Column("tax_benefit_model_version_id", sa.Uuid(), nullable=False),
        sa.Column("trigger", sa.Text(), nullable=False),
        sa.Column("force", sa.Boolean(), nullable=False),
        sa.Column("max_concurrent_years", sa.Integer(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("error_code", sa.Text(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("next_check_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "scheduling_claimed_until", sa.DateTime(timezone=True), nullable=True
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            f"trigger IN ({TRIGGERS})", name=op.f("ck_budget_window_runs_trigger")
        ),
        sa.CheckConstraint(
            f"status IN ({STATUSES})", name=op.f("ck_budget_window_runs_status")
        ),
        sa.CheckConstraint(
            "max_concurrent_years >= 1",
            name=op.f("ck_budget_window_runs_max_concurrent_years"),
        ),
        sa.CheckConstraint(
            "(status = 'running') = (next_check_at IS NOT NULL)",
            name=op.f("ck_budget_window_runs_next_check_at"),
        ),
        sa.CheckConstraint(
            "(status IN ('succeeded', 'failed')) = (completed_at IS NOT NULL)",
            name=op.f("ck_budget_window_runs_completed_at"),
        ),
        sa.CheckConstraint(
            "(status = 'failed') = (error_code IS NOT NULL)",
            name=op.f("ck_budget_window_runs_error"),
        ),
        sa.ForeignKeyConstraint(
            ["report_id"],
            [f"{SCHEMA}.reports.id"],
            name="fk_budget_window_runs_report",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tax_benefit_model_version_id"],
            ["public.tax_benefit_model_versions.id"],
            name="fk_budget_window_runs_catalog_version",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_budget_window_runs"),
        sa.UniqueConstraint(
            "id",
            "report_id",
            "tax_benefit_model_version_id",
            name="uq_budget_window_runs_id_report_catalog_version",
        ),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_budget_window_runs_report_id_created_at",
        "budget_window_runs",
        ["report_id", "created_at"],
        schema=SCHEMA,
    )
    op.create_index(
        "ix_budget_window_runs_due_checks",
        "budget_window_runs",
        ["next_check_at"],
        schema=SCHEMA,
        postgresql_where=sa.text("status = 'running'"),
    )
    op.create_index(
        "ix_budget_window_runs_tax_benefit_model_version_id",
        "budget_window_runs",
        ["tax_benefit_model_version_id"],
        schema=SCHEMA,
    )


def _add_budget_window_run_id() -> None:
    op.add_column(
        "report_runs",
        sa.Column("budget_window_run_id", sa.Uuid(), nullable=True),
        schema=SCHEMA,
    )
    op.create_foreign_key(
        "fk_report_runs_budget_window_run",
        "report_runs",
        "budget_window_runs",
        ["budget_window_run_id", "report_id", "tax_benefit_model_version_id"],
        ["id", "report_id", "tax_benefit_model_version_id"],
        source_schema=SCHEMA,
        referent_schema=SCHEMA,
        ondelete="RESTRICT",
    )
    op.create_unique_constraint(
        "uq_report_runs_budget_window_run_id_year",
        "report_runs",
        ["budget_window_run_id", "year"],
        schema=SCHEMA,
    )


def upgrade() -> None:
    """Add the window size, the budget-window runs and each year's link."""
    _add_budget_window_size()
    _create_budget_window_runs()
    _add_budget_window_run_id()


def downgrade() -> None:
    """Drop what ``upgrade`` added, with its indexes and keys."""
    op.drop_constraint(
        "uq_report_runs_budget_window_run_id_year",
        "report_runs",
        schema=SCHEMA,
        type_="unique",
    )
    op.drop_constraint(
        "fk_report_runs_budget_window_run",
        "report_runs",
        schema=SCHEMA,
        type_="foreignkey",
    )
    op.drop_column("report_runs", "budget_window_run_id", schema=SCHEMA)
    for index in (
        "ix_budget_window_runs_tax_benefit_model_version_id",
        "ix_budget_window_runs_due_checks",
        "ix_budget_window_runs_report_id_created_at",
    ):
        op.drop_index(index, table_name="budget_window_runs", schema=SCHEMA)
    op.drop_table("budget_window_runs", schema=SCHEMA)
    op.drop_constraint(
        op.f("ck_reports_budget_window_size"), "reports", schema=SCHEMA, type_="check"
    )
    op.drop_column("reports", "budget_window_size", schema=SCHEMA)
