"""SQLAlchemy mappings of the Public API's run tables that report calls update.

``policyengine-api`` owns these tables and every schema migration. The
mappings declare only the columns the Modal worker role reads or writes, so
the coordinator and workers can issue typed DML; runtime code never emits DDL.
"""

from __future__ import annotations

from sqlalchemy import JSON, Column, DateTime, MetaData, String, Table
from sqlalchemy.dialects.postgresql import ENUM, UUID

RUN_STATUS_VALUES = ("pending", "running", "succeeded", "failed")
SIMULATION_ROLE_VALUES = ("baseline", "reform")

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
simulation_role_type = ENUM(
    *SIMULATION_ROLE_VALUES,
    name="v2_simulation_role",
    create_type=False,
)

metadata = MetaData(schema="public")

report_runs = Table(
    "report_runs",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("status", report_run_status_type, nullable=False),
    Column("modal_call_id", String(255)),
    Column("error_message", String),
    Column("started_at", DateTime(timezone=True)),
    Column("completed_at", DateTime(timezone=True)),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    Column("labor_supply_result", JSON(none_as_null=True)),
    Column("cliff_impact_result", JSON(none_as_null=True)),
)

simulation_runs = Table(
    "simulation_runs",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("status", simulation_run_status_type, nullable=False),
    Column("modal_call_id", String(255)),
    Column("error_message", String),
    Column("started_at", DateTime(timezone=True)),
    Column("completed_at", DateTime(timezone=True)),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    Column("output_uri", String(2048)),
    Column("output_sha256", String(64)),
)

report_run_simulation_runs = Table(
    "report_run_simulation_runs",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("report_run_id", UUID(as_uuid=True), nullable=False),
    Column("simulation_run_id", UUID(as_uuid=True), nullable=False),
    Column("role", simulation_role_type, nullable=False),
)
