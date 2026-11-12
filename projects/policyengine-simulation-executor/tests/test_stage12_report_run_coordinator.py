"""A report run's coordinator works on the run tables alone.

The simulation service starts the coordinator with a report run's ID. The
coordinator claims that run under its own Modal call ID and starts a worker
for each simulation run it doesn't reuse; each worker claims its own
simulation run under its own call ID. No call creates or claims a Stage 12
evaluation record: every test here hands the coordinator a comparison store
that fails on any use.
"""

from __future__ import annotations

import pytest
from fixtures.fake_object_store import InMemoryObjectStore
from fixtures.macro_output import CURRENT_SINGLE_YEAR_MACRO_RESULT
from policyengine_simulation_contract.stage12_execution import SimulationRole
from policyengine_simulation_executor import stage12_artifacts
from policyengine_simulation_executor.stage12_artifacts import (
    SimulationOutputBucket,
    Stage12ArtifactStore,
)
from policyengine_simulation_executor.stage12_runtime import (
    report_run_coordination as report_run_coordination_module,
)
from policyengine_stage12_persistence import RunStatus
from stage12_child_call_support import ConcurrentInvoker
from stage12_fake_report_runs import (
    BASELINE_RUN_ID,
    REFORM_RUN_ID,
    REPORT_RUN_ID,
    REPORT_RUN_PREFIX,
    SIMULATION_RUN_IDS,
    ComparisonStoreNotUsed,
    FakeReportRuns,
    economy_aggregate,
    report_run_context,
    report_run_input,
)
from stage12_fake_stores import SIMULATION_OUTPUT_BUCKET, FakeArtifacts, FakeStore
from stage12_run_worker_support import RunWorkerInvoker
from stage12_runtime_support import (
    coordinate_report,
    invocation_context,
    parent_record,
    report_execution_input,
)

COORDINATOR_CALL_ID = "fc-coordinator"


def _coordinate(report_runs, invoker, artifacts, *, report=None, **seams):
    context = seams.pop("context", report_run_context())
    return coordinate_report(
        (report or report_run_input()).model_dump(mode="json"),
        context.model_dump(mode="json"),
        application_name=context.modal_application,
        coordinator_invocation_id=COORDINATOR_CALL_ID,
        store=ComparisonStoreNotUsed(),
        artifacts=artifacts,
        invoker=invoker,
        aggregator=seams.pop("aggregator", economy_aggregate),
        report_runs=report_runs,
        **seams,
    )


def _run_through_workers(report_runs=None, **invoker_options):
    report_runs = report_runs or FakeReportRuns()
    artifacts = FakeArtifacts()
    invoker = RunWorkerInvoker(report_runs, artifacts, **invoker_options)
    return report_runs, artifacts, invoker


def test_the_coordinator_claims_its_report_run_and_each_worker_its_simulation_run() -> (
    None
):
    report_runs, artifacts, invoker = _run_through_workers()

    result = _coordinate(report_runs, invoker, artifacts)

    assert result == {"report_run_id": str(REPORT_RUN_ID), "status": "succeeded"}
    run = report_runs.report_runs[REPORT_RUN_ID]
    assert (run.status, run.modal_call_id) == (RunStatus.SUCCEEDED, COORDINATOR_CALL_ID)
    assert run.writes == ["claim_report_run", "complete_report_run"]
    worker_calls = dict(
        zip(
            (simulation["role"] for simulation in invoker.simulations),
            invoker.call_ids,
            strict=True,
        )
    )
    for role, run_id in (("baseline", BASELINE_RUN_ID), ("reform", REFORM_RUN_ID)):
        simulation_run = report_runs.simulation_runs[run_id]
        assert simulation_run.status is RunStatus.SUCCEEDED
        # The worker's own call claimed and completed its run.
        assert simulation_run.modal_call_id == worker_calls[role]
        assert simulation_run.writes == [
            "claim_simulation_run",
            "complete_simulation_run",
        ]
        assert simulation_run.output_uri == (
            f"gs://{SIMULATION_OUTPUT_BUCKET}/staging/{run_id}.parquet"
        )
    # Both simulation runs succeeded before the report run did.
    assert report_runs.events[-1] == "complete_report_run"


