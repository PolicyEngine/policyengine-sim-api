"""Add the simulation job store: simulation.jobs and simulation.job_results.

One ``jobs`` row records each calculation the Simulation Entrypoint submits to
Modal: who submitted it, its input hash and policyengine.py bundle version, the
Modal call, its status, timings and any error. ``job_results`` holds the
validated result of each succeeded job that is not a report. The runtime role
reads and writes both through the default privileges granted in 0001.

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-20
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


def upgrade() -> None:
    """Create the jobs and job_results tables."""
    op.create_table(
        "jobs",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("submitted_by", sa.Text(), nullable=False),
        sa.Column("input_sha256", sa.String(length=64), nullable=False),
        sa.Column("policyengine_version", sa.Text(), nullable=False),
        sa.Column("modal_application", sa.Text(), nullable=False),
        sa.Column("modal_function", sa.Text(), nullable=False),
        sa.Column("modal_call_id", sa.Text(), nullable=True),
        sa.Column("observability_id", sa.Text(), nullable=True),
        sa.Column("error_code", sa.Text(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column(
            "collection_attempts",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("dispatched_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('pending', 'dispatching', 'running', 'succeeded', 'failed')",
            name="ck_jobs_status",
        ),
        sa.CheckConstraint(
            "input_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_jobs_input_sha256",
        ),
        sa.CheckConstraint(
            "collection_attempts >= 0",
            name="ck_jobs_collection_attempts",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_jobs"),
        sa.UniqueConstraint("modal_call_id", name="uq_jobs_modal_call_id"),
        schema=SCHEMA,
    )
    op.create_index(
        "uq_jobs_input_sha256_unfailed",
        "jobs",
        ["input_sha256"],
        unique=True,
        schema=SCHEMA,
        postgresql_where=sa.text("status != 'failed'"),
    )
    op.create_index(
        "ix_jobs_unfinished_status",
        "jobs",
        ["status"],
        schema=SCHEMA,
        postgresql_where=sa.text("status IN ('pending', 'dispatching', 'running')"),
    )
    op.create_table(
        "job_results",
        sa.Column("job_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("result", postgresql.JSONB(), nullable=False),
        sa.Column("result_schema_version", sa.Integer(), nullable=False),
        sa.Column("result_sha256", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "result_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_job_results_result_sha256",
        ),
        sa.CheckConstraint(
            "result_schema_version >= 1",
            name="ck_job_results_result_schema_version",
        ),
        sa.ForeignKeyConstraint(
            ["job_id"],
            [f"{SCHEMA}.jobs.id"],
            name="fk_job_results_job_id_jobs",
        ),
        sa.PrimaryKeyConstraint("job_id", name="pk_job_results"),
        schema=SCHEMA,
    )


def downgrade() -> None:
    """Drop the job_results and jobs tables."""
    op.drop_table("job_results", schema=SCHEMA)
    op.drop_index("ix_jobs_unfinished_status", table_name="jobs", schema=SCHEMA)
    op.drop_index("uq_jobs_input_sha256_unfailed", table_name="jobs", schema=SCHEMA)
    op.drop_table("jobs", schema=SCHEMA)
