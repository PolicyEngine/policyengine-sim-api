"""Tests for reports and simulations whose result returns to their caller.

With ``result_delivery`` set to ``return_to_caller``, the report coordinator
and its simulation workers read and write no Stage 12 rows. They return typed
values, and a failure raises ``ReportExecutionError`` with the redacted detail
a Stage 12 row would record.
"""

from __future__ import annotations

from contextlib import contextmanager
from hashlib import sha256

import pytest
from policyengine_simulation_contract.report_execution import ReportExecutionError
from policyengine_simulation_contract.spm import SPMInputError
from policyengine_simulation_contract.stage12_execution import (
    ReportResultDelivery,
    SimulationArtifactDescriptor,
    SimulationRole,
    Stage12InvocationContext,
)
from policyengine_simulation_executor.stage12_runtime import (
    SimulationCalculation,
    SimulationWorkerDependencies,
    Stage12ExecutionError,
    Stage12FailureDetail,
    run_single_simulation,
)
from policyengine_simulation_executor.stage12_runtime import (
    coordination as coordination_module,
)
from policyengine_simulation_executor.stage12_runtime import (
    simulation as simulation_module,
)
from test_stage12_runtime import (
    BASELINE_ID,
    EVALUATION_ID,
    FakeArtifacts,
    _context,
    _failure_cause,
    _frames,
    _planned_simulation,
)


class StageRecordingRuntime:
    """Record the spans a run opens and the failure causes it attaches."""

    def __init__(self) -> None:
        self.spans: list[str] = []
        self.recorded_exceptions: list[Exception] = []

    @contextmanager
    def span(self, name, *_args, **_kwargs):
        self.spans.append(name)
        yield

    def capture_context(self):
        return {}

    def record_exception(self, error, *, handled, status_code=None):
        assert handled
        self.recorded_exceptions.append(error)


class NoDatabaseStore:
    """A store that fails the test on any use: a returned report writes no rows."""

    def __getattr__(self, name):
        pytest.fail(f"a returned report used the Stage 12 store ({name})")


@pytest.fixture
def forbid_database(monkeypatch):
    """Fail the test if a returned report builds a Stage 12 database store."""

    def refuse_store(*_args, **_kwargs):
        pytest.fail("a returned report built a Stage 12 database store")

    monkeypatch.setattr(simulation_module, "runtime_store", refuse_store)
    monkeypatch.setattr(coordination_module, "runtime_store", refuse_store)


def _returning_context() -> Stage12InvocationContext:
    return _context().model_copy(
        update={"result_delivery": ReportResultDelivery.RETURN_TO_CALLER}
    )


def _run_returned_simulation(simulation, **seams):
    seams.setdefault("artifacts", FakeArtifacts())
    return run_single_simulation(
        simulation.model_dump(mode="json"),
        _returning_context().model_dump(mode="json"),
        required_country="us",
        dependencies=SimulationWorkerDependencies(**seams),
    )


def test_returned_simulation_stores_its_artifact_without_the_database(
    forbid_database,
) -> None:
    simulation = _planned_simulation(SimulationRole.BASELINE)
    artifacts = FakeArtifacts()

    result = _run_returned_simulation(
        simulation,
        artifacts=artifacts,
        calculator=lambda _: _frames(),
    )

    descriptor = SimulationArtifactDescriptor.model_validate(result)
    assert descriptor.role is SimulationRole.BASELINE
    assert descriptor.simulation_execution_id == BASELINE_ID
    stored = artifacts.read(descriptor.artifact.uri)
    assert sha256(stored).hexdigest() == descriptor.artifact.content_sha256
    assert artifacts.input_writes == [
        (_context().artifact_prefix, SimulationRole.BASELINE)
    ]


def test_returned_simulation_never_uses_a_store_it_was_given() -> None:
    simulation = _planned_simulation(SimulationRole.BASELINE)

    result = _run_returned_simulation(
        simulation,
        store=NoDatabaseStore(),
        calculator=lambda _: _frames(),
    )

    assert result["role"] == "baseline"


def test_returned_simulation_times_the_same_stages_as_a_stage12_simulation(
    forbid_database,
) -> None:
    runtime = StageRecordingRuntime()

    _run_returned_simulation(
        _planned_simulation(SimulationRole.BASELINE),
        calculator=lambda _: _frames(),
        runtime=runtime,
    )

    assert runtime.spans == [
        "stage12_input_write",
        "stage12_calculation",
        "stage12_simulation_artifact_write",
    ]


