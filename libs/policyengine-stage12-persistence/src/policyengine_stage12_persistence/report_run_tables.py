"""SQLAlchemy mappings of the Public API's run tables that v2 Modal calls update.

``policyengine-api`` owns these tables and every schema migration. The
mappings declare only the columns the coordinator, the workers, the household
functions and the stuck-run sweep read or write as the Modal worker role, so
they can issue typed DML; runtime code never emits DDL.
"""

from __future__ import annotations

from sqlalchemy import JSON, Column, DateTime, Float, MetaData, String, Table
from sqlalchemy.dialects.postgresql import ENUM, JSONB, UUID

RUN_STATUS_VALUES = ("pending", "running", "succeeded", "failed")

report_run_status_type = ENUM(
    *RUN_STATUS_VALUES,
    name="v2_report_run_status",
    create_type=False,
)
simulation_run_status_type = ENUM(
    *RUN_STATUS_VALUES,
    name="v2_simulation_status",
    create_type=False,
)
household_job_status_type = ENUM(
    *RUN_STATUS_VALUES,
    name="v2_household_job_status",
    create_type=False,
)

metadata = MetaData(schema="public")

# A report run compares one pair of simulation runs; a report with only a
# baseline has no reform run. The cliff impact, SPM receipts and data version
# are the report run's own results.
report_runs = Table(
    "report_runs",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("baseline_simulation_run_id", UUID(as_uuid=True), nullable=False),
    Column("reform_simulation_run_id", UUID(as_uuid=True)),
    Column("status", report_run_status_type, nullable=False),
    Column("modal_call_id", String(255)),
    Column("error_message", String),
    Column("started_at", DateTime(timezone=True)),
    Column("completed_at", DateTime(timezone=True)),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    Column("baseline_cliff_gap", Float),
    Column("baseline_cliff_share", Float),
    Column("reform_cliff_gap", Float),
    Column("reform_cliff_share", Float),
    Column("spm_provenance", JSONB(none_as_null=True)),
    Column("data_version", String(128)),
)

simulation_runs = Table(
    "simulation_runs",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("status", simulation_run_status_type, nullable=False),
    Column("modal_call_id", String(255)),
    Column("error_message", String),
    Column("started_at", DateTime(timezone=True)),
    Column("completed_at", DateTime(timezone=True)),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    Column("output_uri", String(2048)),
    Column("output_sha256", String(64)),
)

# A household job is one household calculation, which a household function
# claims under its Modal call. ``result`` holds the calculation's
# ``HouseholdExecutionResult`` or ``HouseholdExecutionFailure`` document.
household_jobs = Table(
    "household_jobs",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("status", household_job_status_type, nullable=False),
    Column("modal_call_id", String(255)),
    Column("error_message", String),
    Column("result", JSON(none_as_null=True)),
    Column("started_at", DateTime(timezone=True)),
    Column("completed_at", DateTime(timezone=True)),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)
