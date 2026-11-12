"""A report run's coordinator under pre-emption and cancellation, on the run tables.

Modal pre-empts a call with SIGINT, which a synchronous function receives as
``KeyboardInterrupt``, and later runs the same call again under the same call
ID. The coordinator of a report run cancels its unfinished workers and
records nothing; the restarted call resumes its claim, releases each
simulation run an earlier worker still holds, and starts a worker on it
again. Modal never runs a cancelled call again, so a cancelled coordinator
fails its report run and the simulation runs it left unfinished.
"""

from __future__ import annotations

import pytest
from policyengine_stage12_persistence import RunStatus
from stage12_child_call_support import TimeoutInvoker
from stage12_fake_report_runs import (
    BASELINE_RUN_ID,
    REFORM_RUN_ID,
    REPORT_RUN_ID,
    ComparisonStoreNotUsed,
    FakeReportRuns,
    economy_aggregate,
    report_run_context,
    report_run_input,
)
from stage12_fake_stores import FakeArtifacts, FakeStore
from stage12_run_worker_support import RunWorkerInvoker
from stage12_runtime_support import (
    coordinate_report,
    invocation_context,
    parent_record,
    report_execution_input,
)

COORDINATOR_CALL_ID = "fc-coordinator"


class InputCancelledForTest(BaseException):
    """Stands in for Modal's InputCancellation, a BaseException but not an Exception."""


def _preempt(_seconds):
    raise KeyboardInterrupt


def _cancel(_seconds):
    raise InputCancelledForTest


def _coordinate(report_runs, invoker, artifacts=None, **seams):
    context = report_run_context()
    return coordinate_report(
        report_run_input().model_dump(mode="json"),
        context.model_dump(mode="json"),
        application_name=context.modal_application,
        coordinator_invocation_id=COORDINATOR_CALL_ID,
        store=ComparisonStoreNotUsed(),
        artifacts=artifacts or FakeArtifacts(),
        invoker=invoker,
        aggregator=economy_aggregate,
        report_runs=report_runs,
        **seams,
    )


def _claimed_by_earlier_workers(report_runs):
    """Leave both simulation runs as a pre-empted attempt's workers claimed them."""
    report_run = report_runs.report_runs[REPORT_RUN_ID]
    report_run.status = RunStatus.RUNNING
    report_run.modal_call_id = COORDINATOR_CALL_ID
    for role, run_id in (("baseline", BASELINE_RUN_ID), ("reform", REFORM_RUN_ID)):
        simulation_run = report_runs.simulation_runs[run_id]
        simulation_run.status = RunStatus.RUNNING
        simulation_run.modal_call_id = f"fc-{role}-earlier"


def test_a_preempted_coordinator_cancels_its_workers_and_records_nothing() -> None:
    report_runs = FakeReportRuns()
    invoker = TimeoutInvoker()
    calls = []
    spawn = invoker.spawn

    def keeping_spawn(request):
        call = spawn(request)
        calls.append(call)
        return call

    invoker.spawn = keeping_spawn

    with pytest.raises(KeyboardInterrupt):
        _coordinate(report_runs, invoker, sleep=_preempt)

    assert [call.cancelled for call in calls] == [1, 1]
    run = report_runs.report_runs[REPORT_RUN_ID]
    assert run.status is RunStatus.RUNNING
    assert run.writes == ["claim_report_run"]
    # The coordinator writes nothing to its simulation runs; the restarted
    # call starts a worker on each again.
    assert all(
        not simulation_run.writes
        for simulation_run in report_runs.simulation_runs.values()
    )


def test_a_restart_releases_each_earlier_workers_run_and_starts_it_again() -> None:
    report_runs = FakeReportRuns()
    _claimed_by_earlier_workers(report_runs)
    artifacts = FakeArtifacts()
    invoker = RunWorkerInvoker(report_runs, artifacts)

    result = _coordinate(report_runs, invoker, artifacts)

    assert result == {"report_run_id": str(REPORT_RUN_ID), "status": "succeeded"}
    # Each earlier worker was asked to stop before its run was released.
    assert sorted(invoker.restored) == ["fc-baseline-earlier", "fc-reform-earlier"]
    assert all(call.cancelled == 1 for call in invoker.restored.values())
    for run_id in (BASELINE_RUN_ID, REFORM_RUN_ID):
        simulation_run = report_runs.simulation_runs[run_id]
        assert simulation_run.status is RunStatus.SUCCEEDED
        assert simulation_run.modal_call_id in invoker.call_ids
        assert simulation_run.writes == [
            "release_simulation_run",
            "claim_simulation_run",
            "complete_simulation_run",
        ]
    run = report_runs.report_runs[REPORT_RUN_ID]
    assert run.status is RunStatus.SUCCEEDED
    assert run.writes == ["claim_report_run", "complete_report_run"]


def test_a_restart_starts_a_worker_its_earlier_attempt_never_started() -> None:
    report_runs = FakeReportRuns()
    _claimed_by_earlier_workers(report_runs)
    # The earlier attempt's reform worker never claimed its run.
    reform = report_runs.simulation_runs[REFORM_RUN_ID]
    reform.status, reform.modal_call_id = RunStatus.PENDING, None
    artifacts = FakeArtifacts()
    invoker = RunWorkerInvoker(report_runs, artifacts)

    _coordinate(report_runs, invoker, artifacts)

    assert sorted(invoker.restored) == ["fc-baseline-earlier"]
    assert reform.writes == ["claim_simulation_run", "complete_simulation_run"]
    assert reform.status is RunStatus.SUCCEEDED


