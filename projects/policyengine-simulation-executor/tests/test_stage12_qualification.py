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
from policyengine_simulation_executor.stage12_parity import ParityMismatchError
from policyengine_simulation_executor.stage12_qualification import (
    ParityTolerances,
    QualificationRunners,
    _run_stage12_simulation_for_qualification,
    qualify_report_parity,
)
from policyengine_simulation_executor.stage12_runtime import (
    SimulationCalculation,
    calculate_segment,
)
from policyengine_simulation_executor.stage12_runtime.partition import (
    US_REGION_GROUPS,
)
from stage12_runtime_support import planned_simulation, report_execution_input


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
    report = report_execution_input()
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


def test_qualification_runner_exercises_all_segments_and_exact_merge() -> None:
    simulation = planned_simulation(SimulationRole.BASELINE)
    calls = []

    def run_segment(segment):
        calls.append(segment.segment_index)
        index = segment.segment_index
        frames = {
            "household": pd.DataFrame(
                {
                    "household_id": pd.Series([index], dtype="int64"),
                    "household_net_income": pd.Series([index + 0.5], dtype="float32"),
                }
            ),
            "person": pd.DataFrame(
                {
                    "age": pd.Series([30 + index], dtype="int16"),
                    "household_id": pd.Series([index], dtype="int64"),
                    "person_id": pd.Series([100 + index], dtype="int64"),
                }
            ),
        }
        return calculate_segment(
            segment,
            calculator=lambda _: SimulationCalculation(frames=frames),
        )

    result = _run_stage12_simulation_for_qualification(
        simulation,
        groups=US_REGION_GROUPS,
        segment_runner=run_segment,
    )

    assert calls == list(range(20))
    assert result.frames["household"]["household_id"].tolist() == list(range(20))
    assert str(result.frames["person"]["age"].dtype) == "int16"