def test_each_worker_is_started_with_only_its_own_simulation_run() -> None:
    report_runs, artifacts, invoker = _run_through_workers()

    _coordinate(report_runs, invoker, artifacts)

    assert sorted(simulation["role"] for simulation in invoker.simulations) == [
        "baseline",
        "reform",
    ]
    for simulation, context in zip(invoker.simulations, invoker.contexts, strict=True):
        run_id = SIMULATION_RUN_IDS[SimulationRole(simulation["role"])]
        assert context.simulation_run_id == run_id
        assert context.report_run is None
        assert context.artifact_prefix == REPORT_RUN_PREFIX
        assert simulation["simulation_execution_id"] == str(run_id)
        assert simulation["evaluation_id"] == str(REPORT_RUN_ID)
        assert "output_plan" in simulation


def test_coordinator_completes_its_report_run_with_the_economy_output_as_rows() -> None:
    report_runs, artifacts, invoker = _run_through_workers()

    _coordinate(report_runs, invoker, artifacts)

    result = report_runs.report_runs[REPORT_RUN_ID].result
    rows = {rows.table.name: rows.rows for rows in result.rows}
    budget = {row["variable_name"]: row for row in rows["budget_summary"]}
    assert budget["household_tax"]["change"] == 100.0
    assert budget["household_tax"]["report_run_id"] == REPORT_RUN_ID
    assert budget["household_tax"]["baseline_simulation_run_id"] == BASELINE_RUN_ID
    # The shared report is a US one, so its poverty rows use the SPM types.
    assert {row["poverty_type"] for row in rows["poverty"]} == {"spm", "spm_deep"}
    assert (
        result.report_run_columns["data_version"]
        == (CURRENT_SINGLE_YEAR_MACRO_RESULT["data_version"])
    )
    (district,) = rows["congressional_district_impacts"]
    assert (district["state_fips"], district["district_number"]) == (1, 1)


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
    report_runs, artifacts, invoker = _run_through_workers()
    run = report_runs.report_runs[REPORT_RUN_ID]
    run.status, run.modal_call_id = status, modal_call_id

    result = _coordinate(report_runs, invoker, artifacts)

    assert result == {
        "deduplicated": True,
        "report_run_id": str(REPORT_RUN_ID),
        "status": status.value,
    }
    assert invoker.call_ids == []
    assert run.writes == ["claim_report_run"]
    assert all(not run.writes for run in report_runs.simulation_runs.values())


def _with_evaluation_id(report):
    # The report and both simulations name another evaluation together.
    return report.model_copy(
        update={
            "evaluation_id": BASELINE_RUN_ID,
            "baseline": report.baseline.model_copy(
                update={"evaluation_id": BASELINE_RUN_ID}
            ),
            "reform": report.reform.model_copy(
                update={"evaluation_id": BASELINE_RUN_ID}
            ),
        }
    )


def _with_baseline_execution_id(report):
    return report.model_copy(
        update={
            "baseline": report.baseline.model_copy(
                update={"simulation_execution_id": REPORT_RUN_ID}
            )
        }
    )


def _with_runs_swapped(report):
    return report.model_copy(
        update={
            "baseline": report.baseline.model_copy(
                update={"simulation_execution_id": REFORM_RUN_ID}
            ),
            "reform": report.reform.model_copy(
                update={"simulation_execution_id": BASELINE_RUN_ID}
            ),
        }
    )


