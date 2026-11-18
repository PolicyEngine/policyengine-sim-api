"""A worker calculates the cliff result its call names, once, under its own call ID.

A cliff result belongs to one simulation run. The worker claims it, checks
that the call's payload is that run's simulation on the run's versions,
calculates the simulation with the cliff columns, and writes the run's
cliff gap and cliff share, or the failure. It writes nothing to the
simulation run itself, to any report run, or to the comparison store.
"""

from __future__ import annotations

import logging
from hashlib import sha256
from types import SimpleNamespace

import pandas as pd
import pytest
from microdf import MicroDataFrame
from policyengine.outputs.cliff_impact import calculate_cliff_impact
from policyengine_simulation_contract.stage12_execution import (
    EntityOutputPlan,
    GeographySelection,
    ReportAggregate,
    ReportOutputRequirements,
    SimulationRole,
    Stage12OutputPlan,
)
from policyengine_simulation_executor.stage12_artifacts import (
    serialize_simulation_frames,
)
from policyengine_simulation_executor.stage12_runtime import (
    SimulationCalculation,
    SimulationWorkerDependencies,
    Stage12SegmentResult,
    build_segment_inputs,
    merge_segment_results,
    run_single_simulation,
)
from policyengine_simulation_executor.stage12_runtime import (
    cliff_results as cliff_results_module,
)
from policyengine_simulation_executor.stage12_runtime.cliff_results import (
    cliff_totals_from_frames,
)
from policyengine_simulation_executor.stage12_runtime.output_planning import (
    plan_simulation_input,
)
from policyengine_stage12_persistence import RunStatus
from stage12_fake_cliff_results import (
    CLIFF_RESULT_ID,
    FakeCliffResults,
    cliff_result_context,
    cliff_simulation,
)
from stage12_fake_report_runs import (
    BASELINE_RUN_ID,
    REFORM_RUN_ID,
    ComparisonStoreNotUsed,
)
from stage12_fake_stores import FakeArtifacts

WORKER_CALL_ID = "fc-cliff-worker"
OTHER_CALL_ID = "fc-other-cliff-worker"
CLIFF_VARIABLES = ("cliff_gap", "is_adult", "is_on_cliff")
# Three people with different weights: the gap and both counts are weighted
# sums, so an unweighted calculation gives 125.0 and a share of 1.0.
PERSON_WEIGHTS = (1.0, 3.0, 2.0)
WEIGHTED_CLIFF_GAP = 100.0 * 1.0 + 0.0 * 3.0 + 25.0 * 2.0
# People on a cliff (the first and third) over adults (the first two).
WEIGHTED_CLIFF_SHARE = (1.0 + 2.0) / (1.0 + 3.0)


def cliff_plan(country: str = "us") -> Stage12OutputPlan:
    """Return a cliff calculation's plan: the person columns the share reads."""
    return Stage12OutputPlan(
        country=country,
        requirements=ReportOutputRequirements(
            aggregates=tuple(ReportAggregate),
            include_cliff_impacts=True,
            labor_supply_response_active=False,
        ),
        entities=(
            EntityOutputPlan(
                entity="person",
                materialized_variables=tuple(
                    sorted((*CLIFF_VARIABLES, "person_id", "person_weight"))
                ),
                additional_variables=CLIFF_VARIABLES,
            ),
        ),
    )


def cliff_frames() -> dict[str, pd.DataFrame]:
    """Return a calculation's person rows, with their weights and cliff columns."""
    return {
        "person": pd.DataFrame(
            {
                "person_id": [1, 2, 3],
                "person_weight": list(PERSON_WEIGHTS),
                "cliff_gap": [100.0, 0.0, 25.0],
                "is_on_cliff": [True, False, True],
                "is_adult": [True, True, False],
            }
        )
    }


