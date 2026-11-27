"""The Modal classes the v2 runtime's simulation workers are reached through."""

import pytest
from policyengine_simulation_contract.household_execution import (
    household_callable_name,
)
from policyengine_simulation_contract.stage12_simulation_workers import (
    SEGMENT_WORKER_CLASS,
    SEGMENT_WORKER_METHOD,
    worker_class_name,
)


@pytest.mark.parametrize(
    ("method_name", "expected_class_name"),
    [
        ("run_single_simulation_us", "SimulationWorkerUS"),
        ("run_single_simulation_uk", "SimulationWorkerUK"),
        ("run_single_simulation_segment_us", "SimulationSegmentWorkerUS"),
    ],
)
def test_each_simulation_worker_is_a_method_of_its_class_under_its_old_name(
    method_name, expected_class_name
) -> None:
    # The v2 version manifest, coordinator contexts and comparison-run rows
    # store the worker's function name, so the same name reaches the method.
    assert worker_class_name(method_name) == expected_class_name


def test_the_segment_worker_names_agree() -> None:
    assert worker_class_name(SEGMENT_WORKER_METHOD) == SEGMENT_WORKER_CLASS


@pytest.mark.parametrize(
    "function_name",
    [
        "coordinate_report",
        household_callable_name("us"),
        household_callable_name("uk"),
        "validate_worker_us",
        # The run_simulation app's worker, which no v2 caller names.
        "run_simulation",
    ],
)
def test_the_v2_apps_other_functions_have_no_worker_class(function_name) -> None:
    assert worker_class_name(function_name) is None
