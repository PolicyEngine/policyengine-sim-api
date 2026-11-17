"""A report run's coordinator under pre-emption and cancellation, on the run tables.

Modal pre-empts a call with SIGINT, which a synchronous function receives as
``KeyboardInterrupt``, and later runs the same call again under the same call
ID. A pre-empted coordinator records nothing and cancels none of its workers:
other report runs may wait for the simulation runs they hold. The restarted
call resumes its claim and waits for each run an earlier worker still holds,
reading it from its stored file once it succeeds, as it would wait for
another report run's worker. Modal never runs a cancelled call again, so a
cancelled coordinator fails its report run, and leaves its workers to finish.
"""

from __future__ import annotations

import pytest
from policyengine_simulation_executor import spm as runtime_spm
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
from stage12_run_worker_support import EarlierCall, RunWorkerInvoker
from stage12_runtime_support import (
    coordinate_report,
    invocation_context,
    parent_record,
    report_execution_input,
)
from stage12_shared_run_support import (
    HolderCall,
    HolderEnding,
    HolderInvoker,
    StoredOutputFiles,
    stored_output_uri,
)

COORDINATOR_CALL_ID = "fc-coordinator"
EARLIER_CALL_IDS = {"baseline": "fc-baseline-earlier", "reform": "fc-reform-earlier"}
SIMULATION_RUN_IDS = {"baseline": BASELINE_RUN_ID, "reform": REFORM_RUN_ID}


class InputCancelledForTest(BaseException):
    """Stands in for Modal's InputCancellation, a BaseException but not an Exception."""


def _preempt(_seconds):
    raise KeyboardInterrupt


def _cancel(_seconds):
    raise InputCancelledForTest


@pytest.fixture(autouse=True)
def _no_spm_measure(monkeypatch):
    """Resolve every simulation to no SPM measure, so a stored file needs no receipt."""
    monkeypatch.setattr(runtime_spm, "normalize_runtime_spm", lambda _params: None)


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
        aggregator=seams.pop("aggregator", economy_aggregate),
        report_runs=report_runs,
        **seams,
    )


def _claimed_by_earlier_workers(report_runs, roles=("baseline", "reform")):
    """Leave the runs of ``roles`` as a pre-empted attempt's workers claimed them."""
    report_run = report_runs.report_runs[REPORT_RUN_ID]
    report_run.status = RunStatus.RUNNING
    report_run.modal_call_id = COORDINATOR_CALL_ID
    for role in roles:
        simulation_run = report_runs.simulation_runs[SIMULATION_RUN_IDS[role]]
        simulation_run.status = RunStatus.RUNNING
        simulation_run.modal_call_id = EARLIER_CALL_IDS[role]


def _earlier_workers(report_runs, files, roles=("baseline", "reform")):
    """Return the earlier attempt's workers, still running, which succeed."""
    _claimed_by_earlier_workers(report_runs, roles)
    return [
        HolderCall(
            EARLIER_CALL_IDS[role],
            report_runs=report_runs,
            simulation_run_id=SIMULATION_RUN_IDS[role],
            files=files,
            ending=HolderEnding.SUCCEEDS,
        )
        for role in roles
    ]


def test_a_preempted_coordinator_cancels_no_worker_and_records_nothing() -> None:
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

    # Its workers keep calculating; the restarted call waits for them.
    assert [call.cancelled for call in calls] == [0, 0]
    run = report_runs.report_runs[REPORT_RUN_ID]
    assert run.status is RunStatus.RUNNING
    assert run.writes == ["claim_report_run"]
    assert all(
        not simulation_run.writes
        for simulation_run in report_runs.simulation_runs.values()
    )


def test_a_restart_waits_for_each_earlier_worker_and_reads_its_output() -> None:
    report_runs, files, artifacts = (
        FakeReportRuns(),
        StoredOutputFiles(),
        FakeArtifacts(),
    )
    earlier_workers = _earlier_workers(report_runs, files)
    invoker = HolderInvoker(report_runs, artifacts, holders=earlier_workers)

    result = _coordinate(report_runs, invoker, artifacts, output_files=files)

    assert result == {"report_run_id": str(REPORT_RUN_ID), "status": "succeeded"}
    # No worker is started, cancelled or released: the earlier ones finish.
    assert invoker.events == []
    assert all(worker.cancelled == 0 for worker in earlier_workers)
    for role, run_id in SIMULATION_RUN_IDS.items():
        simulation_run = report_runs.simulation_runs[run_id]
        assert (simulation_run.status, simulation_run.modal_call_id) == (
            RunStatus.SUCCEEDED,
            EARLIER_CALL_IDS[role],
        )
        assert simulation_run.writes == ["complete_simulation_run"]
    assert sorted(files.reads) == sorted(
        stored_output_uri(run_id) for run_id in SIMULATION_RUN_IDS.values()
    )
    run = report_runs.report_runs[REPORT_RUN_ID]
    assert run.writes == ["claim_report_run", "complete_report_run"]


def test_a_restart_starts_a_worker_its_earlier_attempt_never_started() -> None:
    report_runs, files, artifacts = (
        FakeReportRuns(),
        StoredOutputFiles(),
        FakeArtifacts(),
    )
    # The earlier attempt's reform worker never claimed its run.
    earlier_workers = _earlier_workers(report_runs, files, roles=("baseline",))
    invoker = HolderInvoker(report_runs, artifacts, holders=earlier_workers)

    _coordinate(report_runs, invoker, artifacts, output_files=files)

    assert sorted(invoker.restored) == ["fc-baseline-earlier"]
    assert invoker.events == ["spawn:reform"]
    reform = report_runs.simulation_runs[REFORM_RUN_ID]
    assert reform.writes == ["claim_simulation_run", "complete_simulation_run"]
    assert reform.status is RunStatus.SUCCEEDED