class RecordingCalculator:
    """A calculator returning ``frames`` and recording each simulation it gets."""

    def __init__(self, frames=None) -> None:
        self.frames = frames if frames is not None else cliff_frames()
        self.simulations = []

    def __call__(self, simulation):
        self.simulations.append(simulation)
        return self.frames


@pytest.fixture(autouse=True)
def _cliff_plan(monkeypatch):
    """Plan every cliff calculation without loading a country model."""
    monkeypatch.setattr(
        cliff_results_module,
        "resolve_cliff_output_plan",
        lambda simulation: cliff_plan(simulation.geography.country),
    )


def _run_cliff_worker(
    cliff_results, *, simulation=None, context=None, **dependencies
) -> dict:
    simulation = simulation or cliff_simulation()
    dependencies.setdefault("store", ComparisonStoreNotUsed())
    dependencies.setdefault("artifacts", FakeArtifacts())
    dependencies.setdefault("calculator", RecordingCalculator())
    return run_single_simulation(
        simulation.model_dump(mode="json"),
        (context or cliff_result_context()).model_dump(mode="json"),
        required_country=dependencies.pop(
            "required_country", simulation.geography.country
        ),
        modal_call_id=dependencies.pop("modal_call_id", WORKER_CALL_ID),
        dependencies=SimulationWorkerDependencies(
            cliff_results=cliff_results, **dependencies
        ),
    )


def test_a_worker_writes_its_runs_weighted_cliff_gap_and_share() -> None:
    cliff_results = FakeCliffResults()
    calculator = RecordingCalculator()

    returned = _run_cliff_worker(cliff_results, calculator=calculator)

    result = cliff_results.cliff_results[CLIFF_RESULT_ID]
    assert result.status is RunStatus.SUCCEEDED
    assert result.modal_call_id == WORKER_CALL_ID
    assert result.writes == ["claim_cliff_result", "complete_cliff_result"]
    assert result.cliff_gap == pytest.approx(WEIGHTED_CLIFF_GAP)
    assert result.cliff_share == pytest.approx(WEIGHTED_CLIFF_SHARE)
    assert returned == {
        "cliff_result_id": str(CLIFF_RESULT_ID),
        "simulation_run_id": str(BASELINE_RUN_ID),
        "status": "succeeded",
        "cliff_gap": pytest.approx(WEIGHTED_CLIFF_GAP),
        "cliff_share": pytest.approx(WEIGHTED_CLIFF_SHARE),
    }
    # It calculated the run's own simulation, planned with the cliff columns.
    (calculated,) = calculator.simulations
    assert calculated.simulation_execution_id == BASELINE_RUN_ID
    assert calculated.output_plan == cliff_plan()
    # The simulation runs and the report run are not the cliff worker's.
    assert cliff_results.simulation_runs[BASELINE_RUN_ID].writes == []
    assert cliff_results.simulation_runs[REFORM_RUN_ID].writes == []
    assert all(not run.writes for run in cliff_results.report_runs.values())


def test_a_succeeded_cliff_result_is_returned_without_calculating_again() -> None:
    cliff_results = FakeCliffResults()
    first_calculator = RecordingCalculator()
    _run_cliff_worker(cliff_results, calculator=first_calculator)
    later_calculator = RecordingCalculator()

    # Modal restarts the call that completed it, under the same call ID.
    returned = _run_cliff_worker(cliff_results, calculator=later_calculator)

    assert later_calculator.simulations == []
    result = cliff_results.cliff_results[CLIFF_RESULT_ID]
    assert result.writes == [
        "claim_cliff_result",
        "complete_cliff_result",
        "claim_cliff_result",
    ]
    assert returned["status"] == "succeeded"
    assert returned["cliff_gap"] == pytest.approx(WEIGHTED_CLIFF_GAP)
    assert returned["cliff_share"] == pytest.approx(WEIGHTED_CLIFF_SHARE)


