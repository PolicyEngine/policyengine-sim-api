"""The Stage 12 coordinator records the status of the report run its call names."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest
from fixtures.macro_output import CURRENT_SINGLE_YEAR_MACRO_RESULT
from policyengine_simulation_contract.stage12_execution import (
    ComparisonRunLifecycleStatus,
    SimulationRole,
)
from policyengine_simulation_executor.stage12_runtime import (
    SimulationWorkerDependencies,
    run_single_simulation,
)
from policyengine_simulation_executor.stage12_runtime import (
    coordination as coordination_module,
)
from policyengine_stage12_persistence import RunStatus
from stage12_child_call_support import (
    ConcurrentInvoker,
    FutureCall,
    InjectedChildFaults,
)
from stage12_fake_report_runs import (
    BASELINE_RUN_ID,
    REFORM_RUN_ID,
    REPORT_RUN_ID,
    FakeReportRuns,
    economy_aggregate,
    report_run_context,
)
from stage12_fake_stores import FakeArtifacts, FakeStore, simulation_frames
from stage12_runtime_support import (
    child_record,
    coordinate_report,
    parent_record,
    planned_simulation,
    report_execution_input,
)

COORDINATOR_CALL_ID = "fc-coordinator"


class RecordingInvoker(ConcurrentInvoker):
    """Run both children concurrently, keeping the context each one received."""

    def __init__(self, artifacts, **options):
        super().__init__(artifacts, **options)
        self.contexts = []

    def spawn(self, request):
        self.contexts.append(request.context)
        return super().spawn(request)


def _coordinate(store, report_runs, invoker, artifacts, *, context=None):
    context = context or report_run_context()
    return coordinate_report(
        report_execution_input().model_dump(mode="json"),
        context.model_dump(mode="json"),
        parent_record().model_dump(mode="json"),
        application_name=context.modal_application,
        coordinator_invocation_id=COORDINATOR_CALL_ID,
        store=store,
        artifacts=artifacts,
        invoker=invoker,
        aggregator=economy_aggregate,
        report_runs=report_runs,
    )


def _recording_report_writes(store, events):
    replace_report = store.replace_report

    def recording_replace_report(record):
        events.append(f"stage12:{record.status.value}")
        return replace_report(record)

    store.replace_report = recording_replace_report
    return store


def test_coordinator_claims_its_report_run_first_and_completes_it_after_stage12() -> (
    None
):
    events: list[str] = []
    store = _recording_report_writes(FakeStore(), events)
    report_runs = FakeReportRuns(events)
    artifacts = FakeArtifacts()
    invoker = RecordingInvoker(artifacts)

    result = _coordinate(store, report_runs, invoker, artifacts)

    run = report_runs.report_runs[REPORT_RUN_ID]
    assert run.status is RunStatus.SUCCEEDED
    assert run.modal_call_id == COORDINATOR_CALL_ID
    assert result["evaluation_id"] == str(report_execution_input().evaluation_id)
    # The report run is claimed first and completed last, so a call Modal
    # restarts between the two success writes finds its Stage 12 report
    # succeeded, and completes the report run from the stored aggregate.
    assert events == ["claim_report_run", "stage12:succeeded", "complete_report_run"]
    assert store.parent.status is ComparisonRunLifecycleStatus.SUCCEEDED
    assert [context["report_run"] for context in invoker.contexts] == [
        report_run_context().report_run.model_dump(mode="json")
    ] * 2


def test_coordinator_completes_its_report_run_with_the_economy_output_as_rows() -> None:
    report_runs = FakeReportRuns()
    artifacts = FakeArtifacts()

    _coordinate(FakeStore(), report_runs, RecordingInvoker(artifacts), artifacts)

    result = report_runs.report_runs[REPORT_RUN_ID].result
    rows = {rows.table.name: rows.rows for rows in result.rows}
    budget = {row["variable_name"]: row for row in rows["budget_summary"]}
    assert budget["household_tax"]["change"] == 100.0
    assert budget["household_tax"]["report_run_id"] == REPORT_RUN_ID
    # The shared report is a US one, so its poverty rows use the SPM types.
    assert {row["poverty_type"] for row in rows["poverty"]} == {"spm", "spm_deep"}
    # The report run's own columns: the output has no cliff impact or SPM
    # receipt, so only the data version is set.
    assert result.report_run_columns == {
        "baseline_cliff_gap": None,
        "baseline_cliff_share": None,
        "reform_cliff_gap": None,
        "reform_cliff_share": None,
        "spm_provenance": None,
        "data_version": CURRENT_SINGLE_YEAR_MACRO_RESULT["data_version"],
    }
    (labor_supply,) = rows["labor_supply_responses"]
    assert labor_supply["report_run_id"] == REPORT_RUN_ID
    assert (
        labor_supply["total_earnings_change"]
        == (CURRENT_SINGLE_YEAR_MACRO_RESULT["labor_supply_response"]["total_change"])
    )
    # The output reports no labour supply response by decile.
    assert rows["labor_supply_decile_responses"] == ()
    # Alabama's first district, by .py's state FIPS codes.
    (district,) = rows["congressional_district_impacts"]
    assert (district["state_fips"], district["district_number"]) == (1, 1)
    assert district["district_geoid"] == 101


def test_comparison_children_receive_the_context_they_received_before() -> None:
    artifacts = FakeArtifacts()
    invoker = RecordingInvoker(artifacts)
    report_runs = FakeReportRuns()

    _coordinate(
        FakeStore(),
        report_runs,
        invoker,
        artifacts,
        context=report_run_context().model_copy(update={"report_run": None}),
    )

    assert all("report_run" not in context for context in invoker.contexts)
    assert report_runs.events == []


@pytest.mark.parametrize(
    ("status", "modal_call_id"),
    [
        # The simulation service recorded another call it started.
        (RunStatus.PENDING, "fc-another-coordinator"),
        (RunStatus.RUNNING, "fc-another-coordinator"),
        (RunStatus.SUCCEEDED, COORDINATOR_CALL_ID),
        (RunStatus.FAILED, COORDINATOR_CALL_ID),
    ],
)
def test_a_report_run_that_cannot_be_claimed_starts_nothing(
    status, modal_call_id
) -> None:
    store = FakeStore()
    report_runs = FakeReportRuns()
    run = report_runs.report_runs[REPORT_RUN_ID]
    run.status, run.modal_call_id = status, modal_call_id
    artifacts = FakeArtifacts()
    invoker = RecordingInvoker(artifacts)

    result = _coordinate(store, report_runs, invoker, artifacts)

    assert result == {
        "deduplicated": True,
        "report_run_id": str(REPORT_RUN_ID),
        "status": status.value,
    }
    assert invoker.events == []
    assert store.parent is None
    assert run.writes == ["claim_report_run"]


def test_a_report_run_must_name_the_simulation_runs_its_call_names() -> None:
    store = FakeStore()
    report_runs = FakeReportRuns()
    report_runs.simulation_run_pairs[REPORT_RUN_ID] = (REFORM_RUN_ID, BASELINE_RUN_ID)
    artifacts = FakeArtifacts()
    invoker = RecordingInvoker(artifacts)

    with pytest.raises(ValueError, match="names other simulation runs"):
        _coordinate(store, report_runs, invoker, artifacts)

    assert invoker.events == []
    assert store.parent is None
    assert report_runs.report_runs[REPORT_RUN_ID].status is RunStatus.PENDING


def test_a_failed_report_fails_its_report_run_and_the_failed_simulation_run() -> None:
    store = FakeStore()
    report_runs = FakeReportRuns()
    artifacts = FakeArtifacts()
    invoker = RecordingInvoker(
        artifacts, faults=InjectedChildFaults(fail_role="reform")
    )

    with pytest.raises(RuntimeError, match="coordination failed"):
        _coordinate(store, report_runs, invoker, artifacts)

    run = report_runs.report_runs[REPORT_RUN_ID]
    assert run.status is RunStatus.FAILED
    assert run.error_message.startswith("simulation_invocation_failed: ")
    reform = report_runs.simulation_runs[REFORM_RUN_ID]
    assert reform.status is RunStatus.FAILED
    assert reform.error_message == run.error_message
    assert store.parent.status is ComparisonRunLifecycleStatus.FAILED


def test_a_failed_report_records_its_stage12_failure_before_its_report_run() -> None:
    events: list[str] = []
    store = _recording_report_writes(FakeStore(), events)
    report_runs = FakeReportRuns(events)
    artifacts = FakeArtifacts()
    invoker = RecordingInvoker(
        artifacts, faults=InjectedChildFaults(fail_role="reform")
    )

    with pytest.raises(RuntimeError, match="coordination failed"):
        _coordinate(store, report_runs, invoker, artifacts)

    assert [
        event for event in events if event in {"stage12:failed", "fail_report_run"}
    ] == ["stage12:failed", "fail_report_run"]


def test_a_stage12_claim_that_fails_fails_the_report_run() -> None:
    store = FakeStore()
    report_runs = FakeReportRuns()

    def unreachable(_record):
        msg = "the Stage 12 database refused the connection"
        raise ConnectionError(msg)

    store.create_or_resolve_report = unreachable
    artifacts = FakeArtifacts()
    invoker = RecordingInvoker(artifacts)

    with pytest.raises(ConnectionError):
        _coordinate(store, report_runs, invoker, artifacts)

    run = report_runs.report_runs[REPORT_RUN_ID]
    assert run.status is RunStatus.FAILED
    assert run.error_message.startswith("report_claim_failed: ")
    assert "refused the connection" not in run.error_message
    assert invoker.events == []


def test_a_stage12_report_another_call_finished_fails_the_report_run() -> None:
    store = FakeStore()
    store.parent = parent_record().model_copy(
        update={
            "status": ComparisonRunLifecycleStatus.SUCCEEDED,
            "coordinator_invocation_id": "fc-another-coordinator",
        }
    )
    report_runs = FakeReportRuns()
    artifacts = FakeArtifacts()
    invoker = RecordingInvoker(artifacts)

    result = _coordinate(store, report_runs, invoker, artifacts)

    assert result["deduplicated"] is True
    run = report_runs.report_runs[REPORT_RUN_ID]
    assert run.status is RunStatus.FAILED
    assert run.error_message == (
        "report_already_claimed: This report's Stage 12 record is succeeded "
        "under another call."
    )
    assert invoker.events == []


class LosingOneFailureWrite(FakeReportRuns):
    """Lose the first write that fails a simulation run, as a dropped connection."""

    def __init__(self):
        super().__init__()
        self.lost_failure_writes = 0

    def fail_simulation_run(self, simulation_run_id, *, error_message, failed_at):
        if not self.lost_failure_writes:
            self.lost_failure_writes += 1
            msg = "the connection dropped"
            raise ConnectionError(msg)
        return super().fail_simulation_run(
            simulation_run_id, error_message=error_message, failed_at=failed_at
        )


def test_a_lost_simulation_run_failure_is_written_when_the_report_fails() -> None:
    store = FakeStore()
    report_runs = LosingOneFailureWrite()
    artifacts = FakeArtifacts()
    invoker = RecordingInvoker(
        artifacts, faults=InjectedChildFaults(fail_role="reform")
    )

    with pytest.raises(RuntimeError, match="coordination failed"):
        _coordinate(store, report_runs, invoker, artifacts)

    assert report_runs.lost_failure_writes == 1
    reform = report_runs.simulation_runs[REFORM_RUN_ID]
    assert reform.status is RunStatus.FAILED
    assert reform.error_message.startswith("simulation_invocation_failed: ")


def test_a_reused_succeeded_child_completes_its_simulation_run() -> None:
    # A worker marked its child record succeeded, then stopped before it
    # completed its simulation run; a later call reuses that child's output.
    store = FakeStore()
    artifacts = FakeArtifacts()
    baseline = planned_simulation(SimulationRole.BASELINE)
    descriptor = artifacts.add_simulation(baseline)
    store.children[baseline.simulation_execution_id] = child_record(
        baseline
    ).model_copy(
        update={
            "status": ComparisonRunLifecycleStatus.SUCCEEDED,
            "output_uri": descriptor.artifact.uri,
            "output_sha256": descriptor.artifact.content_sha256,
            "output_schema_version": 1,
            "row_identity_columns": descriptor.row_identity.identifier_columns,
            "row_count": descriptor.row_identity.row_count,
            "row_identity_sha256": descriptor.row_identity.identity_sha256,
            "completed_at": child_record(baseline).created_at,
        }
    )
    report_runs = FakeReportRuns()
    report_runs.simulation_runs[BASELINE_RUN_ID].status = RunStatus.RUNNING
    invoker = RecordingInvoker(artifacts)

    _coordinate(store, report_runs, invoker, artifacts)

    assert invoker.events == ["spawn:reform"]
    run = report_runs.simulation_runs[BASELINE_RUN_ID]
    assert run.status is RunStatus.SUCCEEDED
    assert run.output_uri == descriptor.artifact.uri
    # The simulation runs are recorded before the report run succeeds.
    assert report_runs.events.index("complete_simulation_run") < (
        report_runs.events.index("complete_report_run")
    )


class WorkerInvoker:
    """Run each child through the real worker code, sharing the coordinator's stores."""

    def __init__(self, store, artifacts, report_runs):
        self.store = store
        self.artifacts = artifacts
        self.report_runs = report_runs
        self.executor = ThreadPoolExecutor(max_workers=2)

    def spawn(self, request):
        role = request.simulation["role"]
        frames = simulation_frames(100.0 if role == "baseline" else 120.0)
        future = self.executor.submit(
            run_single_simulation,
            request.simulation,
            request.context,
            required_country="us",
            modal_call_id=f"fc-{role}-worker",
            dependencies=SimulationWorkerDependencies(
                store=self.store,
                artifacts=self.artifacts,
                calculator=lambda _simulation: frames,
                report_runs=self.report_runs,
            ),
        )
        return FutureCall(f"fc-{role}-worker", future)

    def restore(self, invocation_id):
        msg = f"unexpected restored invocation {invocation_id}"
        raise AssertionError(msg)