def test_an_earlier_workers_finished_run_is_not_written_when_the_report_fails() -> None:
    report_runs, files, artifacts = (
        FakeReportRuns(),
        StoredOutputFiles(),
        FakeArtifacts(),
    )
    earlier_workers = _earlier_workers(report_runs, files, roles=("reform",))
    invoker = HolderInvoker(report_runs, artifacts, holders=earlier_workers)

    def failing_aggregator(**_values):
        msg = "aggregation failed"
        raise ValueError(msg)

    with pytest.raises(RuntimeError, match="coordination failed"):
        _coordinate(
            report_runs,
            invoker,
            artifacts,
            output_files=files,
            aggregator=failing_aggregator,
        )

    # The report failed in its aggregation, after reading the stored output.
    assert "in stage12_aggregation" in (
        report_runs.report_runs[REPORT_RUN_ID].error_message
    )
    reform = report_runs.simulation_runs[REFORM_RUN_ID]
    assert reform.status is RunStatus.SUCCEEDED
    # Other report runs may reuse it, so the failing one writes nothing to it.
    assert reform.writes == ["complete_simulation_run"]


STALE_CALL_ID = "fc-baseline-stale"


class UncancelledEarlierWorker(HolderInvoker):
    """The earlier attempt's baseline worker claims its run first, then calculates it.

    It claims as the restarted coordinator's new worker starts, so that worker
    finds the run held by another call; the earlier worker then calculates it.
    """

    def __init__(self, report_runs, artifacts, **options):
        super().__init__(report_runs, artifacts, **options)
        self.earlier_request = None

    def spawn(self, request):
        if request.simulation["role"] == "baseline" and self.earlier_request is None:
            self.earlier_request = request
            self.report_runs.claim_simulation_run(
                BASELINE_RUN_ID, modal_call_id=STALE_CALL_ID, claimed_at=None
            )
        return super().spawn(request)

    def restore(self, invocation_id):
        if invocation_id == STALE_CALL_ID:
            call = self.worker_call(self.earlier_request, STALE_CALL_ID)
            self.restored[invocation_id] = call
            return call
        return super().restore(invocation_id)


def test_a_restart_waits_for_an_earlier_worker_that_holds_its_run() -> None:
    report_runs, artifacts = FakeReportRuns(), FakeArtifacts()
    # The stale worker writes its output where the coordinator's workers do.
    files = StoredOutputFiles(fallback=artifacts)
    # The earlier attempt started the baseline worker, but it hadn't claimed
    # its run; the earlier reform worker is still calculating.
    earlier_workers = _earlier_workers(report_runs, files, roles=("reform",))
    report_run = report_runs.report_runs[REPORT_RUN_ID]
    report_run.status, report_run.modal_call_id = RunStatus.RUNNING, COORDINATOR_CALL_ID
    invoker = UncancelledEarlierWorker(report_runs, artifacts, holders=earlier_workers)

    result = _coordinate(report_runs, invoker, artifacts, output_files=files)

    assert result == {"report_run_id": str(REPORT_RUN_ID), "status": "succeeded"}
    # The new worker found the run held, so the coordinator waited for the
    # earlier worker, which completed it.
    assert STALE_CALL_ID in invoker.restored
    baseline = report_runs.simulation_runs[BASELINE_RUN_ID]
    assert (baseline.status, baseline.modal_call_id) == (
        RunStatus.SUCCEEDED,
        STALE_CALL_ID,
    )
    assert report_runs.report_runs[REPORT_RUN_ID].status is RunStatus.SUCCEEDED


def test_a_cancelled_coordinator_fails_its_report_run_and_leaves_its_workers() -> None:
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
    # Other report runs may name its simulation runs, so its workers finish.
    for simulation_run in report_runs.simulation_runs.values():
        assert simulation_run.status is RunStatus.SUCCEEDED
        assert simulation_run.writes == [
            "claim_simulation_run",
            "complete_simulation_run",
        ]


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
    # Either way its simulation runs are left as they were.
    assert all(
        not simulation_run.writes
        for simulation_run in report_runs.simulation_runs.values()
    )


class StartedWorkerCalls(TimeoutInvoker):
    """Start workers that never finish; an earlier attempt's worker only cancels."""

    def __init__(self, events):
        super().__init__()
        self.calls = []
        self.run_events = events

    def spawn(self, request):
        call = super().spawn(request)
        self.calls.append(call)
        return call

    def restore(self, invocation_id):
        return EarlierCall(invocation_id, self.run_events)


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

    invoker = StartedWorkerCalls(report_runs.events)

    with pytest.raises(KeyboardInterrupt):
        coordinate_report(
            report_execution_input().model_dump(mode="json"),
            context.model_dump(mode="json"),
            parent_record().model_dump(mode="json"),
            application_name=context.modal_application,
            coordinator_invocation_id=COORDINATOR_CALL_ID,
            store=store,
            artifacts=FakeArtifacts(),
            invoker=invoker,
            aggregator=economy_aggregate,
            report_runs=report_runs,
            sleep=_preempt,
        )

    assert report_runs.events == []
    assert store.parent is not None
    # A comparison run's children keep running: its restarted call
    # reattaches to them.
    assert [call.cancelled for call in invoker.calls] == [0, 0]