def test_two_starts_of_one_cliff_result_calculate_it_once() -> None:
    cliff_results = FakeCliffResults()
    first_calculator = RecordingCalculator()
    second_calculator = RecordingCalculator()

    _run_cliff_worker(cliff_results, calculator=first_calculator)
    returned = _run_cliff_worker(
        cliff_results, calculator=second_calculator, modal_call_id=OTHER_CALL_ID
    )

    assert len(first_calculator.simulations) == 1
    assert second_calculator.simulations == []
    result = cliff_results.cliff_results[CLIFF_RESULT_ID]
    # The second call's claim changed nothing, and it wrote nothing else.
    assert result.writes == [
        "claim_cliff_result",
        "complete_cliff_result",
        "claim_cliff_result",
    ]
    assert result.modal_call_id == WORKER_CALL_ID
    assert returned == {
        "deduplicated": True,
        "cliff_result_id": str(CLIFF_RESULT_ID),
        "status": "succeeded",
    }


def test_a_cliff_result_another_call_holds_is_left_to_that_call() -> None:
    cliff_results = FakeCliffResults()
    result = cliff_results.cliff_results[CLIFF_RESULT_ID]
    result.status = RunStatus.RUNNING
    result.modal_call_id = OTHER_CALL_ID
    calculator = RecordingCalculator()

    returned = _run_cliff_worker(cliff_results, calculator=calculator)

    assert calculator.simulations == []
    assert result.status is RunStatus.RUNNING
    assert result.modal_call_id == OTHER_CALL_ID
    assert result.writes == ["claim_cliff_result"]
    assert returned == {
        "deduplicated": True,
        "cliff_result_id": str(CLIFF_RESULT_ID),
        "status": "running",
    }


def test_a_failed_calculation_records_its_failure_on_the_cliff_result() -> None:
    cliff_results = FakeCliffResults()

    def failing_calculation(_simulation):
        msg = "the calculation ran out of memory"
        raise RuntimeError(msg)

    with pytest.raises(RuntimeError, match=r"^Stage 12 cliff calculation failed$"):
        _run_cliff_worker(cliff_results, calculator=failing_calculation)

    result = cliff_results.cliff_results[CLIFF_RESULT_ID]
    assert result.status is RunStatus.FAILED
    assert result.writes == ["claim_cliff_result", "fail_cliff_result"]
    assert result.error_message.startswith("cliff_calculation_failed: ")
    assert (result.cliff_gap, result.cliff_share) == (None, None)
    assert cliff_results.simulation_runs[BASELINE_RUN_ID].writes == []


def test_a_payload_for_another_simulation_run_fails_the_cliff_result() -> None:
    cliff_results = FakeCliffResults()
    calculator = RecordingCalculator()

    with pytest.raises(RuntimeError, match="cliff calculation failed"):
        _run_cliff_worker(
            cliff_results,
            simulation=cliff_simulation(SimulationRole.REFORM),
            calculator=calculator,
        )

    assert calculator.simulations == []
    result = cliff_results.cliff_results[CLIFF_RESULT_ID]
    assert result.status is RunStatus.FAILED
    assert result.error_message.startswith("cliff_result_input_mismatch: ")


@pytest.mark.parametrize(
    "stored_version",
    [
        {"policyengine_version": "5.1.0"},
        {"country_package_version": "1.700.0"},
        {"year": 2025},
    ],
)
def test_a_run_calculated_on_other_versions_fails_its_cliff_result(
    stored_version,
) -> None:
    cliff_results = FakeCliffResults()
    run = cliff_results.simulation_runs[BASELINE_RUN_ID]
    for column, value in stored_version.items():
        setattr(run, column, value)
    calculator = RecordingCalculator()

    with pytest.raises(RuntimeError, match="cliff calculation failed"):
        _run_cliff_worker(cliff_results, calculator=calculator)

    assert calculator.simulations == []
    result = cliff_results.cliff_results[CLIFF_RESULT_ID]
    assert result.status is RunStatus.FAILED
    assert result.error_message.startswith("cliff_result_input_mismatch: ")