@pytest.mark.parametrize(
    "named_otherwise",
    [_with_evaluation_id, _with_baseline_execution_id, _with_runs_swapped],
)
def test_a_report_input_naming_other_runs_fails_the_report_run_before_any_worker(
    named_otherwise,
) -> None:
    report_runs, artifacts, invoker = _run_through_workers()

    with pytest.raises(RuntimeError, match="coordination failed"):
        _coordinate(
            report_runs, invoker, artifacts, report=named_otherwise(report_run_input())
        )

    run = report_runs.report_runs[REPORT_RUN_ID]
    assert run.status is RunStatus.FAILED
    assert run.error_message.startswith("report_run_input_mismatch: ")
    assert invoker.call_ids == []
    # Runs the call's input doesn't match are not this coordinator's to fail.
    assert all(not run.writes for run in report_runs.simulation_runs.values())


def test_a_report_run_naming_other_simulation_runs_fails_and_leaves_them() -> None:
    report_runs, artifacts, invoker = _run_through_workers()
    report_runs.simulation_run_pairs[REPORT_RUN_ID] = (REFORM_RUN_ID, BASELINE_RUN_ID)

    with pytest.raises(RuntimeError, match="coordination failed"):
        _coordinate(report_runs, invoker, artifacts)

    run = report_runs.report_runs[REPORT_RUN_ID]
    assert run.status is RunStatus.FAILED
    assert run.error_message.startswith("report_run_input_mismatch: ")
    assert "names other simulation runs" in run.error_message
    assert invoker.call_ids == []
    assert all(not run.writes for run in report_runs.simulation_runs.values())


def test_a_malformed_report_input_fails_the_claimed_report_run() -> None:
    report_runs, artifacts, invoker = _run_through_workers()
    malformed = report_run_input().model_dump(mode="json")
    malformed["requested_aggregates"] = []

    with pytest.raises(RuntimeError, match="coordination failed"):
        coordinate_report(
            malformed,
            report_run_context().model_dump(mode="json"),
            application_name=report_run_context().modal_application,
            coordinator_invocation_id=COORDINATOR_CALL_ID,
            store=ComparisonStoreNotUsed(),
            artifacts=artifacts,
            invoker=invoker,
            report_runs=report_runs,
        )

    run = report_runs.report_runs[REPORT_RUN_ID]
    assert run.status is RunStatus.FAILED
    assert run.error_message.startswith("report_coordination_failed: ")
    assert invoker.call_ids == []


def test_a_simulation_run_that_already_failed_fails_the_report_run_unstarted() -> None:
    report_runs, artifacts, invoker = _run_through_workers()
    reform = report_runs.simulation_runs[REFORM_RUN_ID]
    reform.status = RunStatus.FAILED
    reform.error_message = "run_exceeded_maximum_age: The run was still unfinished."

    with pytest.raises(RuntimeError, match="coordination failed"):
        _coordinate(report_runs, invoker, artifacts)

    run = report_runs.report_runs[REPORT_RUN_ID]
    assert run.status is RunStatus.FAILED
    assert run.error_message == (
        f"simulation_run_failed: The reform simulation run {REFORM_RUN_ID} "
        "already failed: run_exceeded_maximum_age: The run was still unfinished."
    )
    assert invoker.call_ids == []
    # The baseline was never started; it fails with its report run.
    baseline = report_runs.simulation_runs[BASELINE_RUN_ID]
    assert baseline.status is RunStatus.FAILED
    assert baseline.error_message == (
        "simulation_cancelled: Cancelled after the report failed."
    )
    assert reform.error_message.startswith("run_exceeded_maximum_age: ")


def test_a_failed_worker_fails_the_report_run_with_its_own_cause() -> None:
    report_runs, artifacts, invoker = _run_through_workers(
        failing_role="reform", held_roles=("baseline",)
    )

    try:
        with pytest.raises(RuntimeError, match="coordination failed"):
            _coordinate(report_runs, invoker, artifacts)
    finally:
        invoker.release_held.set()
        invoker.executor.shutdown(wait=True)

    reform = report_runs.simulation_runs[REFORM_RUN_ID]
    assert reform.status is RunStatus.FAILED
    assert reform.error_message.startswith("simulation_execution_failed: ")
    run = report_runs.report_runs[REPORT_RUN_ID]
    assert run.status is RunStatus.FAILED
    assert run.error_message == reform.error_message
    # The coordinator stopped the baseline, which then could not succeed.
    baseline = report_runs.simulation_runs[BASELINE_RUN_ID]
    assert baseline.status is RunStatus.FAILED
    assert baseline.error_message == (
        "simulation_cancelled: Cancelled after the reform simulation failed."
    )