def test_an_earlier_worker_that_cannot_be_cancelled_still_loses_its_run() -> None:
    report_runs = FakeReportRuns()
    _claimed_by_earlier_workers(report_runs)
    artifacts = FakeArtifacts()
    invoker = RunWorkerInvoker(report_runs, artifacts)
    restore = invoker.restore

    def uncancellable(invocation_id):
        call = restore(invocation_id)

        def refused():
            msg = "Modal could not cancel the call"
            raise ConnectionError(msg)

        call.cancel = refused
        return call

    invoker.restore = uncancellable

    _coordinate(report_runs, invoker, artifacts)

    # Released, the run takes no write from the earlier worker's call.
    assert all(
        simulation_run.status is RunStatus.SUCCEEDED
        and simulation_run.modal_call_id in invoker.call_ids
        for simulation_run in report_runs.simulation_runs.values()
    )


def test_a_cancelled_coordinator_fails_its_report_run_and_unfinished_runs() -> None:
    report_runs = FakeReportRuns()
    artifacts = FakeArtifacts()
    invoker = RunWorkerInvoker(
        report_runs, artifacts, held_roles=("baseline", "reform")
    )

    try:
        with pytest.raises(InputCancelledForTest):
            _coordinate(report_runs, invoker, artifacts, sleep=_cancel)
    finally:
        invoker.release_held.set()
        invoker.executor.shutdown(wait=True)

    run = report_runs.report_runs[REPORT_RUN_ID]
    assert run.status is RunStatus.FAILED
    assert run.error_message.startswith("report_cancelled: ")
    for simulation_run in report_runs.simulation_runs.values():
        assert simulation_run.status is RunStatus.FAILED
        assert simulation_run.error_message == (
            "simulation_cancelled: Cancelled after the coordinator's call was "
            "cancelled."
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
def test_a_coordinator_stopped_while_preparing_its_report(
    interruption, expected_status
) -> None:
    report_runs = FakeReportRuns()

    def stopped(_report):
        raise interruption

    with pytest.raises(interruption):
        _coordinate(
            report_runs,
            RunWorkerInvoker(report_runs, FakeArtifacts()),
            output_plan_resolver=stopped,
        )

    run = report_runs.report_runs[REPORT_RUN_ID]
    assert run.status is expected_status
    if expected_status is RunStatus.FAILED:
        assert run.error_message.startswith("report_cancelled: ")
        assert all(
            simulation_run.status is RunStatus.FAILED
            for simulation_run in report_runs.simulation_runs.values()
        )
    else:
        assert all(
            not simulation_run.writes
            for simulation_run in report_runs.simulation_runs.values()
        )


def test_a_cancellation_as_the_report_run_claim_commits_fails_the_run() -> None:
    report_runs = FakeReportRuns()
    claim = report_runs.claim_report_run

    def cancelled_after_commit(*args, **kwargs):
        claim(*args, **kwargs)
        raise InputCancelledForTest

    report_runs.claim_report_run = cancelled_after_commit

    with pytest.raises(InputCancelledForTest):
        _coordinate(report_runs, RunWorkerInvoker(report_runs, FakeArtifacts()))

    run = report_runs.report_runs[REPORT_RUN_ID]
    assert run.status is RunStatus.FAILED
    assert run.error_message.startswith("report_cancelled: ")


def test_a_preemption_as_the_report_run_claim_commits_records_nothing_else() -> None:
    report_runs = FakeReportRuns()
    claim = report_runs.claim_report_run

    def preempted_after_commit(*args, **kwargs):
        claim(*args, **kwargs)
        raise KeyboardInterrupt

    report_runs.claim_report_run = preempted_after_commit

    with pytest.raises(KeyboardInterrupt):
        _coordinate(report_runs, RunWorkerInvoker(report_runs, FakeArtifacts()))

    run = report_runs.report_runs[REPORT_RUN_ID]
    assert (run.status, run.writes) == (RunStatus.RUNNING, ["claim_report_run"])


def test_a_restarted_call_after_its_report_run_succeeded_records_nothing_again() -> (
    None
):
    report_runs = FakeReportRuns()
    run = report_runs.report_runs[REPORT_RUN_ID]
    run.status, run.modal_call_id = RunStatus.SUCCEEDED, COORDINATOR_CALL_ID
    invoker = RunWorkerInvoker(report_runs, FakeArtifacts())

    result = _coordinate(report_runs, invoker)

    assert result == {
        "deduplicated": True,
        "report_run_id": str(REPORT_RUN_ID),
        "status": "succeeded",
    }
    assert invoker.call_ids == []
    assert run.writes == ["claim_report_run"]


def test_a_preempted_comparison_run_behaves_as_before() -> None:
    store = FakeStore()
    report_runs = FakeReportRuns()
    context = invocation_context()

    with pytest.raises(KeyboardInterrupt):
        coordinate_report(
            report_execution_input().model_dump(mode="json"),
            context.model_dump(mode="json"),
            parent_record().model_dump(mode="json"),
            application_name=context.modal_application,
            coordinator_invocation_id=COORDINATOR_CALL_ID,
            store=store,
            artifacts=FakeArtifacts(),
            invoker=TimeoutInvoker(),
            aggregator=economy_aggregate,
            report_runs=report_runs,
            sleep=_preempt,
        )

    assert report_runs.events == []
    assert store.parent is not None
