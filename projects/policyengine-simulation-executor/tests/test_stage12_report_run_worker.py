"""A Stage 12 worker records its simulation run's status and output file."""

from __future__ import annotations

import pytest
from policyengine_simulation_contract.stage12_execution import (
    ComparisonRunLifecycleStatus,
    SimulationRole,
)
from policyengine_simulation_executor.stage12_runtime import (
    SimulationWorkerDependencies,
    run_single_simulation,
)
from policyengine_simulation_executor.stage12_runtime import (
    simulation as simulation_module,
)
from policyengine_stage12_persistence import RunStatus
from stage12_fake_report_runs import (
    BASELINE_RUN_ID,
    FakeReportRuns,
    report_run_context,
)
from stage12_fake_stores import FakeArtifacts, FakeStore, simulation_frames
from stage12_runtime_support import (
    child_record,
    invocation_context,
    planned_simulation,
)


def _seeded_store(simulation, events=None):
    store = FakeStore()
    store.children[simulation.simulation_execution_id] = child_record(simulation)
    if events is not None:
        replace = store.replace_simulation_if_status

        def recording_replace(record, *, expected_status):
            events.append(f"stage12:{record.status.value}")
            return replace(record, expected_status=expected_status)

        store.replace_simulation_if_status = recording_replace
    return store


def _run_worker(simulation, store, report_runs, *, context=None, calculator=None):
    return run_single_simulation(
        simulation.model_dump(mode="json"),
        (context or report_run_context()).model_dump(mode="json"),
        required_country="us",
        modal_call_id="fc-baseline-worker",
        dependencies=SimulationWorkerDependencies(
            store=store,
            artifacts=FakeArtifacts(),
            calculator=calculator or (lambda _: simulation_frames()),
            report_runs=report_runs,
        ),
    )


def test_worker_records_its_simulation_run_after_its_stage12_record() -> None:
    events: list[str] = []
    simulation = planned_simulation(SimulationRole.BASELINE)
    store = _seeded_store(simulation, events)
    report_runs = FakeReportRuns(events)

    result = _run_worker(simulation, store, report_runs)

    run = report_runs.simulation_runs[BASELINE_RUN_ID]
    assert run.status is RunStatus.SUCCEEDED
    assert run.modal_call_id == "fc-baseline-worker"
    assert run.output_uri == result["artifact"]["uri"]
    assert run.output_sha256 == result["artifact"]["content_sha256"]
    # The simulation run succeeds last, so a worker Modal restarts between
    # the two writes finds its Stage 12 record succeeded and completes the
    # simulation run from it, with the same output file.
    assert events == [
        "stage12:running",
        "claim_simulation_run",
        "stage12:succeeded",
        "complete_simulation_run",
    ]


def test_worker_failure_marks_its_simulation_run_failed() -> None:
    simulation = planned_simulation(SimulationRole.BASELINE)
    store = _seeded_store(simulation)
    report_runs = FakeReportRuns()

    def failing_calculator(_simulation):
        msg = "calculation failed"
        raise ValueError(msg)

    with pytest.raises(RuntimeError, match="execution failed"):
        _run_worker(simulation, store, report_runs, calculator=failing_calculator)

    run = report_runs.simulation_runs[BASELINE_RUN_ID]
    assert run.status is RunStatus.FAILED
    assert run.error_message.startswith("simulation_execution_failed: ValueError")
    child = store.children[simulation.simulation_execution_id]
    assert child.status is ComparisonRunLifecycleStatus.FAILED


def test_worker_refuses_a_simulation_run_that_was_already_failed() -> None:
    simulation = planned_simulation(SimulationRole.BASELINE)
    store = _seeded_store(simulation)
    report_runs = FakeReportRuns()
    report_runs.simulation_runs[BASELINE_RUN_ID].status = RunStatus.FAILED
    calculated = []

    with pytest.raises(RuntimeError, match="cancelled"):
        _run_worker(
            simulation,
            store,
            report_runs,
            calculator=lambda value: calculated.append(value) or simulation_frames(),
        )

    assert calculated == []
    assert report_runs.simulation_runs[BASELINE_RUN_ID].output_uri is None