def test_a_planning_failure_fails_the_report_run_and_its_unstarted_runs() -> None:
    report_runs, artifacts, invoker = _run_through_workers()

    def unplannable(_report):
        msg = "the country model could not be loaded"
        raise RuntimeError(msg)

    with pytest.raises(RuntimeError, match="coordination failed"):
        _coordinate(report_runs, invoker, artifacts, output_plan_resolver=unplannable)

    run = report_runs.report_runs[REPORT_RUN_ID]
    assert run.status is RunStatus.FAILED
    assert run.error_message.startswith("report_coordination_failed: ")
    assert invoker.call_ids == []
    assert all(
        simulation_run.status is RunStatus.FAILED
        and simulation_run.error_message
        == "simulation_cancelled: Cancelled after the report failed."
        for simulation_run in report_runs.simulation_runs.values()
    )


def test_an_aggregation_failure_keeps_the_simulation_runs_that_succeeded() -> None:
    report_runs, artifacts, invoker = _run_through_workers()

    def failing_aggregator(**_values):
        msg = "aggregation failed"
        raise ValueError(msg)

    with pytest.raises(RuntimeError, match="coordination failed"):
        _coordinate(report_runs, invoker, artifacts, aggregator=failing_aggregator)

    assert report_runs.report_runs[REPORT_RUN_ID].status is RunStatus.FAILED
    # Their outputs stay usable: a later report run can reuse them.
    assert all(
        simulation_run.status is RunStatus.SUCCEEDED
        for simulation_run in report_runs.simulation_runs.values()
    )


def test_a_call_naming_a_report_run_and_a_parent_is_refused_unwritten() -> None:
    report_runs, artifacts, invoker = _run_through_workers()

    with pytest.raises(ValueError, match="no evaluation record"):
        coordinate_report(
            report_run_input().model_dump(mode="json"),
            report_run_context().model_dump(mode="json"),
            parent_record().model_dump(mode="json"),
            application_name=report_run_context().modal_application,
            coordinator_invocation_id=COORDINATOR_CALL_ID,
            store=ComparisonStoreNotUsed(),
            artifacts=artifacts,
            invoker=invoker,
            report_runs=report_runs,
        )

    assert report_runs.events == []
    assert invoker.call_ids == []


def test_a_comparison_call_needs_its_evaluation_record() -> None:
    store = FakeStore()
    report_runs = FakeReportRuns()

    with pytest.raises(ValueError, match="evaluation record"):
        coordinate_report(
            report_execution_input().model_dump(mode="json"),
            invocation_context().model_dump(mode="json"),
            application_name=invocation_context().modal_application,
            coordinator_invocation_id=COORDINATOR_CALL_ID,
            store=store,
            artifacts=FakeArtifacts(),
            invoker=ConcurrentInvoker(FakeArtifacts()),
            report_runs=report_runs,
        )

    assert store.parent is None
    assert report_runs.events == []


def test_a_comparison_call_records_no_run_and_gives_its_children_the_same_context() -> (
    None
):
    store = FakeStore()
    report_runs = FakeReportRuns()
    artifacts = FakeArtifacts()
    contexts = []
    invoker = ConcurrentInvoker(artifacts)
    spawn = invoker.spawn

    def recording_spawn(request):
        contexts.append(request.context)
        return spawn(request)

    invoker.spawn = recording_spawn

    coordinate_report(
        report_execution_input().model_dump(mode="json"),
        invocation_context().model_dump(mode="json"),
        parent_record().model_dump(mode="json"),
        application_name=invocation_context().modal_application,
        coordinator_invocation_id=COORDINATOR_CALL_ID,
        store=store,
        artifacts=artifacts,
        invoker=invoker,
        aggregator=economy_aggregate,
        report_runs=report_runs,
    )

    assert report_runs.events == []
    # The workers deployed now accept exactly the fields they accepted before.
    assert all(
        "report_run" not in context and "simulation_run_id" not in context
        for context in contexts
    )
    assert store.parent.aggregate_output_uri is not None


