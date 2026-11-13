"""A report input may compare one simulation, calculated once, with itself.

A report whose baseline and reform have the same policy and dynamic names one
simulation and one run of it on both sides. Its two inputs then carry that
run's ID as their execution ID, and must be the same simulation in every
other way too, policy included.
"""

from __future__ import annotations

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
    SimulationExecutionInput,
    SimulationRole,
)
from pydantic import JsonValue, ValidationError

REPORT_RUN_ID = UUID("00000000-0000-0000-0000-0000000000a1")
SIMULATION_RUN_ID = UUID("00000000-0000-0000-0000-0000000000b1")
OTHER_SIMULATION_RUN_ID = UUID("00000000-0000-0000-0000-0000000000c1")
DATASET_URI = "hf://policyengine/data/microcosm_uk_2024_25.h5@revision"
REFORM_POLICY: dict[str, JsonValue] = {
    "gov.hmrc.income_tax.allowances.personal_allowance.amount": {
        "2026-01-01": 13_000
    }
}


def _side(
    role: SimulationRole,
    simulation_run_id: UUID,
    *,
    policy: dict[str, JsonValue] | None = None,
) -> SimulationExecutionInput:
    return SimulationExecutionInput(
        evaluation_id=REPORT_RUN_ID,
        simulation_execution_id=simulation_run_id,
        role=role,
        policy=policy or {},
        population=DatasetPopulationInput(
            artifact=DatasetArtifactReference(
                uri=DATASET_URI,
                media_type=DatasetArtifactMediaType.HDF5,
                content_sha256="d" * 64,
            )
        ),
        year=2026,
        geography=GeographySelection(country="uk", region="country/wales"),
        options={},
        bundle=BundleProvenance(
            policyengine_version="6.17.1",
            country_package_name="policyengine-uk",
            country_package_version="2.174.1",
            dataset=DatasetProvenance(
                identity="microcosm_uk_2024_25",
                uri=DATASET_URI,
                artifact_revision="revision",
                data_package_name="microcosm-uk",
                data_package_version="1.0.0",
            ),
            bundle_manifest_sha256="b" * 64,
        ),
    )


def _report(
    baseline: SimulationExecutionInput, reform: SimulationExecutionInput
) -> ReportExecutionInput:
    return ReportExecutionInput(
        evaluation_id=REPORT_RUN_ID,
        baseline=baseline,
        reform=reform,
        requested_aggregates=tuple(ReportAggregate),
    )


def test_a_report_may_name_one_simulation_run_on_both_sides() -> None:
    report = _report(
        _side(SimulationRole.BASELINE, SIMULATION_RUN_ID),
        _side(SimulationRole.REFORM, SIMULATION_RUN_ID),
    )

    assert report.baseline.simulation_execution_id == SIMULATION_RUN_ID
    assert report.reform.simulation_execution_id == SIMULATION_RUN_ID


def test_one_simulation_run_on_both_sides_must_have_one_policy() -> None:
    with pytest.raises(ValidationError, match="one simulation run on both sides"):
        _report(
            _side(SimulationRole.BASELINE, SIMULATION_RUN_ID),
            _side(SimulationRole.REFORM, SIMULATION_RUN_ID, policy=REFORM_POLICY),
        )


def test_two_simulation_runs_may_have_the_same_policy() -> None:
    # A forced re-run of a report comparing a simulation with itself
    # calculates two runs of it.
    report = _report(
        _side(SimulationRole.BASELINE, SIMULATION_RUN_ID),
        _side(SimulationRole.REFORM, OTHER_SIMULATION_RUN_ID),
    )

    assert report.reform.simulation_execution_id == OTHER_SIMULATION_RUN_ID
