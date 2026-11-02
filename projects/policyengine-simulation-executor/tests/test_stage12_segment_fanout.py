"""Unit tests for independent Stage 12 Modal segment orchestration."""

from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast

import pandas as pd
import pytest
from policyengine_simulation_contract.stage12_execution import SimulationRole
from policyengine_simulation_executor.stage12_runtime import (
    SegmentRunnerSeams,
    SimulationCalculation,
    Stage12CancellationError,
    Stage12ExecutionError,
    Stage12SegmentRunner,
    build_segment_inputs,
    calculate_segment,
)
from policyengine_simulation_executor.stage12_runtime.partition import (
    US_REGION_GROUPS,
)
from policyengine_simulation_observability.stages import (
    STAGE12_SIMULATION_STAGES,
    Stage,
)
from stage12_runtime_support import planned_simulation

if TYPE_CHECKING:
    from policyengine_observability import ObservabilityRuntime


def _frames(index: int) -> dict[str, pd.DataFrame]:
    return {
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


class FakeRuntime:
    def __init__(self) -> None:
        self.context: dict[str, object] = {}
        self.spans: list[tuple[str, dict | None]] = []

    def set_context(self, **attributes) -> None:
        self.context.update(attributes)

    def capture_context(self) -> dict[str, str]:
        return {"observability_id": "00000000-0000-4000-8000-000000000001"}

    @contextmanager
    def span(self, name, *, attributes=None):
        self.spans.append((name, attributes))
        yield


class FakeCall:
    def __init__(self, outcomes: list[object]) -> None:
        self.outcomes = outcomes
        self.get_timeouts: list[float] = []
        self.cancelled = 0

    def get(self, *, timeout: float):
        self.get_timeouts.append(timeout)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    def cancel(self) -> None:
        self.cancelled += 1


class FakeFunction:
    def __init__(self, results: list[dict], errors: dict[int, list[Exception]]):
        self.results = results
        self.errors = errors
        self.spawns: list[tuple[dict, dict | None]] = []
        self.calls: list[FakeCall] = []

    def spawn(self, payload: dict, *, observability_context: dict | None):
        index = payload["segment_index"]
        self.spawns.append((payload, observability_context))
        call = FakeCall([*self.errors.get(index, []), self.results[index]])
        self.calls.append(call)
        return call


def _results(simulation) -> list[dict]:
    return [
        calculate_segment(
            segment,
            calculator=lambda scoped, index=segment.segment_index: (
                SimulationCalculation(frames=_frames(index))
            ),
        ).model_dump(mode="python")
        for segment in build_segment_inputs(simulation)
    ]


def _runner(
    *,
    errors: dict[int, list[Exception]] | None = None,
    runtime: object | None = None,
):
    simulation = planned_simulation(SimulationRole.BASELINE)
    function = FakeFunction(_results(simulation), errors or {})

    class FunctionLookup:
        @staticmethod
        def from_name(app_name, function_name):
            assert app_name == "stage12-app"
            assert function_name == "run_single_simulation_segment_us"
            return function

    span_runtime = runtime if runtime is not None else FakeRuntime()
    sleeps: list[float] = []
    runner = Stage12SegmentRunner(
        simulation,
        app_name="stage12-app",
        runtime=cast("ObservabilityRuntime", span_runtime),
        seams=SegmentRunnerSeams(
            modal_module=SimpleNamespace(Function=FunctionLookup),
            groups=US_REGION_GROUPS,
            sleep=sleeps.append,
            poll_interval_seconds=0.5,
            poll_interval_max_seconds=2.0,
        ),
    )
    return runner, function, span_runtime, sleeps


def _fail_dispatch_of_segment_five(function: FakeFunction) -> None:
    spawn = function.spawn

    def fail_during_dispatch(payload, *, observability_context):
        if payload["segment_index"] == 5:
            msg = "dispatch failed"
            raise ConnectionError(msg)
        return spawn(payload, observability_context=observability_context)

    function.spawn = fail_during_dispatch


def _return_segment_zero_for_segment_one(function: FakeFunction) -> None:
    # The merge then reads segment index 0 twice and refuses the result set.
    function.results[1] = function.results[0]


def test_starts_twenty_segments_and_merges_them_in_partition_order() -> None:
    runner, function, runtime, sleeps = _runner()

    result = runner.run()

    assert len(function.spawns) == 20
    assert [payload["segment_index"] for payload, _ in function.spawns] == list(
        range(20)
    )
    assert all(context is not None for _, context in function.spawns)
    assert all(call.get_timeouts == [0] for call in function.calls)
    assert result.frames["household"]["household_id"].tolist() == list(range(20))
    assert runtime.context["stage12_segment_count"] == 20
    assert sleeps == []


def test_retries_one_poll_error_without_restarting_the_segment() -> None:
    runner, function, _, _ = _runner(errors={0: [ConnectionError("transient")]})

    runner.run()

    assert len(function.spawns) == 20
    assert function.calls[0].get_timeouts == [0, 0]
    assert not any(call.cancelled for call in function.calls)


def test_not_ready_polls_use_bounded_backoff() -> None:
    runner, function, _, sleeps = _runner(
        errors={index: [TimeoutError()] for index in range(20)}
    )

    runner.run()

    assert sleeps == [0.5]
    assert all(call.get_timeouts == [0, 0] for call in function.calls)


def test_second_poll_error_cancels_all_started_segments() -> None:
    runner, function, _, _ = _runner(
        errors={0: [ConnectionError("first"), RuntimeError("second")]}
    )

    with pytest.raises(Stage12ExecutionError) as error:
        runner.run()

    assert len(function.calls) == 20
    assert all(call.cancelled == 1 for call in function.calls)
    assert error.value.detail.error_code == "segment_execution_failed"
    assert error.value.detail.segment_index == 0
    assert error.value.detail.error_summary.startswith(
        "Segment 0: RuntimeError: second (correlation_id="
    )


def test_dispatch_error_cancels_segments_that_already_started() -> None:
    runner, function, _, _ = _runner()
    _fail_dispatch_of_segment_five(function)

    with pytest.raises(ConnectionError, match="dispatch failed"):
        runner.run()

    assert len(function.calls) == 5
    assert all(call.cancelled == 1 for call in function.calls)


def test_persisted_parent_cancellation_cancels_all_segments() -> None:
    runner, function, _, sleeps = _runner(
        errors={index: [TimeoutError()] for index in range(20)}
    )
    checks = iter([False, False, True])
    runner.cancellation_requested = lambda: next(checks, True)

    with pytest.raises(Stage12CancellationError):
        runner.run()

    assert sleeps == [0.5]
    assert all(call.cancelled == 1 for call in function.calls)


def test_merge_error_is_raised_from_the_merge_span() -> None:
    runner, function, runtime, _ = _runner()
    _return_segment_zero_for_segment_one(function)

    with pytest.raises(ValueError, match="duplicate index"):
        runner.run()

    assert runtime.spans[-1][0] == STAGE12_SIMULATION_STAGES.name(
        Stage.STAGE12_SEGMENT_MERGE
    )
    assert not any(call.cancelled for call in function.calls)


def test_segments_are_dispatched_collected_and_merged_in_the_real_spans(
    real_observability_runtime,
) -> None:
    runner, function, _, _ = _runner(runtime=real_observability_runtime)

    result = runner.run()

    assert len(function.spawns) == 20
    assert result.frames["household"]["household_id"].tolist() == list(range(20))


def test_dispatch_error_is_raised_from_the_real_dispatch_span(
    real_observability_runtime,
) -> None:
    runner, function, _, _ = _runner(runtime=real_observability_runtime)
    _fail_dispatch_of_segment_five(function)

    with pytest.raises(ConnectionError, match="dispatch failed"):
        runner.run()

    assert len(function.calls) == 5
    assert all(call.cancelled == 1 for call in function.calls)


def test_poll_error_is_raised_from_the_real_wait_span(
    real_observability_runtime,
) -> None:
    runner, function, _, _ = _runner(
        errors={0: [ConnectionError("first"), RuntimeError("second")]},
        runtime=real_observability_runtime,
    )

    with pytest.raises(Stage12ExecutionError) as error:
        runner.run()

    assert error.value.detail.error_code == "segment_execution_failed"
    assert all(call.cancelled == 1 for call in function.calls)


def test_merge_error_is_raised_from_the_real_merge_span(
    real_observability_runtime,
) -> None:
    runner, function, _, _ = _runner(runtime=real_observability_runtime)
    _return_segment_zero_for_segment_one(function)

    with pytest.raises(ValueError, match="duplicate index"):
        runner.run()
