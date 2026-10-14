"""Characterization tests for the Stage 12 single-simulation worker's phases."""

from __future__ import annotations

from contextlib import nullcontext

from policyengine_simulation_contract.stage12_execution import (
    ComparisonRunLifecycleStatus,
    SimulationRole,
)
from policyengine_simulation_executor.stage12_runtime import (
    run_single_simulation,
)
from policyengine_simulation_executor.stage12_runtime import (
    simulation as simulation_module,
)
from policyengine_simulation_executor.stage12_runtime.failure_causes import (
    StageProgress,
)
from test_stage12_runtime import (
    NOW,
    FakeArtifacts,
    FakeStore,
    _child,
    _context,
    _frames,
    _planned_simulation,
)


class SpanRecordingRuntime:
    def __init__(self) -> None:
        self.spans: list[str] = []

    def span(self, name, *_args, **_kwargs):
        self.spans.append(name)
        return nullcontext()


def _run(store, artifacts, *, calculator=None, runtime=None):
    simulation = _planned_simulation(SimulationRole.BASELINE)
    seams = {"store": store, "artifacts": artifacts, "runtime": runtime}
    if calculator is not None:
        seams["calculator"] = calculator
    return run_single_simulation(
        simulation.model_dump(mode="json"),
        _context().model_dump(mode="json"),
        required_country="us",
        **seams,
    )


def test_succeeded_child_returns_its_stored_descriptor_without_recalculating():
    store = FakeStore()
    artifacts = FakeArtifacts()
    simulation = _planned_simulation(SimulationRole.BASELINE)
    descriptor = artifacts.add_simulation(simulation)
    succeeded = _child(simulation).model_copy(
        update={
            "status": ComparisonRunLifecycleStatus.SUCCEEDED,
            "output_uri": descriptor.artifact.uri,
            "output_sha256": descriptor.artifact.content_sha256,
            "output_schema_version": 1,
            "row_identity_columns": descriptor.row_identity.identifier_columns,
            "row_count": descriptor.row_identity.row_count,
            "row_identity_sha256": descriptor.row_identity.identity_sha256,
            "completed_at": NOW,
        }
    )
    store.children[simulation.simulation_execution_id] = succeeded
    calculations = []

    result = _run(store, artifacts, calculator=calculations.append)

    assert calculations == []
    assert artifacts.input_writes == []
    assert store.children[simulation.simulation_execution_id] == succeeded
    assert result == simulation_module.descriptor_from_record(
        succeeded, simulation
    ).model_dump(mode="json")


def test_default_calculator_receives_the_runtime_and_times_itself(monkeypatch):
    store = FakeStore()
    simulation = _planned_simulation(SimulationRole.BASELINE)
    store.children[simulation.simulation_execution_id] = _child(simulation)
    runtime = SpanRecordingRuntime()
    calls = []

    def calculate(planned, *, runtime=None, progress=None):
        calls.append((planned.role, runtime, progress))
        return _frames()

    monkeypatch.setattr(simulation_module, "calculate_simulation_frames", calculate)

    _run(store, FakeArtifacts(), calculator=calculate, runtime=runtime)

    [(role, received_runtime, received_progress)] = calls
    assert role is SimulationRole.BASELINE
    assert received_runtime is runtime
    # The worker's own stage tracker, so a failure names the calculation stage.
    assert isinstance(received_progress, StageProgress)
    assert received_progress.process_role == "baseline"
    assert runtime.spans == [
        "stage12_input_write",
        "stage12_simulation_artifact_write",
    ]


def test_injected_calculator_is_timed_by_the_worker():
    store = FakeStore()
    simulation = _planned_simulation(SimulationRole.BASELINE)
    store.children[simulation.simulation_execution_id] = _child(simulation)
    runtime = SpanRecordingRuntime()
    calls = []

    def calculate(planned):
        calls.append(planned.role)
        return _frames()

    _run(store, FakeArtifacts(), calculator=calculate, runtime=runtime)

    assert calls == [SimulationRole.BASELINE]
    assert runtime.spans == [
        "stage12_input_write",
        "stage12_calculation",
        "stage12_simulation_artifact_write",
    ]
