"""A report run's coordinator waits for a simulation run another call holds.

Identical requests share simulation runs, so a report run may name a run that
another report run's worker, or an earlier attempt's worker, is calculating.
The coordinator never cancels or releases that worker: it waits for the call
holding the run, then re-reads the run. A run that succeeded is read from its
stored file, as a reused run is; one that failed fails the report run with its
own failure; one whose holder ended without recording an outcome is failed
while it still records that call, so every coordinator waiting for it stops
waiting. A report run that fails leaves the runs it names to their workers.
"""

from __future__ import annotations

from uuid import UUID

import pytest
from policyengine_simulation_executor import spm as runtime_spm
from policyengine_simulation_executor.stage12_runtime.run_sweep import (
    ENDED_CALL_MESSAGE,
    CallState,
)
from policyengine_stage12_persistence import RunStatus
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
from stage12_fake_stores import FakeArtifacts
from stage12_runtime_support import coordinate_report
from stage12_shared_run_support import (
    OTHER_WORKER_FAILURE,
    FlakyProbes,
    HolderCall,
    HolderEnding,
    HolderInvoker,
    StoredOutputFiles,
    stored_output_uri,
)

COORDINATOR_CALL_ID = "fc-coordinator"
# The worker another report run's coordinator started on the baseline run.
OTHER_WORKER_CALL_ID = "fc-other-report-baseline-worker"


@pytest.fixture(autouse=True)
def _no_spm_measure(monkeypatch):
    """Resolve every simulation to no SPM measure, so a stored file needs no receipt."""
    monkeypatch.setattr(runtime_spm, "normalize_runtime_spm", lambda _params: None)


def _held_by_another_call(report_runs, run_id, call_id, *, status=RunStatus.RUNNING):
    run = report_runs.simulation_runs[run_id]
    run.status, run.modal_call_id = status, call_id


def _coordinate(report_runs, invoker, artifacts, files, **seams):
    context = report_run_context()
    return coordinate_report(
        report_run_input().model_dump(mode="json"),
        context.model_dump(mode="json"),
        application_name=context.modal_application,
        coordinator_invocation_id=COORDINATOR_CALL_ID,
        store=ComparisonStoreNotUsed(),
        artifacts=artifacts,
        invoker=invoker,
        aggregator=seams.pop("aggregator", economy_aggregate),
        report_runs=report_runs,
        output_files=files,
        **seams,
    )


def _baseline_holder(report_runs, files, ending, **options) -> HolderCall:
    _held_by_another_call(report_runs, BASELINE_RUN_ID, OTHER_WORKER_CALL_ID)
    return HolderCall(
        OTHER_WORKER_CALL_ID,
        report_runs=report_runs,
        simulation_run_id=BASELINE_RUN_ID,
        files=files,
        ending=ending,
        **options,
    )


def test_a_run_another_call_holds_is_waited_for_and_read_from_its_stored_file() -> None:
    report_runs, files, artifacts = (
        FakeReportRuns(),
        StoredOutputFiles(),
        FakeArtifacts(),
    )
    holder = _baseline_holder(report_runs, files, HolderEnding.SUCCEEDS)
    invoker = HolderInvoker(report_runs, artifacts, holders=[holder])
    aggregated = []

    def recording_aggregator(**inputs):
        aggregated.append(inputs)
        return economy_aggregate()

    result = _coordinate(
        report_runs, invoker, artifacts, files, aggregator=recording_aggregator
    )

    assert result == {"report_run_id": str(REPORT_RUN_ID), "status": "succeeded"}
    # Only the reform's worker is started; the holder is waited for, never
    # cancelled, and its run is written only by the holder.
    assert invoker.events == ["spawn:reform"]
    assert holder.cancelled == 0
    baseline = report_runs.simulation_runs[BASELINE_RUN_ID]
    assert baseline.writes == ["complete_simulation_run"]
    assert files.reads == [stored_output_uri(BASELINE_RUN_ID)]
    (inputs,) = aggregated
    descriptor = inputs["baseline"].descriptor
    assert descriptor.evaluation_id == REPORT_RUN_ID
    assert descriptor.artifact.uri == stored_output_uri(BASELINE_RUN_ID)


