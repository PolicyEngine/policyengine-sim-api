"""Tests for how a Stage 12 single-simulation worker records and reports failures."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from dataset_year_errors import (
    DATASET_YEAR_MESSAGE,
    DATASET_YEAR_UNAVAILABLE,
    dataset_year_error,
)
from policyengine_simulation_contract.spm import SPMInputError
from policyengine_simulation_contract.stage12_execution import (
    ComparisonRunLifecycleStatus,
    SimulationRole,
)
from policyengine_simulation_executor.stage12_runtime import (
    SimulationCalculation,
    SimulationWorkerDependencies,
    run_single_simulation,
)
from policyengine_simulation_executor.stage12_runtime import (
    simulation as simulation_module,
)
from sqlalchemy.exc import OperationalError
from stage12_fake_stores import FakeArtifacts, FakeStore, simulation_frames
from stage12_runtime_support import (
    BASELINE_ID,
    EVALUATION_ID,
    RecordingRuntime,
    TelemetryRecordingRuntime,
    child_record,
    connection_limit_error,
    invocation_context,
    planned_simulation,
    summary_correlation_id,
    summary_failure_cause,
)


def test_single_worker_exposes_only_a_bounded_failure() -> None:
    store = FakeStore()
    simulation = planned_simulation(SimulationRole.BASELINE)
    store.children[simulation.simulation_execution_id] = child_record(simulation)

    def fail(_simulation):
        msg = "country package calculation failed"
        raise RuntimeError(msg)

    with pytest.raises(
        RuntimeError, match="Stage 12 simulation execution failed"
    ) as error:
        run_single_simulation(
            simulation.model_dump(mode="json"),
            invocation_context().model_dump(mode="json"),
            required_country="us",
            dependencies=SimulationWorkerDependencies(
                store=store,
                artifacts=FakeArtifacts(),
                calculator=fail,
            ),
        )

    assert "country package calculation failed" not in str(error.value)
    child = store.children[simulation.simulation_execution_id]
    assert child.status is ComparisonRunLifecycleStatus.FAILED
    assert child.error_code == "simulation_execution_failed"
    assert summary_failure_cause(child.error_summary) == (
        "RuntimeError in stage12_calculation (baseline): "
        "country package calculation failed"
    )


def test_single_worker_persists_and_records_a_redacted_cause() -> None:
    store = FakeStore()
    simulation = planned_simulation(SimulationRole.BASELINE)
    store.children[simulation.simulation_execution_id] = child_record(simulation)
    runtime = RecordingRuntime()

    def fail(_simulation):
        msg = "could not reach postgresql://runtime:database-secret@db.example/postgres"
        raise RuntimeError(msg)

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

    child = store.children[simulation.simulation_execution_id]
    assert summary_failure_cause(child.error_summary) == (
        "RuntimeError in stage12_calculation (baseline): "
        "could not reach postgresql://[REDACTED]@db.example/postgres"
    )
    assert "database-secret" not in child.error_summary
    [recorded_cause] = runtime.recorded_exceptions
    assert str(recorded_cause) == child.error_summary


def test_single_worker_names_the_failing_artifact_write_stage() -> None:
    class FailingSimulationArtifacts(FakeArtifacts):
        def write_simulation(self, **_kwargs):
            msg = "bucket write rejected"
            raise OSError(msg)

    store = FakeStore()
    simulation = planned_simulation(SimulationRole.BASELINE)
    store.children[simulation.simulation_execution_id] = child_record(simulation)

    with pytest.raises(RuntimeError, match="Stage 12 simulation execution failed"):
        run_single_simulation(
            simulation.model_dump(mode="json"),
            invocation_context().model_dump(mode="json"),
            required_country="us",
            dependencies=SimulationWorkerDependencies(
                store=store,
                artifacts=FailingSimulationArtifacts(),
                calculator=lambda _: simulation_frames(),
            ),
        )

    assert summary_failure_cause(
        store.children[simulation.simulation_execution_id].error_summary
    ) == (
        "OSError in stage12_simulation_artifact_write (baseline): bucket write rejected"
    )


def test_single_worker_preserves_typed_failure_values() -> None:
    store = FakeStore()
    simulation = planned_simulation(SimulationRole.BASELINE)
    store.children[simulation.simulation_execution_id] = child_record(simulation)

    def fail(_simulation):
        msg = "SPM_YEAR_UNAVAILABLE"
        raise SPMInputError(msg, "No SPM values for 2026")

    with pytest.raises(RuntimeError, match="Stage 12 simulation execution failed"):
        run_single_simulation(
            simulation.model_dump(mode="json"),
            invocation_context().model_dump(mode="json"),
            required_country="us",
            dependencies=SimulationWorkerDependencies(
                store=store,
                artifacts=FakeArtifacts(),
                calculator=fail,
            ),
        )

    child = store.children[simulation.simulation_execution_id]
    assert child.error_code == "SPM_YEAR_UNAVAILABLE"
    assert child.error_summary == "No SPM values for 2026"


def test_single_worker_preserves_the_dataset_year_code() -> None:
    store = FakeStore()
    simulation = planned_simulation(SimulationRole.BASELINE)
    store.children[simulation.simulation_execution_id] = child_record(simulation)

    def fail_with_dataset_year_error(_simulation):
        raise dataset_year_error()

    with pytest.raises(RuntimeError, match="Stage 12 simulation execution failed"):
        run_single_simulation(
            simulation.model_dump(mode="json"),
            invocation_context().model_dump(mode="json"),
            required_country="us",
            dependencies=SimulationWorkerDependencies(
                store=store,
                artifacts=FakeArtifacts(),
                calculator=fail_with_dataset_year_error,
            ),
        )

    child = store.children[simulation.simulation_execution_id]
    assert child.error_code == DATASET_YEAR_UNAVAILABLE
    assert child.error_summary == DATASET_YEAR_MESSAGE


def test_single_worker_rejects_bare_year_policy_period_before_calculation() -> None:
    store = FakeStore()
    simulation = planned_simulation(SimulationRole.BASELINE).model_copy(
        update={
            "policy": {"gov.irs.credits.ctc.amount.base[0].amount": {"2026": 3_000}}
        }
    )
    store.children[simulation.simulation_execution_id] = child_record(simulation)

    with pytest.raises(RuntimeError, match="Stage 12 simulation execution failed"):
        run_single_simulation(
            simulation.model_dump(mode="json"),
            invocation_context().model_dump(mode="json"),
            required_country="us",
            dependencies=SimulationWorkerDependencies(
                store=store,
                artifacts=FakeArtifacts(),
                calculator=lambda _: pytest.fail("calculation must not start"),
            ),
        )

    child = store.children[simulation.simulation_execution_id]
    assert child.error_code == "invalid_policy_period"
    assert child.error_summary == (
        "Policy parameter 'gov.irs.credits.ctc.amount.base[0].amount' has invalid "
        "effective period '2026'; expected YYYY-MM-DD or "
        "YYYY-MM-DD.YYYY-MM-DD."
    )


def test_single_worker_builds_its_store_with_the_worker_runtime(monkeypatch) -> None:
    store = FakeStore()
    simulation = planned_simulation(SimulationRole.BASELINE)
    store.children[simulation.simulation_execution_id] = child_record(simulation)
    store_runtimes = []

    def record_store_runtime(runtime):
        store_runtimes.append(runtime)
        return store

    monkeypatch.setattr(simulation_module, "runtime_store", record_store_runtime)
    runtime = RecordingRuntime()

    run_single_simulation(
        simulation.model_dump(mode="json"),
        invocation_context().model_dump(mode="json"),
        required_country="us",
        dependencies=SimulationWorkerDependencies(
            artifacts=FakeArtifacts(),
            calculator=lambda _: simulation_frames(),
            runtime=runtime,
        ),
    )

    # The runtime store reports each connection retry through this runtime.
    assert store_runtimes == [runtime]


def test_single_worker_reports_failure_before_an_unwritable_failure_record() -> None:
    ordered_events: list[tuple[str, object]] = []

    class UnwritableFailureStore(FakeStore):
        def replace_simulation_if_status(self, record, *, expected_status):
            if record.status is ComparisonRunLifecycleStatus.FAILED:
                ordered_events.append(("failure_write", record.error_summary))
                raise connection_limit_error()
            return super().replace_simulation_if_status(
                record, expected_status=expected_status
            )

    store = UnwritableFailureStore()
    simulation = planned_simulation(SimulationRole.BASELINE)
    store.children[simulation.simulation_execution_id] = child_record(simulation)
    calculation_error = ValueError("country column missing from household data")

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
                runtime=TelemetryRecordingRuntime(ordered_events),
            ),
        )

    original_report, failure_write, persistence_report = ordered_events
    assert original_report[0] == "telemetry"
    _message, attributes, reported_error = original_report[1]
    assert reported_error is calculation_error
    assert attributes == {
        "stage12_failure_scope": "simulation_execution",
        "simulation_role": "baseline",
        "evaluation_id": str(EVALUATION_ID),
        "simulation_execution_id": str(BASELINE_ID),
        "correlation_id": summary_correlation_id(failure_write[1]),
    }
    assert failure_write[0] == "failure_write"
    assert summary_failure_cause(failure_write[1]) == (
        "ValueError in stage12_calculation (baseline): "
        "country column missing from household data"
    )
    assert persistence_report[0] == "telemetry"
    assert persistence_report[1][0] == (
        "Stage 12 failure record could not be persisted"
    )
    assert isinstance(persistence_report[1][2], OperationalError)
    child = store.children[simulation.simulation_execution_id]
    assert child.status is ComparisonRunLifecycleStatus.RUNNING


def test_late_simulation_success_does_not_overwrite_parent_cancellation() -> None:
    store = FakeStore()
    simulation = planned_simulation(SimulationRole.BASELINE)
    store.children[simulation.simulation_execution_id] = child_record(simulation)

    def segmented_calculator(value, cancellation_requested):
        current = store.children[value.simulation_execution_id]
        cancelled_at = datetime.now(UTC)
        store.replace_simulation(
            current.model_copy(
                update={
                    "status": ComparisonRunLifecycleStatus.FAILED,
                    "error_code": "simulation_cancelled",
                    "error_summary": "Cancelled after the reform simulation failed.",
                    "updated_at": cancelled_at,
                    "completed_at": cancelled_at,
                }
            )
        )
        assert cancellation_requested() is True
        return SimulationCalculation(frames=simulation_frames())

    with pytest.raises(RuntimeError, match="Stage 12 simulation was cancelled"):
        run_single_simulation(
            simulation.model_dump(mode="json"),
            invocation_context().model_dump(mode="json"),
            required_country="us",
            dependencies=SimulationWorkerDependencies(
                store=store,
                artifacts=FakeArtifacts(),
                calculator=lambda _: pytest.fail("monolithic calculator was selected"),
                segmented_calculator=segmented_calculator,
            ),
        )

    child = store.children[simulation.simulation_execution_id]
    assert child.status is ComparisonRunLifecycleStatus.FAILED
    assert child.error_code == "simulation_cancelled"


def test_single_worker_rejects_frames_that_do_not_satisfy_the_output_plan() -> None:
    store = FakeStore()
    simulation = planned_simulation(SimulationRole.BASELINE)
    store.children[simulation.simulation_execution_id] = child_record(simulation)
    frames = simulation_frames()
    frames["person"] = frames["person"].drop(columns=["age"])

    with pytest.raises(RuntimeError, match="Stage 12 simulation execution failed"):
        run_single_simulation(
            simulation.model_dump(mode="json"),
            invocation_context().model_dump(mode="json"),
            required_country="us",
            dependencies=SimulationWorkerDependencies(
                store=store,
                artifacts=FakeArtifacts(),
                calculator=lambda _: frames,
            ),
        )

    assert store.children[simulation.simulation_execution_id].status is (
        ComparisonRunLifecycleStatus.FAILED
    )
