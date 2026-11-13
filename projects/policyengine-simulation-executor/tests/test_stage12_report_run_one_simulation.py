"""A report run naming one simulation run on both sides calculates it once.

A report whose baseline and reform have the same policy and dynamic names one
simulation, and one run of it, as both its baseline and its reform run. The
coordinator starts one worker for that run, or reads its stored output once,
and aggregates that one output as both sides.
"""

from __future__ import annotations

from hashlib import sha256

import pytest
from policyengine_simulation_contract.stage12_execution import (
    ReportRunIds,
    SimulationRole,
)
from policyengine_simulation_executor import spm as runtime_spm
from policyengine_simulation_executor.stage12_artifacts import (
    serialize_simulation_frames,
)
from policyengine_stage12_persistence import RunStatus
from stage12_fake_report_runs import (
    BASELINE_RUN_ID,
    REPORT_RUN_ID,
    ComparisonStoreNotUsed,
    FakeReportRuns,
    economy_aggregate,
    report_run_context,
    report_run_input,
    report_run_simulation,
)
from stage12_fake_stores import FakeArtifacts, simulation_frames
from stage12_run_worker_support import RunWorkerInvoker
from stage12_runtime_support import coordinate_report
from stage12_shared_run_support import (
    HolderCall,
    HolderEnding,
    HolderInvoker,
    StoredOutputFiles,
    stored_output_uri,
)

COORDINATOR_CALL_ID = "fc-coordinator"
STORED_OUTPUT_URI = f"gs://detailed-save-outputs/simulations/{BASELINE_RUN_ID}.parquet"
ONE_RUN_ON_BOTH_SIDES = ReportRunIds(
    report_run_id=REPORT_RUN_ID,
    baseline_simulation_run_id=BASELINE_RUN_ID,
    reform_simulation_run_id=BASELINE_RUN_ID,
)


def _one_simulation_report():
    """Return the report comparing the baseline simulation with itself."""
    baseline = report_run_simulation(SimulationRole.BASELINE)
    return report_run_input().model_copy(
        update={
            "baseline": baseline,
            "reform": baseline.model_copy(update={"role": SimulationRole.REFORM}),
        }
    )


class FakeOutputFiles:
    """Stored output files by URI, recording each read."""

    def __init__(self) -> None:
        self.payloads: dict[str, bytes] = {}
        self.reads: list[str] = []

    def read(self, uri: str) -> bytes:
        self.reads.append(uri)
        if uri not in self.payloads:
            raise FileNotFoundError(uri)
        return self.payloads[uri]


@pytest.fixture(autouse=True)
def _no_spm_measure(monkeypatch):
    """Resolve every simulation to no SPM measure, so a file needs no receipt."""
    monkeypatch.setattr(runtime_spm, "normalize_runtime_spm", lambda _params: None)


def _succeeded_with_output(report_runs, files: FakeOutputFiles) -> None:
    """Mark the baseline run succeeded with a stored output file."""
    payload, _row_identity = serialize_simulation_frames(simulation_frames())
    files.payloads[STORED_OUTPUT_URI] = payload
    run = report_runs.simulation_runs[BASELINE_RUN_ID]
    run.status = RunStatus.SUCCEEDED
    run.output_uri = STORED_OUTPUT_URI
    run.output_sha256 = sha256(payload).hexdigest()


def _report_runs() -> FakeReportRuns:
    """Return run tables whose report run names the baseline run on both sides."""
    report_runs = FakeReportRuns()
    report_runs.simulation_run_pairs[REPORT_RUN_ID] = (BASELINE_RUN_ID, BASELINE_RUN_ID)
    return report_runs


def _coordinate(report_runs, invoker, artifacts, files, aggregated):
    context = report_run_context().model_copy(
        update={"report_run": ONE_RUN_ON_BOTH_SIDES}
    )

    def recording_aggregator(**inputs):
        aggregated.append(inputs)
        return economy_aggregate()

    return coordinate_report(
        _one_simulation_report().model_dump(mode="json"),
        context.model_dump(mode="json"),
        application_name=context.modal_application,
        coordinator_invocation_id=COORDINATOR_CALL_ID,
        store=ComparisonStoreNotUsed(),
        artifacts=artifacts,
        invoker=invoker,
        aggregator=recording_aggregator,
        report_runs=report_runs,
        output_files=files,
    )