def test_a_report_run_through_real_workers_records_every_run_once() -> None:
    store = FakeStore()
    artifacts = FakeArtifacts()
    report_runs = FakeReportRuns()

    report = report_execution_input()
    # A reform the real worker accepts: an effective period with dates.
    report = report.model_copy(
        update={
            "reform": report.reform.model_copy(
                update={"policy": {"reform": {"2026-01-01.2026-12-31": 1}}}
            )
        }
    )

    coordinate_report(
        report.model_dump(mode="json"),
        report_run_context().model_dump(mode="json"),
        parent_record().model_dump(mode="json"),
        application_name=report_run_context().modal_application,
        coordinator_invocation_id=COORDINATOR_CALL_ID,
        store=store,
        artifacts=artifacts,
        invoker=WorkerInvoker(store, artifacts, report_runs),
        aggregator=economy_aggregate,
        report_runs=report_runs,
    )

    assert report_runs.report_runs[REPORT_RUN_ID].status is RunStatus.SUCCEEDED
    for role, run_id in (("baseline", BASELINE_RUN_ID), ("reform", REFORM_RUN_ID)):
        run = report_runs.simulation_runs[run_id]
        (child,) = [child for child in store.children.values() if child.role == role]
        assert run.status is RunStatus.SUCCEEDED
        assert run.modal_call_id == f"fc-{role}-worker"
        assert run.output_uri == child.output_uri
        assert run.writes.count("complete_simulation_run") >= 1
    assert report_runs.report_runs[REPORT_RUN_ID].writes == [
        "claim_report_run",
        "complete_report_run",
    ]


def test_report_run_coordinator_names_its_aggregate_by_digest(monkeypatch) -> None:
    built = []
    artifacts = FakeArtifacts()

    def recording_artifact_store(*, content_addressed=False):
        built.append(content_addressed)
        return artifacts

    monkeypatch.setattr(coordination_module, "artifact_store", recording_artifact_store)

    coordinate_report(
        report_execution_input().model_dump(mode="json"),
        report_run_context().model_dump(mode="json"),
        parent_record().model_dump(mode="json"),
        application_name=report_run_context().modal_application,
        coordinator_invocation_id=COORDINATOR_CALL_ID,
        store=FakeStore(),
        invoker=RecordingInvoker(artifacts),
        aggregator=economy_aggregate,
        report_runs=FakeReportRuns(),
    )

    assert built == [True]