def test_restarted_worker_completes_its_run_from_its_succeeded_stage12_record() -> None:
    simulation = planned_simulation(SimulationRole.BASELINE)
    store = _seeded_store(simulation)
    report_runs = FakeReportRuns()
    # The earlier attempt marked its Stage 12 record succeeded, then Modal
    # pre-empted it before it completed its simulation run.
    store.children[simulation.simulation_execution_id] = store.children[
        simulation.simulation_execution_id
    ].model_copy(
        update={
            "status": ComparisonRunLifecycleStatus.SUCCEEDED,
            "output_uri": "gs://private/earlier-attempt.parquet",
            "output_sha256": "e" * 64,
            "output_schema_version": 1,
            "row_identity_columns": ("household.household_id",),
            "row_count": 1,
            "row_identity_sha256": "f" * 64,
            "completed_at": child_record(simulation).created_at,
        }
    )
    run = report_runs.simulation_runs[BASELINE_RUN_ID]
    run.status, run.modal_call_id = RunStatus.RUNNING, "fc-baseline-worker"
    calculated = []

    result = _run_worker(
        simulation,
        store,
        report_runs,
        calculator=lambda value: calculated.append(value) or simulation_frames(),
    )

    assert calculated == []
    assert result["artifact"]["uri"] == "gs://private/earlier-attempt.parquet"
    assert run.status is RunStatus.SUCCEEDED
    assert run.output_uri == "gs://private/earlier-attempt.parquet"
    assert run.output_sha256 == "e" * 64


def test_a_simulation_run_another_report_recorded_keeps_its_output() -> None:
    simulation = planned_simulation(SimulationRole.BASELINE)
    store = _seeded_store(simulation)
    report_runs = FakeReportRuns()
    # Another report run, sharing this simulation run, already recorded it.
    store.children[simulation.simulation_execution_id] = store.children[
        simulation.simulation_execution_id
    ].model_copy(update={"status": ComparisonRunLifecycleStatus.RUNNING})
    earlier = report_runs.simulation_runs[BASELINE_RUN_ID]
    earlier.status = RunStatus.SUCCEEDED
    earlier.output_uri = "gs://private/earlier-attempt.parquet"
    earlier.output_sha256 = "e" * 64

    result = _run_worker(simulation, store, report_runs)

    assert earlier.output_uri == "gs://private/earlier-attempt.parquet"
    assert earlier.writes == ["claim_simulation_run", "complete_simulation_run"]
    child = store.children[simulation.simulation_execution_id]
    assert child.status is ComparisonRunLifecycleStatus.SUCCEEDED
    assert child.output_uri == result["artifact"]["uri"]


def test_a_lost_simulation_run_success_still_returns_the_workers_result() -> None:
    # The child record already succeeded; the coordinator completes the run
    # from it, so a failed second write must not fail the worker.
    simulation = planned_simulation(SimulationRole.BASELINE)
    store = _seeded_store(simulation)
    report_runs = FakeReportRuns()

    def dropped(*_args, **_kwargs):
        msg = "the connection dropped"
        raise ConnectionError(msg)

    report_runs.complete_simulation_run = dropped

    result = _run_worker(simulation, store, report_runs)

    child = store.children[simulation.simulation_execution_id]
    assert child.status is ComparisonRunLifecycleStatus.SUCCEEDED
    assert child.output_uri == result["artifact"]["uri"]
    assert report_runs.simulation_runs[BASELINE_RUN_ID].status is RunStatus.RUNNING


def test_comparison_worker_records_no_simulation_run() -> None:
    simulation = planned_simulation(SimulationRole.BASELINE)
    store = _seeded_store(simulation)
    report_runs = FakeReportRuns()

    _run_worker(simulation, store, report_runs, context=invocation_context())

    assert report_runs.events == []


def test_report_run_worker_needs_its_modal_call_id() -> None:
    simulation = planned_simulation(SimulationRole.BASELINE)

    with pytest.raises(ValueError, match="Modal call ID"):
        run_single_simulation(
            simulation.model_dump(mode="json"),
            report_run_context().model_dump(mode="json"),
            required_country="us",
            dependencies=SimulationWorkerDependencies(
                store=_seeded_store(simulation),
                artifacts=FakeArtifacts(),
                report_runs=FakeReportRuns(),
            ),
        )


def test_report_run_worker_names_its_artifacts_by_digest(monkeypatch) -> None:
    simulation = planned_simulation(SimulationRole.BASELINE)
    built = []

    def recording_artifact_store(*, content_addressed=False):
        built.append(content_addressed)
        return FakeArtifacts()

    monkeypatch.setattr(simulation_module, "artifact_store", recording_artifact_store)

    run_single_simulation(
        simulation.model_dump(mode="json"),
        report_run_context().model_dump(mode="json"),
        required_country="us",
        modal_call_id="fc-baseline-worker",
        dependencies=SimulationWorkerDependencies(
            store=_seeded_store(simulation),
            calculator=lambda _: simulation_frames(),
            report_runs=FakeReportRuns(),
        ),
    )

    assert built == [True]
