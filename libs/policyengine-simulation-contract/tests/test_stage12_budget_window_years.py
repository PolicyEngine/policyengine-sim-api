"""Each year of a budget window is calculated as its first year is, in its own year.

A budget window's years differ only in their year and the runs they record
on: the same policies, dataset, region, options and bundle. The simulation
service builds the first year's coordinator input, and every other year's,
whether the service or a coordinator starts it, is derived from it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from policyengine_simulation_contract.stage12_execution import (
    BundleProvenance,
    DatasetArtifactMediaType,
    DatasetArtifactReference,
    DatasetPopulationInput,
    DatasetProvenance,
    GeographySelection,
    ReportAggregate,
    ReportExecutionInput,
    ReportRunIds,
    SimulationExecutionInput,
    SimulationRole,
    Stage12InvocationContext,
    report_run_artifact_prefix,
    year_calculation,
)

FIRST_CREATED_AT = datetime(2026, 11, 12, 9, tzinfo=UTC)
LATER_CREATED_AT = datetime(2026, 12, 1, 3, tzinfo=UTC)
DATASET_URI = "hf://policyengine/data/enhanced_cps_2024.h5@revision"
FIRST_YEAR_RUNS = ReportRunIds(
    report_run_id=UUID("00000000-0000-0000-0000-0000000000a1"),
    baseline_simulation_run_id=UUID("00000000-0000-0000-0000-0000000000b1"),
    reform_simulation_run_id=UUID("00000000-0000-0000-0000-0000000000c1"),
)
LATER_YEAR_RUNS = ReportRunIds(
    report_run_id=UUID("00000000-0000-0000-0000-0000000000a4"),
    baseline_simulation_run_id=UUID("00000000-0000-0000-0000-0000000000b4"),
    reform_simulation_run_id=UUID("00000000-0000-0000-0000-0000000000c4"),
)


def _side(role: SimulationRole, simulation_run_id: UUID) -> SimulationExecutionInput:
    return SimulationExecutionInput(
        evaluation_id=FIRST_YEAR_RUNS.report_run_id,
        simulation_execution_id=simulation_run_id,
        role=role,
        policy=(
            {}
            if role is SimulationRole.BASELINE
            else {"gov.irs.credits.ctc.amount.base[0].amount": {"2026-01-01": 3000}}
        ),
        population=DatasetPopulationInput(
            artifact=DatasetArtifactReference(
                uri=DATASET_URI,
                media_type=DatasetArtifactMediaType.HDF5,
                content_sha256="d" * 64,
            )
        ),
        year=2026,
        geography=GeographySelection(country="us", region="us"),
        options={"include_cliffs": True},
        bundle=BundleProvenance(
            policyengine_version="6.17.1",
            country_package_name="policyengine-us",
            country_package_version="1.500.0",
            dataset=DatasetProvenance(
                identity="enhanced_cps_2024",
                uri=DATASET_URI,
                artifact_revision="revision",
                data_package_name="policyengine-us-data",
                data_package_version="1.2.0",
            ),
            bundle_manifest_sha256="b" * 64,
        ),
    )


def _first_year() -> tuple[ReportExecutionInput, Stage12InvocationContext]:
    report = ReportExecutionInput(
        evaluation_id=FIRST_YEAR_RUNS.report_run_id,
        baseline=_side(
            SimulationRole.BASELINE, FIRST_YEAR_RUNS.baseline_simulation_run_id
        ),
        reform=_side(SimulationRole.REFORM, FIRST_YEAR_RUNS.reform_simulation_run_id),
        requested_aggregates=tuple(ReportAggregate),
    )
    context = Stage12InvocationContext(
        request_id="request-1",
        environment="staging",
        modal_environment="staging",
        worker_version="6.17.1",
        modal_application="policyengine-simulation-v2-py6-17-1",
        simulation_callable="run_single_simulation_us",
        version_manifest_sha256="a" * 64,
        bundle_manifest_sha256="b" * 64,
        artifact_prefix=report_run_artifact_prefix(
            "staging", FIRST_CREATED_AT, FIRST_YEAR_RUNS.report_run_id
        ),
        created_at=FIRST_CREATED_AT,
        retention_expires_at=FIRST_CREATED_AT + timedelta(days=30),
        report_run=FIRST_YEAR_RUNS,
    )
    return report, context


def test_the_artifact_prefix_names_the_environment_month_and_run() -> None:
    assert report_run_artifact_prefix(
        "production", LATER_CREATED_AT, LATER_YEAR_RUNS.report_run_id
    ) == (f"stage-12-runs/production/2026/12/{LATER_YEAR_RUNS.report_run_id}")


def test_a_later_year_is_calculated_on_its_own_year_and_runs() -> None:
    report, context = _first_year()

    later_report, later_context = year_calculation(
        report,
        context,
        year=2029,
        run_ids=LATER_YEAR_RUNS,
        created_at=LATER_CREATED_AT,
    )

    assert later_report.evaluation_id == LATER_YEAR_RUNS.report_run_id
    for side, simulation_run_id in (
        (later_report.baseline, LATER_YEAR_RUNS.baseline_simulation_run_id),
        (later_report.reform, LATER_YEAR_RUNS.reform_simulation_run_id),
    ):
        assert side.year == 2029
        assert side.evaluation_id == LATER_YEAR_RUNS.report_run_id
        assert side.simulation_execution_id == simulation_run_id
    assert later_context.report_run == LATER_YEAR_RUNS
    assert later_context.artifact_prefix == report_run_artifact_prefix(
        "staging", LATER_CREATED_AT, LATER_YEAR_RUNS.report_run_id
    )
    assert later_context.created_at == LATER_CREATED_AT
    # The first year's retention period, counted from the later year's start.
    assert later_context.retention_expires_at == LATER_CREATED_AT + timedelta(days=30)


def test_nothing_else_differs_between_the_years() -> None:
    report, context = _first_year()

    later_report, later_context = year_calculation(
        report,
        context,
        year=2029,
        run_ids=LATER_YEAR_RUNS,
        created_at=LATER_CREATED_AT,
    )

    # Put back the year and the runs: what remains is the first year's input.
    restored_report = later_report.model_copy(
        update={
            "evaluation_id": report.evaluation_id,
            "baseline": later_report.baseline.model_copy(
                update={
                    "year": 2026,
                    "evaluation_id": report.evaluation_id,
                    "simulation_execution_id": report.baseline.simulation_execution_id,
                }
            ),
            "reform": later_report.reform.model_copy(
                update={
                    "year": 2026,
                    "evaluation_id": report.evaluation_id,
                    "simulation_execution_id": report.reform.simulation_execution_id,
                }
            ),
        }
    )
    restored_context = later_context.model_copy(
        update={
            "report_run": context.report_run,
            "artifact_prefix": context.artifact_prefix,
            "created_at": context.created_at,
            "retention_expires_at": context.retention_expires_at,
        }
    )
    assert restored_report == report
    assert restored_context == context


def test_a_call_naming_no_report_run_starts_no_year() -> None:
    report, context = _first_year()

    with pytest.raises(ValueError, match="names no report run"):
        year_calculation(
            report,
            context.model_copy(update={"report_run": None}),
            year=2029,
            run_ids=LATER_YEAR_RUNS,
            created_at=LATER_CREATED_AT,
        )


def test_the_derived_year_is_checked_as_any_input_is() -> None:
    report, context = _first_year()

    with pytest.raises(ValueError, match="year"):
        year_calculation(
            report,
            context,
            year=2201,
            run_ids=LATER_YEAR_RUNS,
            created_at=LATER_CREATED_AT,
        )
