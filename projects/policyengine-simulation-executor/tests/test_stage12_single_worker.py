"""Tests for a Stage 12 single-simulation worker's successful and refused runs."""

from __future__ import annotations

import logging

import pytest
from policyengine_simulation_contract.stage12_execution import (
    ComparisonRunLifecycleStatus,
    SimulationRole,
)
from policyengine_simulation_executor.stage12_runtime import (
    SimulationCalculation,
    SimulationWorkerDependencies,
    run_single_simulation,
)
from stage12_fake_stores import FakeArtifacts, FakeStore, simulation_frames
from stage12_runtime_support import (
    child_record,
    invocation_context,
    planned_simulation,
    spm_calculation_provenance,
)


def test_single_worker_accepts_one_policy_and_persists_one_artifact() -> None:
    store = FakeStore()
    simulation = planned_simulation(SimulationRole.BASELINE)
    store.children[simulation.simulation_execution_id] = child_record(simulation)
    artifacts = FakeArtifacts()

    result = run_single_simulation(
        simulation.model_dump(mode="json"),
        invocation_context().model_dump(mode="json"),
        required_country="us",
        dependencies=SimulationWorkerDependencies(
            store=store,
            artifacts=artifacts,
            calculator=lambda _: simulation_frames(),
        ),
    )

    assert result["role"] == "baseline"
    child = store.children[simulation.simulation_execution_id]
    assert child.status is ComparisonRunLifecycleStatus.SUCCEEDED
    assert child.output_uri == result["artifact"]["uri"]
    assert artifacts.input_writes == [
        (invocation_context().artifact_prefix, SimulationRole.BASELINE)
    ]


def test_single_worker_routes_eligible_us_calculation_to_segment_runner() -> None:
    store = FakeStore()
    simulation = planned_simulation(SimulationRole.BASELINE)
    store.children[simulation.simulation_execution_id] = child_record(simulation)
    received = []

    def segmented_calculator(value, cancellation_requested):
        received.append(value)
        assert cancellation_requested() is False
        return SimulationCalculation(frames=simulation_frames())

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

    assert received == [simulation]


def test_single_worker_honors_explicit_segmentation_opt_out() -> None:
    store = FakeStore()
    simulation = planned_simulation(SimulationRole.BASELINE).model_copy(
        update={"options": {"segmented": False}}
    )
    store.children[simulation.simulation_execution_id] = child_record(simulation)
    segmented_calls = []

    def unexpected_segmented(value, cancellation_requested):
        segmented_calls.append(value)
        return SimulationCalculation(frames=simulation_frames())

    run_single_simulation(
        simulation.model_dump(mode="json"),
        invocation_context().model_dump(mode="json"),
        required_country="us",
        dependencies=SimulationWorkerDependencies(
            store=store,
            artifacts=FakeArtifacts(),
            calculator=lambda _: simulation_frames(),
            segmented_calculator=unexpected_segmented,
        ),
    )

    assert segmented_calls == []


def test_single_worker_refuses_a_simulation_that_is_no_longer_active() -> None:
    store = FakeStore()
    simulation = planned_simulation(SimulationRole.BASELINE)
    store.children[simulation.simulation_execution_id] = child_record(
        simulation
    ).model_copy(update={"status": ComparisonRunLifecycleStatus.FAILED})
    artifacts = FakeArtifacts()

    with pytest.raises(
        RuntimeError, match=r"^Stage 12 simulation is no longer active$"
    ):
        run_single_simulation(
            simulation.model_dump(mode="json"),
            invocation_context().model_dump(mode="json"),
            required_country="us",
            dependencies=SimulationWorkerDependencies(
                store=store,
                artifacts=artifacts,
                calculator=lambda _: pytest.fail("a finished simulation ran again"),
            ),
        )

    assert artifacts.input_writes == []


def test_single_worker_stops_when_another_writer_claimed_the_simulation() -> None:
    class ClaimLosingStore(FakeStore):
        def replace_simulation_if_status(self, record, *, expected_status):
            return self.children[record.simulation_execution_id], False

    store = ClaimLosingStore()
    simulation = planned_simulation(SimulationRole.BASELINE)
    store.children[simulation.simulation_execution_id] = child_record(simulation)
    artifacts = FakeArtifacts()

    with pytest.raises(RuntimeError, match=r"^Stage 12 simulation was cancelled$"):
        run_single_simulation(
            simulation.model_dump(mode="json"),
            invocation_context().model_dump(mode="json"),
            required_country="us",
            dependencies=SimulationWorkerDependencies(
                store=store,
                artifacts=artifacts,
                calculator=lambda _: pytest.fail("an unclaimed simulation ran"),
            ),
        )

    assert artifacts.input_writes == []


def test_single_worker_treats_a_failed_cancellation_check_as_not_cancelled(
    caplog,
) -> None:
    class StatusReadFailingStore(FakeStore):
        def __init__(self):
            super().__init__()
            self.fail_reads = False

        def get_simulation(self, simulation_execution_id):
            if self.fail_reads:
                msg = "status read contains sensitive details"
                raise ConnectionError(msg)
            return super().get_simulation(simulation_execution_id)

    store = StatusReadFailingStore()
    simulation = planned_simulation(SimulationRole.BASELINE)
    store.children[simulation.simulation_execution_id] = child_record(simulation)
    checks = []

    def segmented_calculator(_value, cancellation_requested):
        store.fail_reads = True
        checks.append(cancellation_requested())
        store.fail_reads = False
        return SimulationCalculation(frames=simulation_frames())

    with caplog.at_level(
        logging.WARNING,
        logger="policyengine_simulation_executor.stage12_runtime.simulation",
    ):
        run_single_simulation(
            simulation.model_dump(mode="json"),
            invocation_context().model_dump(mode="json"),
            required_country="us",
            dependencies=SimulationWorkerDependencies(
                store=store,
                artifacts=FakeArtifacts(),
                segmented_calculator=segmented_calculator,
            ),
        )

    assert checks == [False]
    assert (
        f"Stage 12 cancellation check failed for "
        f"{simulation.simulation_execution_id} (ConnectionError)"
    ) in caplog.text
    assert "sensitive" not in caplog.text
    assert (
        store.children[simulation.simulation_execution_id].status
        is ComparisonRunLifecycleStatus.SUCCEEDED
    )


def test_single_worker_retains_detached_calculation_provenance() -> None:
    store = FakeStore()
    simulation = planned_simulation(SimulationRole.BASELINE)
    store.children[simulation.simulation_execution_id] = child_record(simulation)
    artifacts = FakeArtifacts()
    provenance = spm_calculation_provenance()

    result = run_single_simulation(
        simulation.model_dump(mode="json"),
        invocation_context().model_dump(mode="json"),
        required_country="us",
        dependencies=SimulationWorkerDependencies(
            store=store,
            artifacts=artifacts,
            calculator=lambda _: SimulationCalculation(
                frames=simulation_frames(),
                calculation_provenance=provenance,
            ),
        ),
    )

    assert result["calculation_provenance"] == provenance.model_dump(
        mode="json", by_alias=True
    )
    assert "spm_config" not in result["calculation_provenance"]
