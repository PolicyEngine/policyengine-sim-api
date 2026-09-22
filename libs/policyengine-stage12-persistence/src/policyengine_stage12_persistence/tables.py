"""Synchronized SQLAlchemy mappings for the temporary Stage 12 tables.

``policyengine-api`` owns these tables and every schema migration. These Core
mappings exist only so the temporary simulation runtime can issue typed DML;
runtime code must never call ``metadata.create_all`` or otherwise emit DDL.
"""

from __future__ import annotations

from sqlalchemy import (
    JSON,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import ENUM, UUID

REPORT_TABLE_NAME = "stage12_evaluation_reports"
SIMULATION_TABLE_NAME = "stage12_evaluation_simulations"
REPORT_IDENTITY_CONSTRAINT = "uq_stage12_eval_reports_identity"
SIMULATION_IDENTITY_CONSTRAINT = "uq_stage12_eval_simulations_identity"

RUN_STATUS_VALUES = (
    "pending",
    "running",
    "succeeded",
    "failed",
    "incomplete",
    "skipped",
)
AGGREGATION_STATUS_VALUES = (
    "not_started",
    "running",
    "succeeded",
    "failed",
)
RESULT_COMPARISON_STATUS_VALUES = (
    "not_requested",
    "pending",
    "running",
    "matched",
    "different",
    "failed",
)
SIMULATION_ROLE_VALUES = ("baseline", "reform", "standalone")

run_status_type = ENUM(
    *RUN_STATUS_VALUES,
    name="v2_stage12_evaluation_status",
    create_type=False,
)
aggregation_status_type = ENUM(
    *AGGREGATION_STATUS_VALUES,
    name="v2_stage12_aggregation_status",
    create_type=False,
)
result_comparison_status_type = ENUM(
    *RESULT_COMPARISON_STATUS_VALUES,
    name="v2_stage12_result_comparison_status",
    create_type=False,
)
simulation_role_type = ENUM(
    *SIMULATION_ROLE_VALUES,
    name="v2_stage12_simulation_role",
    create_type=False,
)

metadata = MetaData(
    schema="public",
    naming_convention={
        "ix": "ix_%(table_name)s_%(column_0_N_name)s",
        "uq": "uq_%(table_name)s_%(column_0_N_name)s",
        "ck": "%(constraint_name)s",
        "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
        "pk": "pk_%(table_name)s",
    },
)

comparison_reports = Table(
    REPORT_TABLE_NAME,
    metadata,
    Column("evaluation_id", UUID(as_uuid=True), primary_key=True),
    Column("contract_version", Integer, nullable=False),
    Column("status", run_status_type, nullable=False),
    Column("aggregation_status", aggregation_status_type, nullable=False),
    Column("environment", String(255), nullable=False),
    Column("calculation_flow", String(255), nullable=False),
    Column("originating_request_id", String(255), nullable=False),
    Column("production_identity", String(255), nullable=False),
    Column("incumbent_execution_id", String(255)),
    Column("worker_version", String(255), nullable=False),
    Column("modal_application", String(255), nullable=False),
    Column("report_coordinator_callable", String(255), nullable=False),
    Column("version_manifest_sha256", String(64), nullable=False),
    Column("policyengine_version", String(255), nullable=False),
    Column("country_package_name", String(255), nullable=False),
    Column("country_package_version", String(255), nullable=False),
    Column("country", String(2), nullable=False),
    Column("dataset_identity", String(255), nullable=False),
    Column("dataset_uri", String(2048), nullable=False),
    Column("data_package_name", String(255), nullable=False),
    Column("data_package_version", String(255), nullable=False),
    Column("data_artifact_revision", String(255), nullable=False),
    Column("coordinator_invocation_id", String(255)),
    Column("error_code", String(64)),
    Column("error_summary", String(512)),
    Column("aggregate_output_uri", String(2048)),
    Column("aggregate_output_sha256", String(64)),
    Column("aggregate_schema_version", Integer),
    Column(
        "comparison_status",
        result_comparison_status_type,
        nullable=False,
    ),
    Column("comparison_output_uri", String(2048)),
    Column("comparison_output_sha256", String(64)),
    Column("comparison_schema_version", Integer),
    Column("comparison_completed_at", DateTime(timezone=True)),
    Column("comparison_error_code", String(64)),
    Column("comparison_error_summary", String(512)),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    Column("started_at", DateTime(timezone=True)),
    Column("completed_at", DateTime(timezone=True)),
    Column("retention_expires_at", DateTime(timezone=True), nullable=False),
    UniqueConstraint(
        "environment",
        "calculation_flow",
        "production_identity",
        "worker_version",
        "version_manifest_sha256",
        name=REPORT_IDENTITY_CONSTRAINT,
    ),
)
Index(
    "ix_stage12_eval_reports_status_retention",
    comparison_reports.c.status,
    comparison_reports.c.retention_expires_at,
)
Index(
    "ix_stage12_eval_reports_production_identity",
    comparison_reports.c.calculation_flow,
    comparison_reports.c.production_identity,
)

comparison_simulations = Table(
    SIMULATION_TABLE_NAME,
    metadata,
    Column("simulation_execution_id", UUID(as_uuid=True), primary_key=True),
    Column(
        "evaluation_id",
        UUID(as_uuid=True),
        ForeignKey(
            "public.stage12_evaluation_reports.evaluation_id",
            ondelete="CASCADE",
        ),
        nullable=False,
    ),
    Column("contract_version", Integer, nullable=False),
    Column("role", simulation_role_type, nullable=False),
    Column("input_sha256", String(64), nullable=False),
    Column("worker_version", String(255), nullable=False),
    Column("modal_application", String(255), nullable=False),
    Column("simulation_callable", String(255), nullable=False),
    Column("version_manifest_sha256", String(64), nullable=False),
    Column("modal_invocation_id", String(255)),
    Column("status", run_status_type, nullable=False),
    Column("error_code", String(64)),
    Column("error_summary", String(512)),
    Column("output_uri", String(2048)),
    Column("output_sha256", String(64)),
    Column("output_schema_version", Integer),
    Column("row_identity_columns", JSON(none_as_null=True)),
    Column("row_count", Integer),
    Column("row_identity_sha256", String(64)),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    Column("started_at", DateTime(timezone=True)),
    Column("completed_at", DateTime(timezone=True)),
    Column("retention_expires_at", DateTime(timezone=True), nullable=False),
    UniqueConstraint(
        "evaluation_id",
        "role",
        "input_sha256",
        "worker_version",
        "version_manifest_sha256",
        name=SIMULATION_IDENTITY_CONSTRAINT,
    ),
)
Index(
    "ix_stage12_eval_simulations_parent_status",
    comparison_simulations.c.evaluation_id,
    comparison_simulations.c.status,
)
Index(
    "ix_stage12_eval_simulations_status_retention",
    comparison_simulations.c.status,
    comparison_simulations.c.retention_expires_at,
)

REPORT_COLUMNS = tuple(comparison_reports.c.keys())
SIMULATION_COLUMNS = tuple(comparison_simulations.c.keys())

REPORT_CHECK_CONSTRAINTS = frozenset(
    {
        "ck_stage12_eval_reports_contract_version",
        "ck_stage12_eval_reports_retention",
        "ck_stage12_eval_reports_output_fields",
        "ck_stage12_eval_reports_success",
        "ck_stage12_eval_reports_error_code",
    }
)
SIMULATION_CHECK_CONSTRAINTS = frozenset(
    {
        "ck_stage12_eval_simulations_contract_version",
        "ck_stage12_eval_simulations_retention",
        "ck_stage12_eval_simulations_output_fields",
        "ck_stage12_eval_simulations_success",
        "ck_stage12_eval_simulations_error_code",
    }
)