def test_qualification_runs_matching_inputs_through_both_complete_paths() -> None:
    report = report_execution_input()
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
        assert arguments["baseline"].frames is baseline
        assert arguments["reform"].frames is reform
        assert arguments["baseline"].descriptor.bundle == report.baseline.bundle
        assert arguments["reform"].descriptor.bundle == report.reform.bundle
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
        runners=QualificationRunners(
            existing=existing_runner,
            single_simulation=single_runner,
            aggregate=aggregate_builder,
        ),
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
    report = report_execution_input()
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

    with pytest.raises(ParityMismatchError) as error:
        qualify_report_parity(
            _existing_request(),
            report,
            runners=QualificationRunners(
                existing=existing_runner,
                single_simulation=single_runner,
                aggregate=lambda **_: {"result": {}},
            ),
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

    with pytest.raises(ParityMismatchError) as error:
        qualify_report_parity(request, report_execution_input())

    assert error.value.code == code
    assert error.value.path == field


def test_qualification_rejects_existing_executor_provenance_mismatch() -> None:
    report = report_execution_input()
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

    with pytest.raises(ParityMismatchError) as error:
        qualify_report_parity(
            _existing_request(),
            report,
            runners=QualificationRunners(existing=existing_runner),
        )

    assert error.value.code == "country_package_provenance_mismatch"
    assert "different" not in str(error.value)


def _matching_existing_runner(report, baseline, reform, **overrides):
    def existing_runner(_request):
        return {
            "model_version": report.baseline.bundle.country_package_version,
            "data_version": report.baseline.bundle.dataset.artifact_revision,
            "budget": {"budgetary_impact": 20.0},
            "_microdata": extract_output_microdata(
                _Simulation(baseline),
                _Simulation(reform),
            ),
            **overrides,
        }

    return existing_runner


def _matching_single_runner(baseline, reform, calls=None):
    def single_runner(simulation):
        if calls is not None:
            calls.append(simulation.role)
        frames = baseline if simulation.role is SimulationRole.BASELINE else reform
        return SimulationCalculation(frames=frames)

    return single_runner


def test_qualification_receipt_records_every_digest_and_tolerance_field() -> None:
    from policyengine_simulation_executor.stage12_parity import NumericalTolerance

    report = report_execution_input()
    baseline = _frames(100.0)
    reform = _frames(120.0)

    receipt = qualify_report_parity(
        _existing_request(),
        report,
        runners=QualificationRunners(
            existing=_matching_existing_runner(report, baseline, reform),
            single_simulation=_matching_single_runner(baseline, reform),
            aggregate=lambda **_: {
                "result": {
                    "model_version": report.baseline.bundle.country_package_version,
                    "data_version": report.baseline.bundle.dataset.artifact_revision,
                    "budget": {"budgetary_impact": 20.0},
                }
            },
        ),
        tolerances=ParityTolerances(
            simulation={
                "person.person_household_id": NumericalTolerance(),
                "household.household_net_income": NumericalTolerance(absolute=0.1),
            },
            aggregate={"budget.budgetary_impact": NumericalTolerance()},
        ),
    )

    dumped = receipt.model_dump(mode="json")
    digests = {key: value for key, value in dumped.items() if key.endswith("sha256")}
    assert set(digests) == {
        "bundle_manifest_sha256",
        "baseline_input_sha256",
        "reform_input_sha256",
        "existing_baseline_output_sha256",
        "v2_baseline_output_sha256",
        "existing_reform_output_sha256",
        "v2_reform_output_sha256",
        "existing_aggregate_output_sha256",
        "v2_aggregate_output_sha256",
    }
    assert dumped["contract_version"] == 1
    assert dumped["evaluation_id"] == str(report.evaluation_id)
    assert dumped["bundle_manifest_sha256"] == (
        report.baseline.bundle.bundle_manifest_sha256
    )
    assert dumped["baseline_input_sha256"] != dumped["reform_input_sha256"]
    assert (
        dumped["existing_baseline_output_sha256"]
        == (dumped["v2_baseline_output_sha256"])
    )
    assert (
        dumped["existing_baseline_output_sha256"]
        != (dumped["existing_reform_output_sha256"])
    )
    assert dumped["simulation_tolerance_fields"] == [
        "household.household_net_income",
        "person.person_household_id",
    ]
    assert dumped["aggregate_tolerance_fields"] == ["budget.budgetary_impact"]


@pytest.mark.parametrize(
    ("overrides", "code", "path"),
    [
        (
            {"data_version": "different"},
            "dataset_provenance_mismatch",
            "report.data_version",
        ),
        ({"_microdata": None}, "missing_existing_microdata", "report._microdata"),
        (
            {"_microdata": {"baseline": {}, "reform": {}}},
            "invalid_existing_microdata",
            "report._microdata.baseline",
        ),
    ],
)
def test_existing_result_is_checked_before_v2_simulations_run(
    overrides, code, path
) -> None:
    report = report_execution_input()
    baseline = _frames(100.0)
    reform = _frames(120.0)
    calls = []

    with pytest.raises(ParityMismatchError) as error:
        qualify_report_parity(
            _existing_request(),
            report,
            runners=QualificationRunners(
                existing=_matching_existing_runner(
                    report, baseline, reform, **overrides
                ),
                single_simulation=_matching_single_runner(baseline, reform, calls),
            ),
        )

    assert (error.value.code, error.value.path) == (code, path)
    assert calls == []


@pytest.mark.parametrize("v2_report", [{}, {"result": None}, {"result": []}])
def test_v2_aggregate_without_a_result_object_is_rejected(v2_report) -> None:
    report = report_execution_input()
    baseline = _frames(100.0)
    reform = _frames(120.0)

    with pytest.raises(ParityMismatchError) as error:
        qualify_report_parity(
            _existing_request(),
            report,
            runners=QualificationRunners(
                existing=_matching_existing_runner(report, baseline, reform),
                single_simulation=_matching_single_runner(baseline, reform),
                aggregate=lambda **_: v2_report,
            ),
        )

    assert (error.value.code, error.value.path) == (
        "invalid_v2_aggregate",
        "report.result",
    )


@pytest.mark.parametrize("fails", [False, True])
def test_default_existing_runner_uses_and_shuts_down_a_local_runtime(
    monkeypatch, fails
) -> None:
    from policyengine_simulation_executor import (
        simulation_runtime,
        stage12_qualification,
    )

    report = report_execution_input()
    baseline = _frames(100.0)
    reform = _frames(120.0)
    events = []

    class LocalRuntime:
        def shutdown(self):
            events.append("shutdown")

    local_runtime = LocalRuntime()

    def run_simulation_impl(request, *, runtime):
        events.append(("run", request, runtime))
        if fails:
            msg = "existing executor failed"
            raise RuntimeError(msg)
        return _matching_existing_runner(report, baseline, reform)(request)

    monkeypatch.setattr(
        stage12_qualification, "_local_qualification_runtime", lambda: local_runtime
    )
    monkeypatch.setattr(simulation_runtime, "run_simulation_impl", run_simulation_impl)

    if fails:
        with pytest.raises(RuntimeError, match="existing executor failed"):
            qualify_report_parity(_existing_request(), report)
    else:
        with pytest.raises(ParityMismatchError):
            qualify_report_parity(
                _existing_request(),
                report,
                runners=QualificationRunners(
                    single_simulation=_matching_single_runner(baseline, reform),
                    aggregate=lambda **_: {"result": []},
                ),
            )

    assert events == [
        (
            "run",
            {**_existing_request(), "_emit_microdata": True},
            local_runtime,
        ),
        "shutdown",
    ]
