"""Tests for the failure records and reports of a failed Stage 12 coordination."""

from __future__ import annotations

import pytest
from policyengine_simulation_contract.stage12_execution import (
    ComparisonRunLifecycleStatus,
)
from sqlalchemy.exc import OperationalError
from stage12_child_call_support import (
    ConcurrentInvoker,
    InjectedChildFaults,
    RefusingDispatchInvoker,
    TimeoutInvoker,
)
from stage12_fake_stores import FakeArtifacts, FakeStore
from stage12_runtime_support import (
    BASELINE_ID,
    EVALUATION_ID,
    NOW,
    RecordingRuntime,
    TelemetryRecordingRuntime,
    aggregate_payload,
    child_record,
    connection_limit_error,
    coordinate_report,
    invocation_context,
    parent_record,
    report_execution_input,
    summary_correlation_id,
    summary_failure_cause,
)


def test_coordinator_reports_failure_before_an_unwritable_failure_record() -> None:
    ordered_events: list[tuple[str, object]] = []
    aggregation_error = ValueError("aggregate failed validation")

    class UnwritableFailureStore(FakeStore):
        def replace_report(self, record):
            if record.status is ComparisonRunLifecycleStatus.FAILED:
                ordered_events.append(("failure_write", record.error_summary))
                raise connection_limit_error()
            return super().replace_report(record)

    def fail_aggregation(**_):
        raise aggregation_error

    store = UnwritableFailureStore()
    artifacts = FakeArtifacts()

    with pytest.raises(
        RuntimeError, match="Stage 12 report coordination failed"
    ) as error:
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
            runtime=TelemetryRecordingRuntime(ordered_events),
        )

    assert error.value.__cause__ is None
    original_report, failure_write, persistence_report = ordered_events
    assert original_report[1][1] == {
        "stage12_failure_scope": "report_coordination",
        "evaluation_id": str(EVALUATION_ID),
        "correlation_id": summary_correlation_id(failure_write[1]),
    }
    assert original_report[1][2] is aggregation_error
    assert failure_write[0] == "failure_write"
    assert summary_failure_cause(failure_write[1]) == (
        "ValueError in stage12_aggregation: aggregate failed validation"
    )
    assert isinstance(persistence_report[1][2], OperationalError)
    assert store.parent.status is ComparisonRunLifecycleStatus.RUNNING


def test_coordinator_reports_a_child_failure_once_when_its_record_is_unwritable() -> (
    None
):
    ordered_events: list[tuple[str, object]] = []

    class UnreadableChildStore(FakeStore):
        def get_simulation(self, simulation_execution_id):
            ordered_events.append(("child_read", str(simulation_execution_id)))
            raise connection_limit_error()

    store = UnreadableChildStore()
    artifacts = FakeArtifacts()

    with pytest.raises(RuntimeError, match="Stage 12 report coordination failed"):
        coordinate_report(
            report_execution_input().model_dump(mode="json"),
            invocation_context().model_dump(mode="json"),
            parent_record().model_dump(mode="json"),
            application_name=invocation_context().modal_application,
            coordinator_invocation_id="coordinator-1",
            store=store,
            artifacts=artifacts,
            invoker=ConcurrentInvoker(
                artifacts, faults=InjectedChildFaults(fail_role="baseline")
            ),
            aggregator=lambda **_: aggregate_payload({"must": "not run"}),
            runtime=TelemetryRecordingRuntime(ordered_events),
        )

    failure_reports = [
        event[1]
        for event in ordered_events
        if event[0] == "telemetry" and event[1][0] == "Stage 12 execution failed"
    ]
    assert [report[1] for report in failure_reports] == [
        {
            "stage12_failure_scope": "child_wait",
            "simulation_role": "baseline",
            "evaluation_id": str(EVALUATION_ID),
            "simulation_execution_id": str(BASELINE_ID),
            "correlation_id": summary_correlation_id(store.parent.error_summary),
        }
    ]
    assert ordered_events[0][0] == "telemetry"
    assert ordered_events[1] == ("child_read", str(BASELINE_ID))
    # The report record is still written after the child record could not be.
    assert store.parent.status is ComparisonRunLifecycleStatus.FAILED
    assert store.parent.error_code == "simulation_invocation_failed"
    assert summary_failure_cause(store.parent.error_summary) == (
        "RuntimeError in stage12_child_wait (baseline): child calculation failed"
    )


