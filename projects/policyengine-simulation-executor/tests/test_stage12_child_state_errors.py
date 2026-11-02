"""Tests that child state errors never replace the failure a report records."""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest
from policyengine_simulation_contract.stage12_execution import (
    ComparisonRunLifecycleStatus,
    PlannedSimulationExecutionInput,
    SimulationRole,
)
from policyengine_simulation_executor.stage12_runtime import (
    SegmentRunnerSeams,
    SimulationWorkerDependencies,
    Stage12SegmentRunner,
    run_single_simulation,
)
from policyengine_simulation_executor.stage12_runtime import (
    coordination as coordination_module,
)
from policyengine_simulation_executor.stage12_runtime.partition import US_REGION_GROUPS
from stage12_child_call_support import (
    ChildCallScript,
    ConcurrentInvoker,
    RefusingDispatchInvoker,
    ScriptedChildCall,
    ScriptedChildInvoker,
    assert_cancelled,
    assert_log_identifies_child,
    coordinate_with,
    coordination_log_record,
    report_with_segmentation,
)
from stage12_fake_stores import FakeArtifacts, FakeStore
from stage12_runtime_support import (
    BASELINE_ID,
    EVALUATION_ID,
    REFORM_ID,
    RecordingRuntime,
    child_record,
    coordinate_report,
    invocation_context,
    parent_record,
    planned_simulation,
    report_execution_input,
    summary_correlation_id,
)


class ChildStateError(Exception):
    """A database failure while reading or writing a child's own record."""


class OwnChildStateFailingStore(FakeStore):
    """Fail one read, or one failed-status write, of one child's record once.

    Cancellation writes are left alone, so only the failed child's own state
    update can fail.
    """

    def __init__(self, *, failing_simulation_id, failing_operation):
        super().__init__()
        self.failing_simulation_id = failing_simulation_id
        self.failing_operation = failing_operation
        self.remaining_failures = 1

    def _fail_once(self, operation, simulation_execution_id):
        if (
            operation == self.failing_operation
            and simulation_execution_id == self.failing_simulation_id
            and self.remaining_failures > 0
        ):
            self.remaining_failures -= 1
            msg = "database contains sensitive details"
            raise ChildStateError(msg)

    def get_simulation(self, simulation_execution_id):
        self._fail_once("read", simulation_execution_id)
        return super().get_simulation(simulation_execution_id)

    def replace_simulation_if_status(self, record, *, expected_status):
        if record.error_code != "simulation_cancelled":
            self._fail_once("write", record.simulation_execution_id)
        return super().replace_simulation_if_status(
            record, expected_status=expected_status
        )


@pytest.mark.parametrize(
    ("failing_operation", "baseline_status", "baseline_error_code"),
    [
        # The failure is still recorded: the read before the conditional
        # write succeeds.
        (
            "read",
            ComparisonRunLifecycleStatus.FAILED,
            "simulation_invocation_failed",
        ),
        ("write", ComparisonRunLifecycleStatus.RUNNING, None),
    ],
)
def test_failed_child_state_error_does_not_replace_the_child_failure(
    caplog,
    failing_operation,
    baseline_status,
    baseline_error_code,
) -> None:
    store = OwnChildStateFailingStore(
        failing_simulation_id=BASELINE_ID,
        failing_operation=failing_operation,
    )
    invoker = ScriptedChildInvoker(
        wait_errors={"baseline": LookupError("baseline failure is sensitive")},
    )

    with (
        caplog.at_level(logging.WARNING, logger=coordination_module.__name__),
        pytest.raises(RuntimeError, match="Stage 12 report coordination failed"),
    ):
        coordinate_with(report_with_segmentation(segmented=False), store, invoker)

    assert store.parent.error_code == "simulation_invocation_failed"
    summary_correlation_id(store.parent.error_summary)
    baseline = store.children[BASELINE_ID]
    assert baseline.status is baseline_status
    assert baseline.error_code == baseline_error_code
    if baseline_error_code is not None:
        assert baseline.error_summary == store.parent.error_summary
    # The failed child is neither cancelled nor recorded as cancelled.
    assert invoker.calls["baseline"].cancel_count == 0
    assert_cancelled(store.children[REFORM_ID], after="the baseline simulation failed")
    assert invoker.calls["reform"].cancel_count == 1
    assert_log_identifies_child(
        coordination_log_record(caplog, logging.ERROR),
        role="baseline",
        error_type="ChildStateError",
    )