def _assert_one_output_on_both_sides(aggregated) -> None:
    (inputs,) = aggregated
    baseline, reform = inputs["baseline"], inputs["reform"]
    assert baseline.descriptor.role is SimulationRole.BASELINE
    assert reform.descriptor.role is SimulationRole.REFORM
    assert baseline.descriptor.artifact == reform.descriptor.artifact
    assert baseline.descriptor.simulation_execution_id == BASELINE_RUN_ID
    assert reform.descriptor.simulation_execution_id == BASELINE_RUN_ID
    assert baseline.frames["household"].equals(reform.frames["household"])


def test_one_pending_run_on_both_sides_is_calculated_by_one_worker() -> None:
    report_runs = _report_runs()
    artifacts = FakeArtifacts()
    invoker = RunWorkerInvoker(report_runs, artifacts)
    aggregated = []

    result = _coordinate(report_runs, invoker, artifacts, FakeOutputFiles(), aggregated)

    assert result == {"report_run_id": str(REPORT_RUN_ID), "status": "succeeded"}
    assert invoker.events == ["spawn:baseline"]
    simulation_run = report_runs.simulation_runs[BASELINE_RUN_ID]
    assert simulation_run.status is RunStatus.SUCCEEDED
    assert simulation_run.writes == ["claim_simulation_run", "complete_simulation_run"]
    _assert_one_output_on_both_sides(aggregated)


def test_one_succeeded_run_on_both_sides_is_read_once() -> None:
    report_runs = _report_runs()
    files = FakeOutputFiles()
    _succeeded_with_output(report_runs, files)
    artifacts = FakeArtifacts()
    invoker = RunWorkerInvoker(report_runs, artifacts)
    aggregated = []

    result = _coordinate(report_runs, invoker, artifacts, files, aggregated)

    assert result == {"report_run_id": str(REPORT_RUN_ID), "status": "succeeded"}
    assert invoker.events == []
    assert files.reads == [STORED_OUTPUT_URI]
    assert report_runs.simulation_runs[BASELINE_RUN_ID].writes == []
    _assert_one_output_on_both_sides(aggregated)


def test_one_run_on_both_sides_whose_worker_fails_fails_the_report_run() -> None:
    report_runs = _report_runs()
    artifacts = FakeArtifacts()
    invoker = RunWorkerInvoker(report_runs, artifacts, failing_role="baseline")

    try:
        with pytest.raises(RuntimeError, match="coordination failed"):
            _coordinate(report_runs, invoker, artifacts, FakeOutputFiles(), [])
    finally:
        invoker.executor.shutdown(wait=True)

    assert invoker.events == ["spawn:baseline"]
    simulation_run = report_runs.simulation_runs[BASELINE_RUN_ID]
    assert simulation_run.status is RunStatus.FAILED
    assert simulation_run.error_message.startswith("simulation_execution_failed: ")
    report_run = report_runs.report_runs[REPORT_RUN_ID]
    assert report_run.status is RunStatus.FAILED
    assert report_run.error_message == simulation_run.error_message


def test_one_run_on_both_sides_that_another_call_holds_is_waited_for_once() -> None:
    report_runs, files, artifacts = _report_runs(), StoredOutputFiles(), FakeArtifacts()
    run = report_runs.simulation_runs[BASELINE_RUN_ID]
    run.status, run.modal_call_id = RunStatus.RUNNING, "fc-other-worker"
    holder = HolderCall(
        "fc-other-worker",
        report_runs=report_runs,
        simulation_run_id=BASELINE_RUN_ID,
        files=files,
        ending=HolderEnding.SUCCEEDS,
    )
    invoker = HolderInvoker(report_runs, artifacts, holders=[holder])
    aggregated = []

    result = _coordinate(report_runs, invoker, artifacts, files, aggregated)

    assert result == {"report_run_id": str(REPORT_RUN_ID), "status": "succeeded"}
    assert invoker.events == []
    assert files.reads == [stored_output_uri(BASELINE_RUN_ID)]
    (inputs,) = aggregated
    assert inputs["baseline"].descriptor.role is SimulationRole.BASELINE
    assert inputs["reform"].descriptor.role is SimulationRole.REFORM
    assert (
        inputs["baseline"].descriptor.artifact == inputs["reform"].descriptor.artifact
    )