def test_coordinator_reports_a_child_dispatch_failure_once() -> None:
    ordered_events: list[tuple[str, object]] = []
    dispatch_error = ConnectionError("Modal refused the dispatch")
    store = FakeStore()
    invoker = RefusingDispatchInvoker(dispatch_error)

    with pytest.raises(RuntimeError, match="Stage 12 report coordination failed"):
        coordinate_report(
            report_execution_input().model_dump(mode="json"),
            invocation_context().model_dump(mode="json"),
            parent_record().model_dump(mode="json"),
            application_name=invocation_context().modal_application,
            coordinator_invocation_id="coordinator-1",
            store=store,
            artifacts=FakeArtifacts(),
            invoker=invoker,
            aggregator=lambda **_: aggregate_payload({"must": "not run"}),
            runtime=TelemetryRecordingRuntime(ordered_events),
        )

    failure_reports = [
        event[1]
        for event in ordered_events
        if event[0] == "telemetry" and event[1][0] == "Stage 12 execution failed"
    ]
    baseline_child = store.children[BASELINE_ID]
    assert [(attributes, error) for _message, attributes, error in failure_reports] == [
        (
            {
                "stage12_failure_scope": "child_dispatch",
                "simulation_role": "baseline",
                "evaluation_id": str(EVALUATION_ID),
                "simulation_execution_id": str(BASELINE_ID),
                "correlation_id": summary_correlation_id(baseline_child.error_summary),
            },
            dispatch_error,
        )
    ]
    assert invoker.events == ["spawn:baseline"]
    assert baseline_child.status is ComparisonRunLifecycleStatus.FAILED
    assert baseline_child.error_code == "simulation_dispatch_failed"
    assert summary_failure_cause(baseline_child.error_summary) == (
        "ConnectionError in stage12_child_dispatch (baseline): "
        "Modal refused the dispatch"
    )
    assert store.parent.status is ComparisonRunLifecycleStatus.FAILED
    assert store.parent.error_summary == baseline_child.error_summary


def test_coordinator_records_a_redacted_dispatch_failure_cause() -> None:
    store = FakeStore()
    artifacts = FakeArtifacts()
    invoker = RefusingDispatchInvoker(
        ConnectionError("Modal rejected the call with token=opaque-token")
    )
    runtime = RecordingRuntime()

    with pytest.raises(RuntimeError, match="Stage 12 report coordination failed"):
        coordinate_report(
            report_execution_input().model_dump(mode="json"),
            invocation_context().model_dump(mode="json"),
            parent_record().model_dump(mode="json"),
            application_name=invocation_context().modal_application,
            coordinator_invocation_id="coordinator-1",
            store=store,
            artifacts=artifacts,
            invoker=invoker,
            aggregator=lambda **_: aggregate_payload({"must": "not run"}),
            runtime=runtime,
        )

    assert invoker.events == ["spawn:baseline"]
    assert artifacts.aggregate_writes == []
    failed_child = store.children[BASELINE_ID]
    assert failed_child.status is ComparisonRunLifecycleStatus.FAILED
    assert failed_child.error_code == "simulation_dispatch_failed"
    assert summary_failure_cause(failed_child.error_summary) == (
        "ConnectionError in stage12_child_dispatch (baseline): "
        "Modal rejected the call with token=[REDACTED]"
    )
    assert store.parent.error_code == "simulation_dispatch_failed"
    assert store.parent.error_summary == failed_child.error_summary
    [recorded_cause] = runtime.recorded_exceptions
    assert str(recorded_cause) == failed_child.error_summary


def test_coordinator_rejects_incompatible_row_identity() -> None:
    store = FakeStore()
    artifacts = FakeArtifacts()
    invoker = ConcurrentInvoker(
        artifacts, faults=InjectedChildFaults(incompatible=True)
    )

    with pytest.raises(RuntimeError, match="Stage 12 report coordination failed"):
        coordinate_report(
            report_execution_input().model_dump(mode="json"),
            invocation_context().model_dump(mode="json"),
            parent_record().model_dump(mode="json"),
            application_name=invocation_context().modal_application,
            coordinator_invocation_id="coordinator-1",
            store=store,
            artifacts=artifacts,
            invoker=invoker,
            aggregator=lambda **_: aggregate_payload({"must": "not run"}),
        )
    assert artifacts.aggregate_writes == []


def test_coordinator_reuses_matching_successful_child() -> None:
    report = report_execution_input()
    store = FakeStore()
    artifacts = FakeArtifacts()
    baseline_descriptor = artifacts.add_simulation(report.baseline)
    baseline = child_record(report.baseline).model_copy(
        update={
            "status": ComparisonRunLifecycleStatus.SUCCEEDED,
            "output_uri": baseline_descriptor.artifact.uri,
            "output_sha256": baseline_descriptor.artifact.content_sha256,
            "output_schema_version": 1,
            "row_identity_columns": baseline_descriptor.row_identity.identifier_columns,
            "row_count": baseline_descriptor.row_identity.row_count,
            "row_identity_sha256": baseline_descriptor.row_identity.identity_sha256,
            "completed_at": NOW,
        }
    )
    store.children[report.baseline.simulation_execution_id] = baseline
    invoker = ConcurrentInvoker(artifacts)

    coordinate_report(
        report.model_dump(mode="json"),
        invocation_context().model_dump(mode="json"),
        parent_record().model_dump(mode="json"),
        application_name=invocation_context().modal_application,
        coordinator_invocation_id="coordinator-1",
        store=store,
        artifacts=artifacts,
        invoker=invoker,
        aggregator=lambda **_: aggregate_payload(),
    )

    assert invoker.events == ["spawn:reform"]