def test_a_cliff_result_failed_meanwhile_takes_no_values() -> None:
    cliff_results = FakeCliffResults()
    result = cliff_results.cliff_results[CLIFF_RESULT_ID]

    def calculation_then_sweep(_simulation):
        # The stuck-run sweep fails the cliff result while it calculates.
        result.status = RunStatus.FAILED
        result.error_message = "stuck_run: the call ended"
        return cliff_frames()

    with pytest.raises(
        RuntimeError, match=r"^Stage 12 cliff calculation was cancelled$"
    ):
        _run_cliff_worker(cliff_results, calculator=calculation_then_sweep)

    assert result.writes == ["claim_cliff_result", "complete_cliff_result"]
    assert result.error_message == "stuck_run: the call ended"
    assert (result.cliff_gap, result.cliff_share) == (None, None)


def test_a_us_national_cliff_calculation_is_split_by_region_group() -> None:
    cliff_results = FakeCliffResults()
    segmented = []
    calculator = RecordingCalculator()

    def segmented_calculator(simulation, cancellation_requested):
        segmented.append(simulation)
        assert cancellation_requested() is False
        return SimulationCalculation(frames=cliff_frames())

    _run_cliff_worker(
        cliff_results,
        calculator=calculator,
        segmented_calculator=segmented_calculator,
    )

    (split,) = segmented
    assert split.geography.region == "us"
    assert split.output_plan.requirements.include_cliff_impacts is True
    assert calculator.simulations == []
    result = cliff_results.cliff_results[CLIFF_RESULT_ID]
    assert result.cliff_gap == pytest.approx(WEIGHTED_CLIFF_GAP)
    assert result.cliff_share == pytest.approx(WEIGHTED_CLIFF_SHARE)


@pytest.mark.parametrize(
    ("region", "options"),
    [("state/ut", {}), ("us", {"segmented": False})],
)
def test_a_state_or_unsegmented_cliff_calculation_runs_in_one_worker(
    region, options
) -> None:
    cliff_results = FakeCliffResults()
    simulation = cliff_simulation().model_copy(
        update={
            "geography": GeographySelection(country="us", region=region),
            "options": options,
        }
    )
    calculator = RecordingCalculator()

    def segmented_calculator(_simulation, _cancellation_requested):
        msg = "a state calculation is not split"
        raise AssertionError(msg)

    _run_cliff_worker(
        cliff_results,
        simulation=simulation,
        calculator=calculator,
        segmented_calculator=segmented_calculator,
    )

    assert len(calculator.simulations) == 1
    assert cliff_results.cliff_results[CLIFF_RESULT_ID].status is (RunStatus.SUCCEEDED)


def test_a_dataset_with_no_adults_fails_the_cliff_result() -> None:
    cliff_results = FakeCliffResults()
    frames = cliff_frames()
    frames["person"] = frames["person"].assign(is_adult=False)

    with pytest.raises(RuntimeError, match="cliff calculation failed"):
        _run_cliff_worker(cliff_results, calculator=RecordingCalculator(frames))

    result = cliff_results.cliff_results[CLIFF_RESULT_ID]
    assert result.status is RunStatus.FAILED
    assert result.error_message.startswith("cliff_share_undefined: ")


def test_a_call_without_its_modal_call_id_claims_nothing() -> None:
    cliff_results = FakeCliffResults()

    with pytest.raises(ValueError, match="needs its Modal call ID"):
        _run_cliff_worker(cliff_results, modal_call_id=None)

    assert cliff_results.cliff_results[CLIFF_RESULT_ID].writes == []


