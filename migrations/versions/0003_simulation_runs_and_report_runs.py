"""Add simulation runs and report runs.

``simulation_runs`` records each execution of a simulation for a catalog
version and year: the region and dataset rows it resolved in that version, its
status, Modal call, timings, error, trigger and attempt number, unique per
simulation, catalog version, year and attempt. Composite foreign keys through
the Public API's ``uq_regions_model_version_code`` and
``uq_datasets_id_model_version`` keep a run on its own version's rows, and need
``REFERENCES`` on ``tax_benefit_model_versions``, ``regions`` and ``datasets``
for the migrator role. ``report_runs`` records each calculation of a report:
two simulation runs of one catalog version and year and, for an economy
report, the comparison step's status and Modal call. The runtime role may read,
insert and update both tables. Nothing writes them yet.

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-22
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

SCHEMA = "simulation"
STATUSES = "'pending', 'dispatching', 'running', 'succeeded', 'failed'"
TRIGGERS = "'initial', 'manual', 'system'"


def _public_api(table_and_column: str) -> str:
    return f"public.{table_and_column}"


def _execution_columns(prefix: str, *, required: bool) -> list[sa.Column]:
    return [
        sa.Column(f"{prefix}status", sa.Text(), nullable=not required),
        sa.Column(f"{prefix}modal_call_id", sa.Text(), nullable=True),
        sa.Column(f"{prefix}error_code", sa.Text(), nullable=True),
        sa.Column(f"{prefix}error_message", sa.Text(), nullable=True),
        sa.Column(
            f"{prefix}check_count",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(f"{prefix}dispatched_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(f"{prefix}next_check_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(f"{prefix}completed_at", sa.DateTime(timezone=True), nullable=True),
    ]


def _status_checks(
    table: str, prefix: str, *, optional: bool
) -> list[sa.CheckConstraint]:
    status = f"{prefix}status"
    unless_absent = f"{status} IS NULL OR " if optional else ""
    return [
        sa.CheckConstraint(
            f"{unless_absent}{status} IN ({STATUSES})",
            name=op.f(f"ck_{table}_{prefix}status"),
        ),
        sa.CheckConstraint(
            f"{unless_absent}"
            f"({status} IN ('pending', 'dispatching') "
            f"AND {prefix}modal_call_id IS NULL) "
            f"OR ({status} IN ('running', 'succeeded') "
            f"AND {prefix}modal_call_id IS NOT NULL) "
            f"OR {status} = 'failed'",
            name=op.f(f"ck_{table}_{prefix}modal_call"),
        ),
        sa.CheckConstraint(
            f"{unless_absent}"
            f"({status} IN ('succeeded', 'failed')) "
            f"= ({prefix}completed_at IS NOT NULL)",
            name=op.f(f"ck_{table}_{prefix}completed_at"),
        ),
        sa.CheckConstraint(
            f"{unless_absent}({status} = 'failed') = ({prefix}error_code IS NOT NULL)",
            name=op.f(f"ck_{table}_{prefix}error"),
        ),
        sa.CheckConstraint(
            f"{unless_absent}"
            f"({status} = 'running') = ({prefix}next_check_at IS NOT NULL)",
            name=op.f(f"ck_{table}_{prefix}next_check_at"),
        ),
        sa.CheckConstraint(
            f"{prefix}check_count >= 0", name=op.f(f"ck_{table}_{prefix}check_count")
        ),
    ]


def _create_simulation_runs() -> None:
    op.create_unique_constraint(
        "uq_simulations_id_kind", "simulations", ["id", "kind"], schema=SCHEMA
    )
    op.create_table(
        "simulation_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("simulation_id", sa.Uuid(), nullable=False),
        sa.Column("simulation_kind", sa.Text(), nullable=False),
        sa.Column("tax_benefit_model_version_id", sa.Uuid(), nullable=False),
        sa.Column("year", sa.Integer(), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("region_code", sa.String(length=255), nullable=True),
        sa.Column("dataset_id", sa.Uuid(), nullable=True),
        sa.Column("trigger", sa.Text(), nullable=False),
        *_execution_columns("", required=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "year BETWEEN 1900 AND 2200", name=op.f("ck_simulation_runs_year")
        ),
        sa.CheckConstraint("attempt >= 1", name=op.f("ck_simulation_runs_attempt")),
        sa.CheckConstraint(
            f"trigger IN ({TRIGGERS})", name=op.f("ck_simulation_runs_trigger")
        ),
        sa.CheckConstraint(
            "(simulation_kind = 'household' AND region_code IS NULL "
            "AND dataset_id IS NULL) "
            "OR (simulation_kind = 'economy' AND region_code IS NOT NULL "
            "AND dataset_id IS NOT NULL)",
            name=op.f("ck_simulation_runs_population_matches_kind"),
        ),
        *_status_checks("simulation_runs", "", optional=False),
        sa.ForeignKeyConstraint(
            ["simulation_id", "simulation_kind"],
            [f"{SCHEMA}.simulations.id", f"{SCHEMA}.simulations.kind"],
            name="fk_simulation_runs_simulation",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tax_benefit_model_version_id"],
            [_public_api("tax_benefit_model_versions.id")],
            name="fk_simulation_runs_catalog_version",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tax_benefit_model_version_id", "region_code"],
            [
                _public_api("regions.tax_benefit_model_version_id"),
                _public_api("regions.code"),
            ],
            name="fk_simulation_runs_region",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["dataset_id", "tax_benefit_model_version_id"],
            [
                _public_api("datasets.id"),
                _public_api("datasets.tax_benefit_model_version_id"),
            ],
            name="fk_simulation_runs_dataset",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_simulation_runs"),
        sa.UniqueConstraint(
            "simulation_id",
            "tax_benefit_model_version_id",
            "year",
            "attempt",
            name="uq_simulation_runs_attempt",
        ),
        sa.UniqueConstraint(
            "id",
            "tax_benefit_model_version_id",
            "year",
            name="uq_simulation_runs_id_catalog_version_year",
        ),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_simulation_runs_catalog_version_region",
        "simulation_runs",
        ["tax_benefit_model_version_id", "region_code"],
        schema=SCHEMA,
    )
    op.create_index(
        "ix_simulation_runs_dataset_id",
        "simulation_runs",
        ["dataset_id"],
        schema=SCHEMA,
    )
    op.create_index(
        "ix_simulation_runs_modal_call_id",
        "simulation_runs",
        ["modal_call_id"],
        schema=SCHEMA,
        postgresql_where=sa.text("modal_call_id IS NOT NULL"),
    )
    op.create_index(
        "ix_simulation_runs_due_checks",
        "simulation_runs",
        ["next_check_at"],
        schema=SCHEMA,
        postgresql_where=sa.text("status = 'running'"),
    )
    op.create_index(
        "ix_simulation_runs_dispatching",
        "simulation_runs",
        ["updated_at"],
        schema=SCHEMA,
        postgresql_where=sa.text("status = 'dispatching'"),
    )


def _create_report_runs() -> None:
    op.create_unique_constraint(
        "uq_reports_id_kind", "reports", ["id", "kind"], schema=SCHEMA
    )
    simulation_run_key = [
        f"{SCHEMA}.simulation_runs.id",
        f"{SCHEMA}.simulation_runs.tax_benefit_model_version_id",
        f"{SCHEMA}.simulation_runs.year",
    ]
    op.create_table(
        "report_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("report_id", sa.Uuid(), nullable=False),
        sa.Column("report_kind", sa.Text(), nullable=False),
        sa.Column("tax_benefit_model_version_id", sa.Uuid(), nullable=False),
        sa.Column("year", sa.Integer(), nullable=False),
        sa.Column("baseline_simulation_run_id", sa.Uuid(), nullable=False),
        sa.Column("reform_simulation_run_id", sa.Uuid(), nullable=True),
        sa.Column("trigger", sa.Text(), nullable=False),
        *_execution_columns("comparison_", required=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "year BETWEEN 1900 AND 2200", name=op.f("ck_report_runs_year")
        ),
        sa.CheckConstraint(
            f"trigger IN ({TRIGGERS})", name=op.f("ck_report_runs_trigger")
        ),
        sa.CheckConstraint(
            "(report_kind = 'economy' AND reform_simulation_run_id IS NOT NULL "
            "AND comparison_status IS NOT NULL) "
            "OR (report_kind = 'household' AND comparison_status IS NULL "
            "AND comparison_modal_call_id IS NULL "
            "AND comparison_error_code IS NULL "
            "AND comparison_dispatched_at IS NULL "
            "AND comparison_next_check_at IS NULL "
            "AND comparison_completed_at IS NULL "
            "AND comparison_check_count = 0)",
            name=op.f("ck_report_runs_comparison_matches_kind"),
        ),
        *_status_checks("report_runs", "comparison_", optional=True),
        sa.ForeignKeyConstraint(
            ["report_id", "report_kind"],
            [f"{SCHEMA}.reports.id", f"{SCHEMA}.reports.kind"],
            name="fk_report_runs_report",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["baseline_simulation_run_id", "tax_benefit_model_version_id", "year"],
            simulation_run_key,
            name="fk_report_runs_baseline_simulation_run",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["reform_simulation_run_id", "tax_benefit_model_version_id", "year"],
            simulation_run_key,
            name="fk_report_runs_reform_simulation_run",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_report_runs"),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_report_runs_report_id_created_at",
        "report_runs",
        ["report_id", "created_at"],
        schema=SCHEMA,
    )
    for column in ("baseline_simulation_run_id", "reform_simulation_run_id"):
        op.create_index(
            f"ix_report_runs_{column}", "report_runs", [column], schema=SCHEMA
        )
    op.create_index(
        "ix_report_runs_due_comparison_checks",
        "report_runs",
        ["comparison_next_check_at"],
        schema=SCHEMA,
        postgresql_where=sa.text("comparison_status = 'running'"),
    )
    op.create_index(
        "ix_report_runs_comparisons_dispatching",
        "report_runs",
        ["updated_at"],
        schema=SCHEMA,
        postgresql_where=sa.text("comparison_status = 'dispatching'"),
    )


def upgrade() -> None:
    """Create both tables; the default privileges let the runtime role use them."""
    _create_simulation_runs()
    _create_report_runs()


def downgrade() -> None:
    """Drop the tables ``upgrade`` created, with their indexes and keys."""
    for index in (
        "ix_report_runs_comparisons_dispatching",
        "ix_report_runs_due_comparison_checks",
        "ix_report_runs_reform_simulation_run_id",
        "ix_report_runs_baseline_simulation_run_id",
        "ix_report_runs_report_id_created_at",
    ):
        op.drop_index(index, table_name="report_runs", schema=SCHEMA)
    op.drop_table("report_runs", schema=SCHEMA)
    op.drop_constraint("uq_reports_id_kind", "reports", schema=SCHEMA, type_="unique")
    for index in (
        "ix_simulation_runs_dispatching",
        "ix_simulation_runs_due_checks",
        "ix_simulation_runs_modal_call_id",
        "ix_simulation_runs_dataset_id",
        "ix_simulation_runs_catalog_version_region",
    ):
        op.drop_index(index, table_name="simulation_runs", schema=SCHEMA)
    op.drop_table("simulation_runs", schema=SCHEMA)
    op.drop_constraint(
        "uq_simulations_id_kind", "simulations", schema=SCHEMA, type_="unique"
    )