def test_a_waited_for_run_that_fails_fails_the_report_run_with_its_own_failure() -> (
    None
):
    report_runs, files, artifacts = (
        FakeReportRuns(),
        StoredOutputFiles(),
        FakeArtifacts(),
    )
    holder = _baseline_holder(report_runs, files, HolderEnding.FAILS)
    invoker = HolderInvoker(
        report_runs, artifacts, holders=[holder], held_roles=("reform",)
    )

    try:
        with pytest.raises(RuntimeError, match="coordination failed"):
            _coordinate(report_runs, invoker, artifacts, files)
    finally:
        invoker.release_held.set()
        invoker.executor.shutdown(wait=True)

    report_run = report_runs.report_runs[REPORT_RUN_ID]
    assert report_run.status is RunStatus.FAILED
    assert report_run.error_message == OTHER_WORKER_FAILURE
    assert report_runs.simulation_runs[BASELINE_RUN_ID].writes == [
        "fail_simulation_run"
    ]
    # The reform's own worker is left to finish: its output stays reusable.
    reform = report_runs.simulation_runs[REFORM_RUN_ID]
    assert reform.status is RunStatus.SUCCEEDED
    assert reform.writes == ["claim_simulation_run", "complete_simulation_run"]


@pytest.mark.parametrize(
    "status", [RunStatus.RUNNING, RunStatus.PENDING], ids=["running", "pending"]
)
def test_a_waited_for_run_whose_holder_stopped_without_an_outcome_is_failed(
    status,
) -> None:
    # A pending run under a call is one the simulation service started a
    # worker on, which never claimed it.
    report_runs, files, artifacts = (
        FakeReportRuns(),
        StoredOutputFiles(),
        FakeArtifacts(),
    )
    holder = _baseline_holder(report_runs, files, HolderEnding.STOPS)
    report_runs.simulation_runs[BASELINE_RUN_ID].status = status
    invoker = HolderInvoker(report_runs, artifacts, holders=[holder])

    with pytest.raises(RuntimeError, match="coordination failed"):
        _coordinate(report_runs, invoker, artifacts, files)

    baseline = report_runs.simulation_runs[BASELINE_RUN_ID]
    assert baseline.status is RunStatus.FAILED
    assert baseline.error_message == ENDED_CALL_MESSAGE
    assert baseline.writes == ["fail_simulation_run_held_by"]
    report_run = report_runs.report_runs[REPORT_RUN_ID]
    assert report_run.status is RunStatus.FAILED
    assert report_run.error_message == ENDED_CALL_MESSAGE


class ClaimedFirstByAnotherWorker(HolderInvoker):
    """Another coordinator's worker claims the baseline run as this one's starts."""

    def spawn(self, request):
        if request.simulation["role"] == "baseline":
            self.report_runs.claim_simulation_run(
                BASELINE_RUN_ID, modal_call_id=OTHER_WORKER_CALL_ID, claimed_at=None
            )
        return super().spawn(request)


def test_a_worker_that_finds_its_run_held_leaves_the_coordinator_waiting_for_it() -> (
    None
):
    report_runs, files, artifacts = (
        FakeReportRuns(),
        StoredOutputFiles(),
        FakeArtifacts(),
    )
    holder = HolderCall(
        OTHER_WORKER_CALL_ID,
        report_runs=report_runs,
        simulation_run_id=BASELINE_RUN_ID,
        files=files,
        ending=HolderEnding.SUCCEEDS,
    )
    invoker = ClaimedFirstByAnotherWorker(report_runs, artifacts, holders=[holder])

    result = _coordinate(report_runs, invoker, artifacts, files)

    assert result == {"report_run_id": str(REPORT_RUN_ID), "status": "succeeded"}
    assert sorted(invoker.events) == ["spawn:baseline", "spawn:reform"]
    baseline = report_runs.simulation_runs[BASELINE_RUN_ID]
    assert (baseline.status, baseline.modal_call_id) == (
        RunStatus.SUCCEEDED,
        OTHER_WORKER_CALL_ID,
    )
    assert files.reads == [stored_output_uri(BASELINE_RUN_ID)]


def test_a_closed_wait_window_leaves_the_waited_for_run_to_its_holder() -> None:
    report_runs, files, artifacts = (
        FakeReportRuns(),
        StoredOutputFiles(),
        FakeArtifacts(),
    )
    holder = _baseline_holder(
        report_runs, files, HolderEnding.SUCCEEDS, probes_before_ending=10**6
    )
    invoker = HolderInvoker(
        report_runs, artifacts, holders=[holder], held_roles=("reform",)
    )

    try:
        with pytest.raises(RuntimeError, match="coordination failed"):
            _coordinate(report_runs, invoker, artifacts, files, wait_timeout_seconds=0)
    finally:
        invoker.release_held.set()
        invoker.executor.shutdown(wait=True)

    report_run = report_runs.report_runs[REPORT_RUN_ID]
    assert report_run.status is RunStatus.FAILED
    assert report_run.error_message.startswith("simulation_wait_timeout: ")
    assert holder.cancelled == 0
    assert report_runs.simulation_runs[BASELINE_RUN_ID].writes == []
    # The reform's own worker is left to finish too.
    assert report_runs.simulation_runs[REFORM_RUN_ID].status is RunStatus.SUCCEEDED