def test_returned_simulation_failure_raises_its_redacted_detail(
    forbid_database,
) -> None:
    simulation = _planned_simulation(SimulationRole.BASELINE)
    runtime = StageRecordingRuntime()

    def fail(_simulation):
        msg = "could not reach postgresql://runtime:database-secret@db.example/postgres"
        raise RuntimeError(msg)

    with pytest.raises(ReportExecutionError) as raised:
        _run_returned_simulation(simulation, calculator=fail, runtime=runtime)

    failure = raised.value.failure
    assert failure.evaluation_id == EVALUATION_ID
    assert failure.error_code == "simulation_execution_failed"
    assert _failure_cause(failure.error_summary) == (
        "RuntimeError in stage12_calculation (baseline): "
        "could not reach postgresql://[REDACTED]@db.example/postgres"
    )
    assert str(raised.value) == "Report execution failed"
    # The original error, with its unredacted message, stays in telemetry.
    assert raised.value.__cause__ is None
    assert raised.value.__suppress_context__ is True
    [recorded_cause] = runtime.recorded_exceptions
    assert str(recorded_cause) == failure.error_summary


def test_returned_simulation_keeps_a_typed_failure(forbid_database) -> None:
    def fail(_simulation):
        msg = "SPM_YEAR_UNAVAILABLE"
        raise SPMInputError(msg, "No SPM values for 2026")

    with pytest.raises(ReportExecutionError) as raised:
        _run_returned_simulation(
            _planned_simulation(SimulationRole.BASELINE), calculator=fail
        )

    assert raised.value.failure.error_code == "SPM_YEAR_UNAVAILABLE"
    assert raised.value.failure.error_summary == "No SPM values for 2026"


def test_returned_simulation_rejects_an_invalid_policy_period_before_calculating(
    forbid_database,
) -> None:
    simulation = _planned_simulation(SimulationRole.BASELINE).model_copy(
        update={
            "policy": {"gov.irs.credits.ctc.amount.base[0].amount": {"2026": 3_000}}
        }
    )

    with pytest.raises(ReportExecutionError) as raised:
        _run_returned_simulation(
            simulation,
            calculator=lambda _: pytest.fail("calculation must not start"),
        )

    assert raised.value.failure.error_code == "invalid_policy_period"
    assert raised.value.failure.error_summary == (
        "Policy parameter 'gov.irs.credits.ctc.amount.base[0].amount' has invalid "
        "effective period '2026'; expected YYYY-MM-DD or "
        "YYYY-MM-DD.YYYY-MM-DD."
    )


def test_returned_segmented_simulation_is_never_told_to_stop(
    forbid_database,
) -> None:
    # No record can carry a cancellation, so the coordinator stops this call
    # through Modal instead.
    simulation = _planned_simulation(SimulationRole.BASELINE)
    cancellation_answers = []

    def segmented_calculator(planned, cancellation_requested):
        cancellation_answers.append((planned, cancellation_requested()))
        return SimulationCalculation(frames=_frames())

    _run_returned_simulation(
        simulation,
        calculator=lambda _: pytest.fail("the unsegmented calculator was selected"),
        segmented_calculator=segmented_calculator,
    )

    assert cancellation_answers == [(simulation, False)]


def test_returned_segmented_simulation_keeps_the_segment_failure(
    forbid_database,
) -> None:
    segment_failure = Stage12FailureDetail(
        error_code="segment_execution_failed",
        error_summary="Segment 3: MemoryError in stage12_calculation",
    )

    def segmented_calculator(_planned, _cancellation_requested):
        raise Stage12ExecutionError(segment_failure)

    with pytest.raises(ReportExecutionError) as raised:
        _run_returned_simulation(
            _planned_simulation(SimulationRole.BASELINE),
            calculator=lambda _: pytest.fail("the unsegmented calculator was selected"),
            segmented_calculator=segmented_calculator,
        )

    assert raised.value.failure.error_code == "segment_execution_failed"
    assert raised.value.failure.error_summary == (
        "Segment 3: MemoryError in stage12_calculation"
    )
