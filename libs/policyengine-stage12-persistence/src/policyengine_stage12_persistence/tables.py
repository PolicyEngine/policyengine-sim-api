"""Synchronized SQLAlchemy mapped classes for the temporary Stage 12 tables.

``policyengine-api`` owns these tables and every schema migration. These
classes exist only so the temporary simulation runtime can issue typed DML;
runtime code must never call ``metadata.create_all`` or otherwise emit DDL.
"""

from __future__ import annotations

import uuid  # noqa: TC003  # SQLAlchemy reads Mapped annotations at run time
from datetime import datetime  # noqa: TC003  # SQLAlchemy reads Mapped annotations
from typing import ClassVar

from sqlalchemy import (
    JSON,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import ENUM, UUID
from sqlalchemy.orm import Mapped, mapped_column

from policyengine_stage12_persistence.orm_base import ComparisonBase

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

metadata = ComparisonBase.metadata


class ComparisonReport(ComparisonBase):
    """One report's run on the comparison path."""

    __tablename__ = REPORT_TABLE_NAME
    __table_args__: ClassVar = (
        UniqueConstraint(
            "environment",
            "calculation_flow",
            "production_identity",
            "worker_version",
            "version_manifest_sha256",
            name=REPORT_IDENTITY_CONSTRAINT,
        ),
        Index(
            "ix_stage12_eval_reports_status_retention",
            "status",
            "retention_expires_at",
        ),
        Index(
            "ix_stage12_eval_reports_production_identity",
            "calculation_flow",
            "production_identity",
        ),
    )

    evaluation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True
    )
    contract_version: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(run_status_type)
    aggregation_status: Mapped[str] = mapped_column(aggregation_status_type)
    environment: Mapped[str] = mapped_column(String(255))
    calculation_flow: Mapped[str] = mapped_column(String(255))
    originating_request_id: Mapped[str] = mapped_column(String(255))
    observability_id: Mapped[str | None] = mapped_column(String(36))
    production_identity: Mapped[str] = mapped_column(String(255))
    incumbent_execution_id: Mapped[str | None] = mapped_column(String(255))
    worker_version: Mapped[str] = mapped_column(String(255))
    modal_application: Mapped[str] = mapped_column(String(255))
    report_coordinator_callable: Mapped[str] = mapped_column(String(255))
    version_manifest_sha256: Mapped[str] = mapped_column(String(64))
    policyengine_version: Mapped[str] = mapped_column(String(255))
    country_package_name: Mapped[str] = mapped_column(String(255))
    country_package_version: Mapped[str] = mapped_column(String(255))
    country: Mapped[str] = mapped_column(String(2))
    dataset_identity: Mapped[str] = mapped_column(String(255))
    dataset_uri: Mapped[str] = mapped_column(String(2048))
    data_package_name: Mapped[str] = mapped_column(String(255))
    data_package_version: Mapped[str] = mapped_column(String(255))
    data_artifact_revision: Mapped[str] = mapped_column(String(255))
    coordinator_invocation_id: Mapped[str | None] = mapped_column(String(255))
    error_code: Mapped[str | None] = mapped_column(String(64))
    error_summary: Mapped[str | None] = mapped_column(String(512))
    aggregate_output_uri: Mapped[str | None] = mapped_column(String(2048))
    aggregate_output_sha256: Mapped[str | None] = mapped_column(String(64))
    aggregate_schema_version: Mapped[int | None] = mapped_column(Integer)
    comparison_status: Mapped[str] = mapped_column(result_comparison_status_type)
    comparison_output_uri: Mapped[str | None] = mapped_column(String(2048))
    comparison_output_sha256: Mapped[str | None] = mapped_column(String(64))
    comparison_schema_version: Mapped[int | None] = mapped_column(Integer)
    comparison_completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    comparison_error_code: Mapped[str | None] = mapped_column(String(64))
    comparison_error_summary: Mapped[str | None] = mapped_column(String(512))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    retention_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ComparisonSimulation(ComparisonBase):
    """One simulation's execution within a comparison report."""

    __tablename__ = SIMULATION_TABLE_NAME
    __table_args__: ClassVar = (
        UniqueConstraint(
            "evaluation_id",
            "role",
            "input_sha256",
            "worker_version",
            "version_manifest_sha256",
            name=SIMULATION_IDENTITY_CONSTRAINT,
        ),
        Index(
            "ix_stage12_eval_simulations_parent_status",
            "evaluation_id",
            "status",
        ),
        Index(
            "ix_stage12_eval_simulations_status_retention",
            "status",
            "retention_expires_at",
        ),
    )

    simulation_execution_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True
    )
    evaluation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(
            "public.stage12_evaluation_reports.evaluation_id",
            ondelete="CASCADE",
        ),
    )
    contract_version: Mapped[int] = mapped_column(Integer)
    role: Mapped[str] = mapped_column(simulation_role_type)
    input_sha256: Mapped[str] = mapped_column(String(64))
    worker_version: Mapped[str] = mapped_column(String(255))
    modal_application: Mapped[str] = mapped_column(String(255))
    simulation_callable: Mapped[str] = mapped_column(String(255))
    version_manifest_sha256: Mapped[str] = mapped_column(String(64))
    modal_invocation_id: Mapped[str | None] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(run_status_type)
    error_code: Mapped[str | None] = mapped_column(String(64))
    error_summary: Mapped[str | None] = mapped_column(String(512))
    output_uri: Mapped[str | None] = mapped_column(String(2048))
    output_sha256: Mapped[str | None] = mapped_column(String(64))
    output_schema_version: Mapped[int | None] = mapped_column(Integer)
    row_identity_columns: Mapped[list[str] | None] = mapped_column(
        JSON(none_as_null=True)
    )
    row_count: Mapped[int | None] = mapped_column(Integer)
    row_identity_sha256: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    retention_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


# The Core tables the store's statements use until they name these classes.
comparison_reports = ComparisonReport.__table__
comparison_simulations = ComparisonSimulation.__table__

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
