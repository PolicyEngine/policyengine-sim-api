"""Unit tests for independent Stage 12 Modal segment orchestration."""

from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast
from uuid import UUID

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


class ClassNotDeployedError(Exception):
    """Stands in for ``modal.exception.NotFoundError``."""


def _segment_worker_lookup(
    segment_function: object,
    lookups: list[tuple[str, str, str]] | None = None,
    *,
    class_deployed: bool = True,
) -> SimpleNamespace:
    """Stand in for ``modal``: its segment worker spawns ``segment_function``.

    Each lookup by name is appended to ``lookups`` as ``(kind, app, name)``.
    With ``class_deployed`` False, hydrating the class raises the not-found
    error, and the plain function of the method's name spawns instead.
    """

    def record(kind: str, app_name: str, name: str) -> None:
        if lookups is not None:
            lookups.append((kind, app_name, name))

    class SegmentWorkerClass:
        def hydrate(self):
            if not class_deployed:
                msg = "the app defines no such class"
                raise ClassNotDeployedError(msg)
            return self

        def __call__(self):
            return SimpleNamespace(run_single_simulation_segment_us=segment_function)

    class ClassLookup:
        @staticmethod
        def from_name(app_name, class_name):
            record("class", app_name, class_name)
            return SegmentWorkerClass()

    class FunctionLookup:
        @staticmethod
        def from_name(app_name, function_name):
            record("function", app_name, function_name)
            return segment_function

    return SimpleNamespace(
        Cls=ClassLookup,
        Function=FunctionLookup,
        exception=SimpleNamespace(NotFoundError=ClassNotDeployedError),
    )


def test_segments_are_started_through_the_segment_worker_class() -> None:
    # The class's containers restore from a memory snapshot with the US
    # models loaded.
    simulation = planned_simulation(SimulationRole.BASELINE)
    function = FakeFunction(_results(simulation), {})
    lookups: list[tuple[str, str, str]] = []
    runner = Stage12SegmentRunner(
        simulation,
        app_name="stage12-app",
        runtime=cast("ObservabilityRuntime", FakeRuntime()),
        seams=SegmentRunnerSeams(
            modal_module=_segment_worker_lookup(function, lookups),
            groups=US_REGION_GROUPS,
            sleep=lambda _seconds: None,
        ),
    )

    runner.run()

    assert lookups == [("class", "stage12-app", "SimulationSegmentWorkerUS")]
    assert len(function.spawns) == len(US_REGION_GROUPS)


def test_segments_start_on_the_function_when_the_app_has_no_segment_class() -> None:
    # A US simulation still running when its app is rolled back in place, to a
    # commit from before the classes, finds only the plain function.
    simulation = planned_simulation(SimulationRole.BASELINE)
    function = FakeFunction(_results(simulation), {})
    lookups: list[tuple[str, str, str]] = []
    runner = Stage12SegmentRunner(
        simulation,
        app_name="stage12-app",
        runtime=cast("ObservabilityRuntime", FakeRuntime()),
        seams=SegmentRunnerSeams(
            modal_module=_segment_worker_lookup(
                function, lookups, class_deployed=False
            ),
            groups=US_REGION_GROUPS,
            sleep=lambda _seconds: None,
        ),
    )

    runner.run()

    assert lookups == [
        ("class", "stage12-app", "SimulationSegmentWorkerUS"),
        ("function", "stage12-app", "run_single_simulation_segment_us"),
    ]
    assert len(function.spawns) == len(US_REGION_GROUPS)


def _runner(
    *,
    errors: dict[int, list[Exception]] | None = None,
    runtime: object | None = None,
):
    simulation = planned_simulation(SimulationRole.BASELINE)
    function = FakeFunction(_results(simulation), errors or {})

    span_runtime = runtime if runtime is not None else FakeRuntime()
    sleeps: list[float] = []
    runner = Stage12SegmentRunner(
        simulation,
        app_name="stage12-app",
        runtime=cast("ObservabilityRuntime", span_runtime),
        seams=SegmentRunnerSeams(
            modal_module=_segment_worker_lookup(function),
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


class InputCancelledForTest(BaseException):
    """Stands in for Modal's InputCancellation, a BaseException but not an Exception."""


@pytest.mark.parametrize("interruption", [KeyboardInterrupt, InputCancelledForTest])
def test_preempted_or_cancelled_worker_cancels_every_started_segment(
    interruption,
) -> None:
    # Modal pre-empts a call with SIGINT (KeyboardInterrupt) and cancels one
    # with InputCancellation; it does not cancel the calls the worker spawned.
    runner, function, _, _ = _runner(errors={3: [interruption()]})

    with pytest.raises(interruption):
        runner.run()

    assert len(function.calls) == 20
    assert all(call.cancelled == 1 for call in function.calls)


def test_worker_preempted_during_dispatch_cancels_the_segments_it_started() -> None:
    runner, function, _, _ = _runner()
    spawn = function.spawn

    def preempt_during_dispatch(payload, *, observability_context):
        if payload["segment_index"] == 5:
            raise KeyboardInterrupt
        return spawn(payload, observability_context=observability_context)

    function.spawn = preempt_during_dispatch

    with pytest.raises(KeyboardInterrupt):
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


class FunctionRecordingSpawnKeywords(FakeFunction):
    """A segment function that also records each spawn's keyword arguments."""

    def __init__(self, results: list[dict]) -> None:
        super().__init__(results, {})
        self.spawn_keywords: list[dict] = []

    def spawn(self, payload: dict, **keywords):
        self.spawn_keywords.append(keywords)
        return super().spawn(
            payload, observability_context=keywords["observability_context"]
        )


@pytest.mark.parametrize(
    ("simulation_run_id", "expected_keyword"),
    [
        (
            UUID("00000000-0000-4000-8000-00000000b001"),
            {"simulation_run_id": "00000000-0000-4000-8000-00000000b001"},
        ),
        (None, {}),
    ],
    ids=["simulation-run", "comparison-path"],
)
def test_segments_are_started_naming_the_simulation_run_of_their_worker(
    simulation_run_id, expected_keyword
) -> None:
    # Each segment's telemetry names the simulation run it calculates part of.
    simulation = planned_simulation(SimulationRole.BASELINE)
    function = FunctionRecordingSpawnKeywords(_results(simulation))

    runner = Stage12SegmentRunner(
        simulation,
        app_name="stage12-app",
        runtime=cast("ObservabilityRuntime", FakeRuntime()),
        simulation_run_id=simulation_run_id,
        seams=SegmentRunnerSeams(
            modal_module=_segment_worker_lookup(function),
            groups=US_REGION_GROUPS,
            sleep=lambda _seconds: None,
        ),
    )

    runner.run()

    assert len(function.spawn_keywords) == 20
    for keywords in function.spawn_keywords:
        assert set(keywords) == {"observability_context", *expected_keyword}
        assert keywords.get("simulation_run_id") == expected_keyword.get(
            "simulation_run_id"
        )
