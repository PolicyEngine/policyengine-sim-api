"""A report call's coordinator under pre-emption: stop the children, then resume.

Modal pre-empts a call with SIGINT, which a synchronous function receives as
``KeyboardInterrupt``, and later runs the same call again under the same call
ID. The coordinator of a report run cancels its unfinished children, lets
Modal restart it, and the restarted call starts them again and records the
report run once.
"""

from __future__ import annotations

import pytest
from policyengine_simulation_contract.stage12_execution import (
    ComparisonRunLifecycleStatus,
)
from policyengine_stage12_persistence import RunStatus
from stage12_child_call_support import ConcurrentInvoker, TimeoutInvoker
from stage12_fake_report_runs import (
    REPORT_RUN_ID,
    FakeReportRuns,
    economy_aggregate,
    report_run_context,
)
from stage12_fake_stores import FakeArtifacts, FakeStore
from stage12_runtime_support import (
    coordinate_report,
    invocation_context,
    parent_record,
    report_execution_input,
)

COORDINATOR_CALL_ID = "fc-coordinator"


class CancelledCall:
    def __init__(self, object_id):
        self.object_id = object_id
        self.cancelled = 0

    def get(self, *, timeout=None):
        msg = "a cancelled call has no result"
        raise AssertionError(msg)

    def cancel(self):
        self.cancelled += 1


class RestartInvoker(ConcurrentInvoker):
    """Run new children; restoring an earlier attempt's child only allows cancelling."""

    def __init__(self, artifacts):
        super().__init__(artifacts)
        self.restored: dict[str, CancelledCall] = {}

    def restore(self, invocation_id):
        self.events.append(f"restore:{invocation_id}")
        call = CancelledCall(invocation_id)
        self.restored[invocation_id] = call
        return call


def _preempt(_seconds):
    raise KeyboardInterrupt


def _coordinate(store, report_runs, invoker, *, context, **seams):
    return coordinate_report(
        report_execution_input().model_dump(mode="json"),
        context.model_dump(mode="json"),
        parent_record().model_dump(mode="json"),
        application_name=context.modal_application,
        coordinator_invocation_id=COORDINATOR_CALL_ID,
        store=store,
        artifacts=seams.pop("artifacts", FakeArtifacts()),
        invoker=invoker,
        aggregator=economy_aggregate,
        report_runs=report_runs,
        **seams,
    )


def _preempted_first_attempt(store, report_runs, *, context):
    invoker = TimeoutInvoker()
    calls = []
    spawn = invoker.spawn

    def keeping_spawn(request):
        call = spawn(request)
        calls.append(call)
        return call

    invoker.spawn = keeping_spawn
    with pytest.raises(KeyboardInterrupt):
        _coordinate(store, report_runs, invoker, context=context, sleep=_preempt)
    return calls


def test_preempted_coordinator_cancels_its_children_and_records_no_failure() -> None:
    store = FakeStore()
    report_runs = FakeReportRuns()

    calls = _preempted_first_attempt(store, report_runs, context=report_run_context())

    assert [call.cancelled for call in calls] == [1, 1]
    run = report_runs.report_runs[REPORT_RUN_ID]
    assert run.status is RunStatus.RUNNING
    assert run.writes == ["claim_report_run"]
    assert store.parent.status is ComparisonRunLifecycleStatus.RUNNING
    assert all(
        child.status is ComparisonRunLifecycleStatus.RUNNING
        for child in store.children.values()
    )
    assert all(
        simulation_run.status is RunStatus.PENDING
        for simulation_run in report_runs.simulation_runs.values()
    )


def test_restarted_coordinator_resumes_its_claim_and_starts_its_children_again() -> (
    None
):
    store = FakeStore()
    report_runs = FakeReportRuns()
    _preempted_first_attempt(store, report_runs, context=report_run_context())
    artifacts = FakeArtifacts()
    invoker = RestartInvoker(artifacts)

    result = _coordinate(
        store, report_runs, invoker, context=report_run_context(), artifacts=artifacts
    )

    assert result["evaluation_id"] == str(report_execution_input().evaluation_id)
    assert invoker.events == [
        "restore:call-baseline",
        "spawn:baseline",
        "restore:call-reform",
        "spawn:reform",
    ]
    assert all(call.cancelled == 1 for call in invoker.restored.values())
    run = report_runs.report_runs[REPORT_RUN_ID]
    assert run.status is RunStatus.SUCCEEDED
    assert run.writes == ["claim_report_run", "claim_report_run", "complete_report_run"]
    assert store.parent.status is ComparisonRunLifecycleStatus.SUCCEEDED


def _preempted_around_completion(report_runs, *, after_writing):
    """Make the report run's completion pre-empt the call, before or after writing."""
    complete = report_runs.complete_report_run

    def preempted_completion(*args, **kwargs):
        if after_writing:
            complete(*args, **kwargs)
        raise KeyboardInterrupt

    report_runs.complete_report_run = preempted_completion
    return complete


