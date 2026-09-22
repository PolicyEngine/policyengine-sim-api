from __future__ import annotations

from policyengine_simulation_contract.stage12_execution import (
    ComparisonReportRecord,
    ComparisonSimulationRecord,
)
from policyengine_stage12_persistence.tables import (
    REPORT_CHECK_CONSTRAINTS,
    REPORT_COLUMNS,
    REPORT_IDENTITY_CONSTRAINT,
    SIMULATION_CHECK_CONSTRAINTS,
    SIMULATION_COLUMNS,
    SIMULATION_IDENTITY_CONSTRAINT,
    comparison_reports,
    comparison_simulations,
    metadata,
)
from sqlalchemy import ForeignKeyConstraint, UniqueConstraint


def test_mappings_cover_every_cross_service_record_field() -> None:
    assert set(REPORT_COLUMNS) == set(ComparisonReportRecord.model_fields)
    assert set(SIMULATION_COLUMNS) == set(ComparisonSimulationRecord.model_fields)
    assert comparison_reports.schema == "public"
    assert comparison_simulations.schema == "public"


def test_mappings_retain_identity_and_parent_constraints() -> None:
    report_unique = next(
        constraint
        for constraint in comparison_reports.constraints
        if isinstance(constraint, UniqueConstraint)
    )
    simulation_unique = next(
        constraint
        for constraint in comparison_simulations.constraints
        if isinstance(constraint, UniqueConstraint)
    )
    parent_key = next(
        constraint
        for constraint in comparison_simulations.constraints
        if isinstance(constraint, ForeignKeyConstraint)
    )

    assert report_unique.name == REPORT_IDENTITY_CONSTRAINT
    assert tuple(column.name for column in report_unique.columns) == (
        "environment",
        "calculation_flow",
        "production_identity",
        "worker_version",
        "version_manifest_sha256",
    )
    assert simulation_unique.name == SIMULATION_IDENTITY_CONSTRAINT
    assert tuple(column.name for column in simulation_unique.columns) == (
        "evaluation_id",
        "role",
        "input_sha256",
        "worker_version",
        "version_manifest_sha256",
    )
    assert parent_key.ondelete == "CASCADE"
    assert parent_key.name == (
        "fk_stage12_evaluation_simulations_evaluation_id_"
        "stage12_evaluation_reports"
    )
    assert comparison_reports.primary_key.name == "pk_stage12_evaluation_reports"
    assert (
        comparison_simulations.primary_key.name
        == "pk_stage12_evaluation_simulations"
    )


def test_mapping_metadata_is_declarative_only() -> None:
    assert set(metadata.tables) == {
        "public.stage12_evaluation_reports",
        "public.stage12_evaluation_simulations",
    }
    assert REPORT_CHECK_CONSTRAINTS
    assert SIMULATION_CHECK_CONSTRAINTS