class RefusedBaselineStart(HolderInvoker):
    """Modal refuses the call that would start the baseline's worker."""

    def spawn(self, request):
        if request.simulation["role"] == "baseline":
            msg = "Modal refused the call"
            raise ConnectionError(msg)
        return super().spawn(request)


def test_a_worker_start_refused_fails_the_report_run_and_leaves_its_run_pending() -> (
    None
):
    report_runs, files, artifacts = (
        FakeReportRuns(),
        StoredOutputFiles(),
        FakeArtifacts(),
    )
    invoker = RefusedBaselineStart(report_runs, artifacts)

    with pytest.raises(RuntimeError, match="coordination failed"):
        _coordinate(report_runs, invoker, artifacts, files)

    report_run = report_runs.report_runs[REPORT_RUN_ID]
    assert report_run.status is RunStatus.FAILED
    assert report_run.error_message.startswith("simulation_dispatch_failed: ")
    # Another report run naming it may start it; otherwise the sweep fails it.
    for run_id in (BASELINE_RUN_ID, REFORM_RUN_ID):
        simulation_run = report_runs.simulation_runs[run_id]
        assert (simulation_run.status, simulation_run.writes) == (
            RunStatus.PENDING,
            [],
        )


class FlakyOwnWorkers(HolderInvoker):
    """Start real workers whose first probe fails as a lost connection does."""

    def spawn(self, request):
        return FlakyProbes(super().spawn(request))


def test_a_failed_probe_of_a_running_holder_keeps_the_coordinator_waiting() -> None:
    report_runs, files, artifacts = (
        FakeReportRuns(),
        StoredOutputFiles(),
        FakeArtifacts(),
    )
    holder = _baseline_holder(report_runs, files, HolderEnding.SUCCEEDS)
    invoker = HolderInvoker(report_runs, artifacts, holders=[holder])
    invoker.holders[OTHER_WORKER_CALL_ID] = FlakyProbes(holder)
    asked = []

    def still_running(modal_call_id):
        asked.append(modal_call_id)
        return CallState.RUNNING

    result = _coordinate(
        report_runs, invoker, artifacts, files, call_state=still_running
    )

    assert result == {"report_run_id": str(REPORT_RUN_ID), "status": "succeeded"}
    # The failed probe made the coordinator ask Modal, which reported the call
    # running, so the run was left to its holder, which completed it.
    assert asked == [OTHER_WORKER_CALL_ID]
    assert report_runs.simulation_runs[BASELINE_RUN_ID].writes == [
        "complete_simulation_run"
    ]


def test_a_failed_probe_of_a_running_own_worker_keeps_the_coordinator_waiting() -> None:
    report_runs, files, artifacts = (
        FakeReportRuns(),
        StoredOutputFiles(),
        FakeArtifacts(),
    )
    invoker = FlakyOwnWorkers(report_runs, artifacts)

    result = _coordinate(
        report_runs,
        invoker,
        artifacts,
        files,
        call_state=lambda _modal_call_id: CallState.RUNNING,
    )

    assert result == {"report_run_id": str(REPORT_RUN_ID), "status": "succeeded"}
    for run_id in (BASELINE_RUN_ID, REFORM_RUN_ID):
        simulation_run = report_runs.simulation_runs[run_id]
        assert simulation_run.status is RunStatus.SUCCEEDED
        assert simulation_run.writes == [
            "claim_simulation_run",
            "complete_simulation_run",
        ]


def test_a_waited_for_run_must_share_the_dataset_and_region_of_the_other_run() -> None:
    # As for a reused run: the two sides of one report calculate one
    # population and geography.
    report_runs, files, artifacts = (
        FakeReportRuns(),
        StoredOutputFiles(),
        FakeArtifacts(),
    )
    holder = _baseline_holder(report_runs, files, HolderEnding.SUCCEEDS)
    other_dataset_id = UUID("00000000-0000-0000-0000-0000000000f1")
    report_runs.simulation_runs[BASELINE_RUN_ID].dataset_id = other_dataset_id
    invoker = HolderInvoker(report_runs, artifacts, holders=[holder])

    with pytest.raises(RuntimeError, match="coordination failed"):
        _coordinate(report_runs, invoker, artifacts, files)

    message = report_runs.report_runs[REPORT_RUN_ID].error_message
    assert message.startswith("reused_simulation_run_mismatch: ")
    assert f"baseline simulation run {BASELINE_RUN_ID}" in message
    assert f"dataset_id {other_dataset_id} instead of " in message
    # The run is its holder's: nothing is written to it.
    assert report_runs.simulation_runs[BASELINE_RUN_ID].writes == [
        "complete_simulation_run"
    ]