def _first_attempt_preempted_at_completion(store, report_runs, *, after_writing):
    complete = _preempted_around_completion(report_runs, after_writing=after_writing)
    artifacts = FakeArtifacts()
    with pytest.raises(KeyboardInterrupt):
        _coordinate(
            store,
            report_runs,
            ConcurrentInvoker(artifacts),
            context=report_run_context(),
            artifacts=artifacts,
        )
    report_runs.complete_report_run = complete
    return artifacts


def test_a_call_restarted_between_its_two_success_writes_completes_the_run() -> None:
    store = FakeStore()
    report_runs = FakeReportRuns()
    artifacts = _first_attempt_preempted_at_completion(
        store, report_runs, after_writing=False
    )
    restart_invoker = RestartInvoker(artifacts)

    result = _coordinate(
        store,
        report_runs,
        restart_invoker,
        context=report_run_context(),
        artifacts=artifacts,
    )

    assert result == {
        "deduplicated": True,
        "report_run_id": str(REPORT_RUN_ID),
        "status": "succeeded",
    }
    # Nothing is calculated again: the result comes from the stored aggregate.
    assert restart_invoker.events == []
    run = report_runs.report_runs[REPORT_RUN_ID]
    assert run.status is RunStatus.SUCCEEDED
    rows = {rows.table.name: rows.rows for rows in run.result.rows}
    assert {row["variable_name"] for row in rows["budget_summary"]} >= {"household_tax"}
    assert store.parent.status is ComparisonRunLifecycleStatus.SUCCEEDED


def test_a_call_restarted_after_its_report_run_succeeded_records_nothing_again() -> (
    None
):
    store = FakeStore()
    report_runs = FakeReportRuns()
    _first_attempt_preempted_at_completion(store, report_runs, after_writing=True)
    restart_invoker = RestartInvoker(FakeArtifacts())

    result = _coordinate(
        store, report_runs, restart_invoker, context=report_run_context()
    )

    assert result == {
        "deduplicated": True,
        "report_run_id": str(REPORT_RUN_ID),
        "status": "succeeded",
    }
    assert restart_invoker.events == []
    run = report_runs.report_runs[REPORT_RUN_ID]
    assert run.writes == ["claim_report_run", "complete_report_run", "claim_report_run"]
    # Both records agree: the Stage 12 report succeeded before the report run.
    assert store.parent.status is ComparisonRunLifecycleStatus.SUCCEEDED


class InputCancelledForTest(BaseException):
    """Stands in for Modal's InputCancellation, a BaseException but not an Exception."""


def _cancel(_seconds):
    raise InputCancelledForTest


def test_a_cancelled_coordinator_records_its_report_run_and_children_failed() -> None:
    # Modal does not run a cancelled call again, so nothing would ever finish
    # these runs if the coordinator left them running.
    store = FakeStore()
    report_runs = FakeReportRuns()

    with pytest.raises(InputCancelledForTest):
        _coordinate(
            store,
            report_runs,
            TimeoutInvoker(),
            context=report_run_context(),
            sleep=_cancel,
        )

    run = report_runs.report_runs[REPORT_RUN_ID]
    assert run.status is RunStatus.FAILED
    assert run.error_message.startswith("report_cancelled: ")
    assert store.parent.status is ComparisonRunLifecycleStatus.FAILED
    assert all(
        child.status is ComparisonRunLifecycleStatus.FAILED
        for child in store.children.values()
    )
    assert all(
        simulation_run.status is RunStatus.FAILED
        for simulation_run in report_runs.simulation_runs.values()
    )


@pytest.mark.parametrize(
    ("interruption", "expected_status"),
    [
        # Modal runs a pre-empted call again, which resumes the claim.
        (KeyboardInterrupt, RunStatus.RUNNING),
        # Modal never runs a cancelled call again.
        (InputCancelledForTest, RunStatus.FAILED),
    ],
)
def test_a_coordinator_stopped_while_claiming_its_report(
    interruption, expected_status
) -> None:
    store = FakeStore()
    report_runs = FakeReportRuns()

    def stopped(_record):
        raise interruption

    store.create_or_resolve_report = stopped

    with pytest.raises(interruption):
        _coordinate(
            store,
            report_runs,
            RestartInvoker(FakeArtifacts()),
            context=report_run_context(),
        )

    run = report_runs.report_runs[REPORT_RUN_ID]
    assert run.status is expected_status
    if expected_status is RunStatus.FAILED:
        assert run.error_message.startswith("report_cancelled: ")


def test_a_preempted_comparison_run_behaves_as_before() -> None:
    store = FakeStore()
    report_runs = FakeReportRuns()

    calls = _preempted_first_attempt(store, report_runs, context=invocation_context())
    restart_invoker = RestartInvoker(FakeArtifacts())
    result = _coordinate(
        store, report_runs, restart_invoker, context=invocation_context()
    )

    assert [call.cancelled for call in calls] == [0, 0]
    assert result["deduplicated"] is True
    assert restart_invoker.events == []
    assert report_runs.events == []
