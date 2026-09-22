"""Controlled parity qualification across the existing and Stage 12 paths."""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd
import pytest

from policyengine_simulation_contract.stage12_execution import SimulationRole
from policyengine_simulation_executor.simulation_microdata import (
    extract_output_microdata,
)
from policyengine_simulation_executor.stage12_parity import ParityMismatch
from policyengine_simulation_executor.stage12_qualification import (
    qualify_report_parity,
)
from policyengine_simulation_executor.stage12_runtime import SimulationCalculation

from test_stage12_runtime import _report


def _frames(value: float) -> dict[str, pd.DataFrame]:
    return {
        "household": pd.DataFrame(
            {
                "household_id": np.array([2, 1], dtype=np.int64),
                "household_weight": np.array([1.0, 2.0], dtype=np.float32),
                "household_net_income": np.array(
                    [value + 10.0, value], dtype=np.float32
                ),
            }
        ),
        "person": pd.DataFrame(
            {
                "person_id": np.array([2, 1], dtype=np.int64),
                "person_household_id": np.array([2, 1], dtype=np.int64),
            }
        ),
    }


class _Simulation:
    def __init__(self, frames: dict[str, pd.DataFrame]):
        from types import SimpleNamespace

        self.output_dataset = SimpleNamespace(data=SimpleNamespace(entity_data=frames))


def _existing_request() -> dict[str, Any]:
    report = _report()
    return {
        "country": "us",
        "scope": "macro",
        "time_period": "2026",
        "region": "us",
        "baseline": report.baseline.policy,
        "reform": report.reform.policy,
        "policyengine_version": report.baseline.bundle.policyengine_version,
        "version": report.baseline.bundle.country_package_version,
    }


def test_qualification_runs_matching_inputs_through_both_complete_paths() -> None:
    report = _report()
    baseline = _frames(100.0)
    reform = _frames(120.0)
    simulation_calls = []

    def existing_runner(request):
        assert request == {**_existing_request(), "_emit_microdata": True}
        return {
            "model_version": report.baseline.bundle.country_package_version,
            "data_version": report.baseline.bundle.dataset.artifact_revision,
            "budget": {"budgetary_impact": 20.0},
            "poverty": {"all": {"baseline": 0.1, "reform": 0.09}},
            "_microdata": extract_output_microdata(
                _Simulation(baseline),
                _Simulation(reform),
            ),
        }

    def single_runner(simulation):
        simulation_calls.append(simulation)
        frames = baseline if simulation.role is SimulationRole.BASELINE else reform
        return SimulationCalculation(frames=frames)

    def aggregate_builder(**arguments):
        assert arguments["report"] == report
        assert arguments["baseline_frames"] is baseline
        assert arguments["reform_frames"] is reform
        assert arguments["baseline_descriptor"].bundle == report.baseline.bundle
        assert arguments["reform_descriptor"].bundle == report.reform.bundle
        return {
            "result": {
                "model_version": report.baseline.bundle.country_package_version,
                "data_version": report.baseline.bundle.dataset.artifact_revision,
                "budget": {"budgetary_impact": 20.0},
                "poverty": {"all": {"baseline": 0.1, "reform": 0.09}},
            }
        }

    receipt = qualify_report_parity(
        _existing_request(),
        report,
        existing_runner=existing_runner,
        single_simulation_runner=single_runner,
        aggregate_builder=aggregate_builder,
    )

    assert {call.role for call in simulation_calls} == {
        SimulationRole.BASELINE,
        SimulationRole.REFORM,
    }
    assert receipt.evaluation_id == str(report.evaluation_id)
    assert receipt.existing_baseline_output_sha256 == (
        receipt.v2_baseline_output_sha256
    )
    assert receipt.existing_reform_output_sha256 == receipt.v2_reform_output_sha256
    assert receipt.existing_aggregate_output_sha256 == (
        receipt.v2_aggregate_output_sha256
    )
    assert receipt.simulation_tolerance_fields == ()
    assert receipt.aggregate_tolerance_fields == ()


def test_qualification_fails_with_bounded_diagnostic_without_output_values() -> None:
    report = _report()
    baseline = _frames(100.0)
    reform = _frames(120.0)

    def existing_runner(_request):
        return {
            "model_version": report.baseline.bundle.country_package_version,
            "data_version": report.baseline.bundle.dataset.artifact_revision,
            "budget": {"budgetary_impact": 20.0},
            "_microdata": extract_output_microdata(
                _Simulation(baseline),
                _Simulation(reform),
            ),
        }

    def single_runner(simulation):
        frames = _frames(
            100.0 if simulation.role is SimulationRole.BASELINE else 999_999.0
        )
        return SimulationCalculation(frames=frames)

    with pytest.raises(ParityMismatch) as error:
        qualify_report_parity(
            _existing_request(),
            report,
            existing_runner=existing_runner,
            single_simulation_runner=single_runner,
            aggregate_builder=lambda **_: {"result": {}},
        )

    assert error.value.code == "numeric_value_mismatch"
    assert error.value.path == "household.household_net_income"
    assert "999999" not in str(error.value)


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        ("version", "different", "normalized_input_mismatch"),
        ("data", "unreviewed-dataset", "dataset_provenance_mismatch"),
    ],
)
def test_qualification_rejects_different_input_provenance(field, value, code) -> None:
    request = {**_existing_request(), field: value}

    with pytest.raises(ParityMismatch) as error:
        qualify_report_parity(request, _report())

    assert error.value.code == code
    assert error.value.path == field


def test_qualification_rejects_existing_executor_provenance_mismatch() -> None:
    report = _report()
    baseline = _frames(100.0)
    reform = _frames(120.0)

    def existing_runner(_request):
        return {
            "model_version": "different",
            "data_version": report.baseline.bundle.dataset.artifact_revision,
            "_microdata": extract_output_microdata(
                _Simulation(baseline),
                _Simulation(reform),
            ),
        }

    with pytest.raises(ParityMismatch) as error:
        qualify_report_parity(
            _existing_request(),
            report,
            existing_runner=existing_runner,
        )

    assert error.value.code == "country_package_provenance_mismatch"
    assert "different" not in str(error.value)