def test_report_run_coordinator_names_its_artifacts_by_digest(monkeypatch) -> None:
    built = []
    report_runs, artifacts, invoker = _run_through_workers()

    def recording_artifact_store(*, report_run=False):
        built.append(report_run)
        return artifacts

    monkeypatch.setattr(
        report_run_coordination_module, "artifact_store", recording_artifact_store
    )

    context = report_run_context()
    coordinate_report(
        report_run_input().model_dump(mode="json"),
        context.model_dump(mode="json"),
        application_name=context.modal_application,
        coordinator_invocation_id=COORDINATOR_CALL_ID,
        store=ComparisonStoreNotUsed(),
        invoker=invoker,
        aggregator=economy_aggregate,
        report_runs=report_runs,
    )

    assert built == [True]


def test_a_report_run_reads_its_workers_outputs_from_the_simulation_output_bucket() -> (
    None
):
    artifact_bucket = "policyengine-stage12-staging-artifacts"
    output_bucket = "policyengine-v2-staging-simulation-outputs"
    report_runs = FakeReportRuns()
    artifact_objects = InMemoryObjectStore()
    output_objects = InMemoryObjectStore()
    artifacts = Stage12ArtifactStore(
        artifact_bucket,
        store=artifact_objects,
        content_addressed=True,
        simulation_outputs=SimulationOutputBucket(output_bucket, store=output_objects),
    )

    _coordinate(report_runs, RunWorkerInvoker(report_runs, artifacts), artifacts)

    assert report_runs.report_runs[REPORT_RUN_ID].status is RunStatus.SUCCEEDED
    for run_id in (BASELINE_RUN_ID, REFORM_RUN_ID):
        output_uri = report_runs.simulation_runs[run_id].output_uri
        run_prefix = f"gs://{output_bucket}/simulation-runs/staging/us/{run_id}/"
        assert output_uri.startswith(f"{run_prefix}output-")
        assert (
            output_uri.removeprefix(f"gs://{output_bucket}/") in output_objects.objects
        )
    assert len(output_objects.objects) == 2
    # Only the planned inputs go to the Stage 12 artifact bucket, under the
    # report run's prefix; the result is in the result tables, so no
    # aggregate file is written.
    assert len(artifact_objects.objects) == 2
    assert all(
        path.startswith(f"{REPORT_RUN_PREFIX}/inputs/")
        for path in artifact_objects.objects
    )


def test_a_report_run_coordinator_without_an_output_bucket_fails_before_its_claim(
    monkeypatch,
) -> None:
    monkeypatch.setenv(
        "STAGE12_ARTIFACT_BUCKET", "policyengine-stage12-staging-artifacts"
    )
    monkeypatch.delenv("V2_SIMULATION_OUTPUT_BUCKET", raising=False)
    # Every bucket the coordinator opens is in memory, so it never reaches GCP.
    monkeypatch.setattr(
        stage12_artifacts, "ArtifactStore", lambda _bucket_name: InMemoryObjectStore()
    )
    report_runs, _artifacts, invoker = _run_through_workers()
    context = report_run_context()

    with pytest.raises(ValueError, match="V2_SIMULATION_OUTPUT_BUCKET"):
        coordinate_report(
            report_run_input().model_dump(mode="json"),
            context.model_dump(mode="json"),
            application_name=context.modal_application,
            coordinator_invocation_id=COORDINATOR_CALL_ID,
            store=ComparisonStoreNotUsed(),
            invoker=invoker,
            aggregator=economy_aggregate,
            report_runs=report_runs,
        )

    assert invoker.call_ids == []
    report_run = report_runs.report_runs[REPORT_RUN_ID]
    assert (report_run.status, report_run.writes) == (RunStatus.PENDING, [])