class ReformDispatchFailureInvoker(ScriptedChildInvoker):
    def spawn(self, request):
        role = PlannedSimulationExecutionInput.model_validate(request.simulation).role
        if role is SimulationRole.REFORM:
            self.events.append("spawn:reform")
            msg = "dispatch contains sensitive details"
            raise ConnectionError(msg)
        return super().spawn(request)


@pytest.mark.parametrize("failing_operation", ["read", "write"])
def test_dispatch_failure_state_error_does_not_replace_the_child_failure(
    caplog,
    failing_operation,
) -> None:
    store = OwnChildStateFailingStore(
        failing_simulation_id=REFORM_ID,
        failing_operation=failing_operation,
    )
    invoker = ReformDispatchFailureInvoker()

    with (
        caplog.at_level(logging.WARNING, logger=coordination_module.__name__),
        pytest.raises(RuntimeError, match="Stage 12 report coordination failed"),
    ):
        coordinate_with(report_with_segmentation(segmented=False), store, invoker)

    assert invoker.events == ["spawn:baseline", "spawn:reform"]
    assert store.parent.error_code == "simulation_dispatch_failed"
    summary_correlation_id(store.parent.error_summary)
    # The reform record could not be updated, so it keeps its dispatch
    # placeholder rather than a cancellation.
    reform = store.children[REFORM_ID]
    assert reform.status is ComparisonRunLifecycleStatus.RUNNING
    assert reform.error_code is None
    assert_cancelled(store.children[BASELINE_ID], after="the reform simulation failed")
    assert invoker.calls["baseline"].cancel_count == 1
    assert_log_identifies_child(
        coordination_log_record(caplog, logging.ERROR),
        role="reform",
        error_type="ChildStateError",
    )


class ExceptionRecordingRuntime(RecordingRuntime):
    """Record every telemetry call that can report a failure."""

    def __init__(self) -> None:
        super().__init__()
        self.reports: list[tuple[str, object, dict, object]] = []

    def log(self, message, *, severity="INFO", attributes=None, error=None):
        self.reports.append(("log", message, dict(attributes or {}), error))

    def record_exception(self, error, *, handled, status_code=None):
        self.reports.append(("exception", None, {}, error))

    def event(self, name, *, attributes=None, **_options):
        self.reports.append(("event", name, dict(attributes or {}), None))

    def set_context(self, **_attributes):
        return None


def _fail_simulation_calculation(runtime):
    store = FakeStore()
    simulation = planned_simulation(SimulationRole.BASELINE)
    store.children[simulation.simulation_execution_id] = child_record(simulation)
    calculation_error = ValueError("calculation contains sensitive details")

    def fail(_simulation):
        raise calculation_error

    with pytest.raises(RuntimeError, match="Stage 12 simulation execution failed"):
        run_single_simulation(
            simulation.model_dump(mode="json"),
            invocation_context().model_dump(mode="json"),
            required_country="us",
            dependencies=SimulationWorkerDependencies(
                store=store,
                artifacts=FakeArtifacts(),
                calculator=fail,
                runtime=runtime,
            ),
        )
    return calculation_error, store.children[BASELINE_ID].error_summary


def _fail_child_call(runtime):
    store = FakeStore()
    child_error = LookupError("baseline failure contains sensitive details")
    invoker = ScriptedChildInvoker(wait_errors={"baseline": child_error})

    with pytest.raises(RuntimeError, match="Stage 12 report coordination failed"):
        coordinate_with(
            report_with_segmentation(segmented=False),
            store,
            invoker,
            runtime=runtime,
        )
    return child_error, store.parent.error_summary


def _fail_child_dispatch(runtime):
    store = FakeStore()
    dispatch_error = ConnectionError("dispatch contains sensitive details")

    with pytest.raises(RuntimeError, match="Stage 12 report coordination failed"):
        coordinate_with(
            report_execution_input(),
            store,
            RefusingDispatchInvoker(dispatch_error),
            runtime=runtime,
        )
    return dispatch_error, store.parent.error_summary


def _fail_report_aggregation(runtime):
    store = FakeStore()
    artifacts = FakeArtifacts()
    aggregation_error = ValueError("aggregation contains sensitive details")

    def fail_aggregation(**_values):
        raise aggregation_error

    with pytest.raises(RuntimeError, match="Stage 12 report coordination failed"):
        coordinate_report(
            report_execution_input().model_dump(mode="json"),
            invocation_context().model_dump(mode="json"),
            parent_record().model_dump(mode="json"),
            application_name=invocation_context().modal_application,
            coordinator_invocation_id="coordinator-1",
            store=store,
            artifacts=artifacts,
            invoker=ConcurrentInvoker(artifacts),
            aggregator=fail_aggregation,
            runtime=runtime,
        )
    return aggregation_error, store.parent.error_summary