NEXT_HOLDER_CALL_ID = "fc-next-holder"


class HandedToAnotherCall(HolderCall):
    """A holder that, as it ends, leaves its run to another call."""

    def get(self, *, timeout=None):
        try:
            return super().get(timeout=timeout)
        except RuntimeError:
            run = self.report_runs.simulation_runs[self.simulation_run_id]
            run.modal_call_id = NEXT_HOLDER_CALL_ID
            raise


def test_a_waited_for_run_held_by_a_new_call_is_waited_for_under_it() -> None:
    report_runs, files, artifacts = (
        FakeReportRuns(),
        StoredOutputFiles(),
        FakeArtifacts(),
    )
    _held_by_another_call(report_runs, BASELINE_RUN_ID, OTHER_WORKER_CALL_ID)
    first = HandedToAnotherCall(
        OTHER_WORKER_CALL_ID,
        report_runs=report_runs,
        simulation_run_id=BASELINE_RUN_ID,
        files=files,
        ending=HolderEnding.STOPS,
    )
    second = HolderCall(
        NEXT_HOLDER_CALL_ID,
        report_runs=report_runs,
        simulation_run_id=BASELINE_RUN_ID,
        files=files,
        ending=HolderEnding.SUCCEEDS,
    )
    invoker = HolderInvoker(report_runs, artifacts, holders=[first, second])

    result = _coordinate(report_runs, invoker, artifacts, files)

    assert result == {"report_run_id": str(REPORT_RUN_ID), "status": "succeeded"}
    assert set(invoker.restored) == {OTHER_WORKER_CALL_ID, NEXT_HOLDER_CALL_ID}
    # The first call's end didn't fail a run the next call holds.
    assert report_runs.simulation_runs[BASELINE_RUN_ID].writes == [
        "complete_simulation_run"
    ]


class LeftUnstarted(HolderCall):
    """A holder whose run is returned to pending with no call as it ends."""

    def get(self, *, timeout=None):
        try:
            return super().get(timeout=timeout)
        except RuntimeError:
            run = self.report_runs.simulation_runs[self.simulation_run_id]
            run.status, run.modal_call_id = RunStatus.PENDING, None
            raise


def test_a_waited_for_run_left_unstarted_gets_this_coordinators_worker() -> None:
    report_runs, files, artifacts = (
        FakeReportRuns(),
        StoredOutputFiles(),
        FakeArtifacts(),
    )
    _held_by_another_call(report_runs, BASELINE_RUN_ID, OTHER_WORKER_CALL_ID)
    holder = LeftUnstarted(
        OTHER_WORKER_CALL_ID,
        report_runs=report_runs,
        simulation_run_id=BASELINE_RUN_ID,
        files=files,
        ending=HolderEnding.STOPS,
    )
    invoker = HolderInvoker(report_runs, artifacts, holders=[holder])

    result = _coordinate(report_runs, invoker, artifacts, files)

    assert result == {"report_run_id": str(REPORT_RUN_ID), "status": "succeeded"}
    assert sorted(invoker.events) == ["spawn:baseline", "spawn:reform"]
    baseline = report_runs.simulation_runs[BASELINE_RUN_ID]
    assert baseline.status is RunStatus.SUCCEEDED
    assert baseline.modal_call_id in invoker.call_ids


def test_a_forced_report_run_starts_workers_on_its_fresh_runs_and_waits_for_none() -> (
    None
):
    # C1 · 24, answer 3: a forced re-run calculates extra simulation runs that
    # no other report run joins, so both are pending with no call when its
    # coordinator starts; it needs no marker to tell it so.
    report_runs, files, artifacts = (
        FakeReportRuns(),
        StoredOutputFiles(),
        FakeArtifacts(),
    )
    invoker = HolderInvoker(report_runs, artifacts)

    result = _coordinate(report_runs, invoker, artifacts, files)

    assert result == {"report_run_id": str(REPORT_RUN_ID), "status": "succeeded"}
    assert sorted(invoker.events) == ["spawn:baseline", "spawn:reform"]
    assert invoker.restored == {}
    for run_id in (BASELINE_RUN_ID, REFORM_RUN_ID):
        assert report_runs.simulation_runs[run_id].writes == [
            "claim_simulation_run",
            "complete_simulation_run",
        ]