def test_coordinator_resumes_a_matching_running_child_invocation() -> None:
    report = report_execution_input()
    store = FakeStore()
    artifacts = FakeArtifacts()
    store.children[report.baseline.simulation_execution_id] = child_record(
        report.baseline
    ).model_copy(
        update={
            "status": ComparisonRunLifecycleStatus.RUNNING,
            "modal_invocation_id": "call-baseline",
            "started_at": NOW,
        }
    )
    invoker = ConcurrentInvoker(artifacts)

    coordinate_report(
        report.model_dump(mode="json"),
        invocation_context().model_dump(mode="json"),
        parent_record().model_dump(mode="json"),
        application_name=invocation_context().modal_application,
        coordinator_invocation_id="coordinator-1",
        store=store,
        artifacts=artifacts,
        invoker=invoker,
        aggregator=lambda **_: aggregate_payload(),
    )

    assert invoker.events == ["restore:baseline", "spawn:reform"]


def test_coordinator_records_bounded_timeout_without_an_aggregate() -> None:
    store = FakeStore()
    artifacts = FakeArtifacts()
    invoker = TimeoutInvoker()

    with pytest.raises(
        RuntimeError, match="Stage 12 report coordination failed"
    ) as error:
        coordinate_report(
            report_execution_input().model_dump(mode="json"),
            invocation_context().model_dump(mode="json"),
            parent_record().model_dump(mode="json"),
            application_name=invocation_context().modal_application,
            coordinator_invocation_id="coordinator-1",
            store=store,
            artifacts=artifacts,
            invoker=invoker,
            aggregator=lambda **_: aggregate_payload({"must": "not run"}),
            wait_timeout_seconds=0,
        )

    assert "sensitive" not in str(error.value)

    assert invoker.events == ["spawn:baseline", "spawn:reform"]
    assert artifacts.aggregate_writes == []
    assert store.parent.status is ComparisonRunLifecycleStatus.FAILED
    assert store.parent.error_code == "simulation_wait_timeout"
    assert store.parent.error_summary == (
        "Stage 12 simulations did not finish within 0 seconds of the report "
        "coordinator starting."
    )
    assert all(
        child.status is ComparisonRunLifecycleStatus.FAILED
        for child in store.children.values()
    )
    assert all(
        child.error_code == "simulation_cancelled" for child in store.children.values()
    )


def test_closed_wait_window_is_reported_before_its_failure_records() -> None:
    ordered_events: list[tuple[str, object]] = []

    class UnwritableFailureStore(FakeStore):
        def replace_simulation_if_status(self, record, *, expected_status):
            if record.status is ComparisonRunLifecycleStatus.FAILED:
                ordered_events.append(("failure_write", record.error_code))
                raise connection_limit_error()
            return super().replace_simulation_if_status(
                record, expected_status=expected_status
            )

        def replace_report(self, record):
            if record.status is ComparisonRunLifecycleStatus.FAILED:
                ordered_events.append(("failure_write", record.error_code))
                raise connection_limit_error()
            return super().replace_report(record)

    store = UnwritableFailureStore()
    invoker = TimeoutInvoker()

    with pytest.raises(RuntimeError, match="Stage 12 report coordination failed"):
        coordinate_report(
            report_execution_input().model_dump(mode="json"),
            invocation_context().model_dump(mode="json"),
            parent_record().model_dump(mode="json"),
            application_name=invocation_context().modal_application,
            coordinator_invocation_id="coordinator-1",
            store=store,
            artifacts=FakeArtifacts(),
            invoker=invoker,
            aggregator=lambda **_: aggregate_payload({"must": "not run"}),
            runtime=TelemetryRecordingRuntime(ordered_events),
            wait_timeout_seconds=0,
        )

    original_report, *failure_writes, persistence_report = ordered_events
    event_kind, (message, attributes, reported_error) = original_report
    assert event_kind == "telemetry"
    assert message == "Stage 12 execution failed"
    assert attributes == {"stage12_failure_scope": "child_wait_timeout"}
    assert reported_error.detail.error_code == "simulation_wait_timeout"
    # Both cancellation records and the report record were attempted after
    # the closed window had already been reported.
    assert failure_writes == [
        ("failure_write", "simulation_cancelled"),
        ("failure_write", "simulation_cancelled"),
        ("failure_write", "simulation_wait_timeout"),
    ]
    assert persistence_report[1][0] == (
        "Stage 12 failure record could not be persisted"
    )
    assert store.parent.status is ComparisonRunLifecycleStatus.RUNNING