@pytest.mark.parametrize(
    "fail",
    [
        _fail_simulation_calculation,
        _fail_child_call,
        _fail_child_dispatch,
        _fail_report_aggregation,
    ],
)
@pytest.mark.parametrize("with_runtime", [True, False], ids=["runtime", "no-runtime"])
def test_an_unexpected_failure_is_reported_exactly_once(
    caplog,
    fail,
    with_runtime,
) -> None:
    runtime = ExceptionRecordingRuntime() if with_runtime else None

    with caplog.at_level(logging.DEBUG):
        original_error, error_summary = fail(runtime)

    correlation_id = summary_correlation_id(error_summary)
    logged_failures = [record for record in caplog.records if record.exc_info]
    if runtime is not None:
        [(kind, message, attributes, reported_error)] = [
            report for report in runtime.reports if report[0] == "log"
        ]
        assert (kind, message) == ("log", "Stage 12 execution failed")
        assert reported_error is original_error
        assert attributes["correlation_id"] == correlation_id
        # The redacted cause, never the original error, is also attached to
        # the failing operation, once.
        [recorded_cause] = [
            report[3] for report in runtime.reports if report[0] == "exception"
        ]
        assert str(recorded_cause) == error_summary
        assert sorted(report[0] for report in runtime.reports) == ["exception", "log"]
        assert logged_failures == []
    else:
        [logged_failure] = logged_failures
        assert logged_failure.exc_info[1] is original_error
        assert f"correlation_id={correlation_id}" in logged_failure.getMessage()


def test_a_segment_failure_is_reported_exactly_once(caplog) -> None:
    store = FakeStore()
    simulation = planned_simulation(SimulationRole.BASELINE)
    store.children[simulation.simulation_execution_id] = child_record(simulation)
    runtime = ExceptionRecordingRuntime()
    segment_error = RuntimeError("segment contains sensitive details")

    class SegmentFunction:
        def spawn(self, payload, *, observability_context):
            segment_index = payload["segment_index"]
            # The first segment fails on every probe; the others keep running.
            return ScriptedChildCall(
                f"segment-{segment_index}",
                script=ChildCallScript(
                    wait_error=segment_error if segment_index == 0 else None
                ),
            )

    class SegmentFunctionLookup:
        @staticmethod
        def from_name(_app_name, _function_name):
            return SegmentFunction()

    def segmented_calculator(value, cancellation_requested):
        return Stage12SegmentRunner(
            value,
            app_name="stage12-app",
            runtime=runtime,
            cancellation_requested=cancellation_requested,
            seams=SegmentRunnerSeams(
                modal_module=SimpleNamespace(Function=SegmentFunctionLookup),
                groups=US_REGION_GROUPS,
                sleep=lambda _seconds: None,
            ),
        ).run()

    with (
        caplog.at_level(logging.DEBUG),
        pytest.raises(RuntimeError, match="Stage 12 simulation execution failed"),
    ):
        run_single_simulation(
            simulation.model_dump(mode="json"),
            invocation_context().model_dump(mode="json"),
            required_country="us",
            dependencies=SimulationWorkerDependencies(
                store=store,
                artifacts=FakeArtifacts(),
                calculator=lambda _: pytest.fail("monolithic calculator was selected"),
                segmented_calculator=segmented_calculator,
                runtime=runtime,
            ),
        )

    child = store.children[BASELINE_ID]
    assert child.error_code == "segment_execution_failed"
    [(kind, message, attributes, reported_error)] = [
        report for report in runtime.reports if report[0] == "log"
    ]
    [recorded_cause] = [
        report[3] for report in runtime.reports if report[0] == "exception"
    ]
    assert str(recorded_cause) == child.error_summary
    assert (kind, message) == ("log", "Stage 12 execution failed")
    assert reported_error is segment_error
    assert attributes == {
        "stage12_failure_scope": "segment_call",
        "simulation_role": "baseline",
        "evaluation_id": str(EVALUATION_ID),
        "simulation_execution_id": str(BASELINE_ID),
        "segment_index": 0,
        "correlation_id": summary_correlation_id(
            child.error_summary.removeprefix("Segment 0: ")
        ),
    }
    assert [record for record in caplog.records if record.exc_info] == []
