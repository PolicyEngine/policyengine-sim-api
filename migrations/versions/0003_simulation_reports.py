"""Add the report tables: simulation.reports and simulation.report_results.

A report row records an economy or household report. A row migrated from
API v1 keeps the v1 report ID in ``legacy_report_id`` and starts
``not_started`` with no job; once a job is attached the report is
``submitted`` and its progress is that job's status. ``report_results`` holds
one aggregate per finished report job, validated by a Pydantic model and
stored as JSONB with its schema version, SHA-256, summary columns and
artifact references. Nothing writes these tables yet.

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-20
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

SCHEMA = "simulation"


def upgrade() -> None:
    """Create the reports and report_results tables."""
    op.create_table(
        "reports",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("country", sa.Text(), nullable=False),
        sa.Column("legacy_report_id", sa.BigInteger(), nullable=True),
        sa.Column("job_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "kind IN ('economy', 'household')",
            name="ck_reports_kind",
        ),
        sa.CheckConstraint("country ~ '^[a-z]{2}$'", name="ck_reports_country"),
        sa.CheckConstraint(
            "(status = 'not_started' AND job_id IS NULL) "
            "OR (status = 'submitted' AND job_id IS NOT NULL)",
            name="ck_reports_status_matches_job",
        ),
        sa.ForeignKeyConstraint(
            ["job_id"],
            [f"{SCHEMA}.jobs.id"],
            name="fk_reports_job_id_jobs",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_reports"),
        sa.UniqueConstraint("legacy_report_id", name="uq_reports_legacy_report_id"),
        schema=SCHEMA,
    )
    op.create_index("ix_reports_job_id", "reports", ["job_id"], schema=SCHEMA)
    op.create_table(
        "report_results",
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("report_kind", sa.Text(), nullable=False),
        sa.Column("aggregate", postgresql.JSONB(), nullable=False),
        sa.Column("aggregate_schema_version", sa.Integer(), nullable=False),
        sa.Column("aggregate_sha256", sa.String(length=64), nullable=False),
        sa.Column("budgetary_impact", sa.Float(), nullable=True),
        sa.Column("tax_revenue_impact", sa.Float(), nullable=True),
        sa.Column("state_tax_revenue_impact", sa.Float(), nullable=True),
        sa.Column("benefit_spending_impact", sa.Float(), nullable=True),
        sa.Column("households", sa.Float(), nullable=True),
        sa.Column("baseline_net_income", sa.Float(), nullable=True),
        sa.Column("model_version", sa.Text(), nullable=True),
        sa.Column("data_version", sa.Text(), nullable=True),
        sa.Column(
            "artifacts",
            postgresql.JSONB(),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "report_kind IN ('economy', 'household')",
            name="ck_report_results_report_kind",
        ),
        sa.CheckConstraint(
            "aggregate_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_report_results_aggregate_sha256",
        ),
        sa.CheckConstraint(
            "aggregate_schema_version >= 1",
            name="ck_report_results_aggregate_schema_version",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(artifacts) = 'array'",
            name="ck_report_results_artifacts",
        ),
        sa.ForeignKeyConstraint(
            ["job_id"],
            [f"{SCHEMA}.jobs.id"],
            name="fk_report_results_job_id_jobs",
        ),
        sa.PrimaryKeyConstraint("job_id", name="pk_report_results"),
        schema=SCHEMA,
    )


def downgrade() -> None:
    """Drop the report_results and reports tables."""
    op.drop_table("report_results", schema=SCHEMA)
    op.drop_index("ix_reports_job_id", table_name="reports", schema=SCHEMA)
    op.drop_table("reports", schema=SCHEMA)