def test_a_payload_for_another_countrys_worker_fails_the_claimed_cliff_result() -> None:
    cliff_results = FakeCliffResults()
    calculator = RecordingCalculator()

    with pytest.raises(RuntimeError, match="cliff calculation failed"):
        _run_cliff_worker(cliff_results, calculator=calculator, required_country="uk")

    assert calculator.simulations == []
    result = cliff_results.cliff_results[CLIFF_RESULT_ID]
    assert result.writes == ["claim_cliff_result", "fail_cliff_result"]
    assert result.error_message.startswith("cliff_calculation_failed: ")


class PersonVariables:
    """Stands in for a country model version: every cliff variable is a person's."""

    def get_variable(self, _name):
        return SimpleNamespace(entity="person")


def test_the_cliff_gap_and_share_are_pys_own_from_the_same_rows() -> None:
    frames = cliff_frames()
    calculated = SimpleNamespace(
        id="cliff-test",
        tax_benefit_model_version=PersonVariables(),
        output_dataset=SimpleNamespace(
            data=SimpleNamespace(
                person=MicroDataFrame(frames["person"], weights="person_weight")
            )
        ),
    )

    impact = cliff_totals_from_frames(frames, cliff_plan())

    expected = calculate_cliff_impact(calculated, calculated).baseline
    assert (impact.cliff_gap, impact.cliff_share) == (
        pytest.approx(expected.cliff_gap),
        pytest.approx(expected.cliff_share),
    )


def test_a_failed_read_during_a_split_calculation_is_logged_and_ignored(
    caplog,
) -> None:
    cliff_results = FakeCliffResults()
    readable = cliff_results.get_cliff_result

    def unreadable(_cliff_result_id):
        msg = "the session pooler has no free connections"
        raise ConnectionError(msg)

    def segmented_calculator(_simulation, cancellation_requested):
        cliff_results.get_cliff_result = unreadable
        taken = cancellation_requested()
        cliff_results.get_cliff_result = readable
        assert taken is False
        return SimulationCalculation(frames=cliff_frames())

    with caplog.at_level(logging.WARNING):
        _run_cliff_worker(cliff_results, segmented_calculator=segmented_calculator)

    assert cliff_results.cliff_results[CLIFF_RESULT_ID].status is (RunStatus.SUCCEEDED)
    assert f"Could not read cliff result {CLIFF_RESULT_ID}" in caplog.text


def test_merged_region_group_rows_give_the_cliffs_of_the_whole_country() -> None:
    """The split is exact: the share is taken from the merged rows, not averaged."""
    people = 400
    country = pd.DataFrame(
        {
            "person_id": list(range(people)),
            "person_weight": [0.5 + (person * 37) % 900 for person in range(people)],
            "cliff_gap": [
                float((person * 53) % 3_000) if person % 5 == 0 else 0.0
                for person in range(people)
            ],
            "is_on_cliff": [person % 7 == 0 for person in range(people)],
            "is_adult": [person % 10 < 7 for person in range(people)],
        }
    )
    plan = cliff_plan()
    simulation = plan_simulation_input(cliff_simulation(), plan)
    groups = [[f"state/group-{index}"] for index in range(20)]
    segments = build_segment_inputs(simulation, groups=groups)
    group_size = people // len(groups)
    payloads = []
    for segment in segments:
        start = segment.segment_index * group_size
        rows = country.iloc[start : start + group_size].reset_index(drop=True)
        parquet, _ = serialize_simulation_frames({"person": rows})
        payloads.append(
            Stage12SegmentResult(
                segment_index=segment.segment_index,
                role=simulation.role,
                region_codes=segment.region_codes,
                parquet_payload=parquet,
                payload_sha256=sha256(parquet).hexdigest(),
            ).model_dump(mode="python")
        )

    merged = merge_segment_results(simulation, payloads, groups=groups)

    split = cliff_totals_from_frames(merged.frames, plan)
    whole = cliff_totals_from_frames({"person": country}, plan)
    assert split.cliff_gap == pytest.approx(whole.cliff_gap, rel=1e-12)
    assert split.cliff_share == pytest.approx(whole.cliff_share, rel=1e-12)
