"""Tests for reports and simulations whose result returns to their caller.

With ``result_delivery`` set to ``return_to_caller``, the report coordinator
and its simulation workers read and write no Stage 12 rows. They return typed
values, and a failure raises ``ReportExecutionError`` with the redacted detail
a Stage 12 row would record.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from hashlib import sha256
from uuid import uuid4

import pytest
from fixtures.macro_output import CURRENT_SINGLE_YEAR_MACRO_RESULT
from policyengine_simulation_contract.macro_output import SingleYearMacroOutput
from policyengine_simulation_contract.report_execution import (
    ReportExecutionError,
    ReportExecutionFailure,
    ReportExecutionProvenance,
    ReportExecutionResult,
    report_aggregate_sha256,
)
from policyengine_simulation_contract.spm import SPMInputError
from policyengine_simulation_contract.stage12_execution import (
    ReportAggregate,
    ReportResultDelivery,
    SimulationArtifactDescriptor,
    SimulationRole,
    Stage12InvocationContext,
)
from policyengine_simulation_executor.stage12_runtime import (
    CoordinatorDependencies,
    CoordinatorInvocation,
    SimulationCalculation,
    SimulationWorkerDependencies,
    Stage12ExecutionError,
    Stage12FailureDetail,
    coordinate_report_for_caller,
    run_single_simulation,
)
from policyengine_simulation_executor.stage12_runtime import (
    coordination as coordination_module,
)
from policyengine_simulation_executor.stage12_runtime import (
    simulation as simulation_module,
)
from policyengine_simulation_observability.stages import (
    STAGE12_CANONICAL_REPORT_STAGES,
    Stage,
)
from test_stage12_runtime import (
    BASELINE_ID,
    EVALUATION_ID,
    REFORM_ID,
    ConcurrentInvoker,
    ContextRecordingInvoker,
    FakeArtifacts,
    FakeStore,
    InjectedChildFaults,
    ReformDispatchFailureInvoker,
    ScriptedChildInvoker,
    _aggregate_payload,
    _assert_log_identifies_child,
    _bundle,
    _context,
    _coordination_log,
    _failure_cause,
    _frames,
    _output_plan,
    _parent,
    _planned_simulation,
    _report,
    _report_with_segmentation,
    coordinate_report,
)


class StageRecordingRuntime:
    """Record the stages a run opens and the failure causes it attaches."""

    def __init__(self) -> None:
        self.spans: list[str] = []
        self.operations: list[str] = []
        self.recorded_exceptions: list[Exception] = []

    @contextmanager
    def span(self, name, *_args, **_kwargs):
        self.spans.append(name)
        yield

    @contextmanager
    def operation(self, name, *_args, **_kwargs):
        self.operations.append(name)
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


def _macro_aggregate(**_inputs):
    return _aggregate_payload(CURRENT_SINGLE_YEAR_MACRO_RESULT)


def _coordinate_returned_report(invoker, *, context=None, report=None, **seams):
    context = context or _returning_context()
    seams.setdefault("store", NoDatabaseStore())
    seams.setdefault("artifacts", FakeArtifacts())
    seams.setdefault("aggregator", _macro_aggregate)
    seams.setdefault("output_plan_resolver", lambda _: _output_plan())
    return coordinate_report_for_caller(
        (report or _report()).model_dump(mode="json"),
        context.model_dump(mode="json"),
        invocation=CoordinatorInvocation(
            application_name=context.modal_application,
            invocation_id="coordinator-1",
        ),
        dependencies=CoordinatorDependencies(invoker=invoker, **seams),
    )


def test_returned_report_returns_its_typed_result_without_the_database(
    forbid_database,
) -> None:
    artifacts = FakeArtifacts()
    invoker = ContextRecordingInvoker(artifacts)
    aggregator_inputs = {}

    def aggregate(**inputs):
        aggregator_inputs.update(inputs)
        return _macro_aggregate()

    # The coordinator, not the caller, chooses where the artifacts go.
    caller_context = _returning_context().model_copy(
        update={"artifact_prefix": f"caller-chosen/{EVALUATION_ID}"}
    )

    returned = _coordinate_returned_report(
        invoker,
        context=caller_context,
        artifacts=artifacts,
        aggregator=aggregate,
    )

    result = ReportExecutionResult.model_validate(returned)
    assert returned == result.model_dump(mode="json")
    assert result.evaluation_id == EVALUATION_ID
    assert result.requested_aggregates == tuple(ReportAggregate)
    assert result.aggregate == SingleYearMacroOutput.model_validate(
        CURRENT_SINGLE_YEAR_MACRO_RESULT
    )
    assert result.aggregate_sha256 == report_aggregate_sha256(result.aggregate)
    assert result.output_plan == _output_plan()
    assert result.baseline.simulation_execution_id == BASELINE_ID
    assert result.reform.simulation_execution_id == REFORM_ID
    for descriptor in (result.baseline, result.reform):
        stored = artifacts.read(descriptor.artifact.uri)
        assert sha256(stored).hexdigest() == descriptor.artifact.content_sha256
    assert result.provenance == ReportExecutionProvenance(
        bundle=_bundle(),
        worker_version="5.2.0",
        modal_environment="staging",
        modal_application="policyengine-simulation-v2-py5-2-0",
        simulation_callable="run_single_simulation_us",
        version_manifest_sha256="b" * 64,
        coordinator_invocation_id="coordinator-1",
    )
    assert result.completed_at.tzinfo is not None
    household_income = {
        role: aggregator_inputs[role]
        .frames["household"]["household_net_income"]
        .tolist()
        for role in ("baseline", "reform")
    }
    assert household_income == {"baseline": [100.0], "reform": [120.0]}
    # The aggregate is returned inline, never written as an artifact.
    assert artifacts.aggregate_writes == []
    assert invoker.events == ["spawn:baseline", "spawn:reform"]
    latest_start = max(interval[0] for interval in invoker.intervals.values())
    earliest_end = min(interval[1] for interval in invoker.intervals.values())
    assert latest_start < earliest_end
    assert [context["result_delivery"] for context in invoker.child_contexts] == [
        "return_to_caller",
        "return_to_caller",
    ]
    assert [context["artifact_prefix"] for context in invoker.child_contexts] == [
        f"stage-12-runs/staging/2026/09/{EVALUATION_ID}",
        f"stage-12-runs/staging/2026/09/{EVALUATION_ID}",
    ]


def test_returned_report_runs_only_the_stages_it_needs(forbid_database) -> None:
    runtime = StageRecordingRuntime()
    artifacts = FakeArtifacts()

    _coordinate_returned_report(
        ConcurrentInvoker(artifacts), artifacts=artifacts, runtime=runtime
    )

    stage_name = STAGE12_CANONICAL_REPORT_STAGES.name
    # No claim, child state or aggregate artifact stages: nothing is stored.
    assert runtime.operations == [
        stage_name(Stage.STAGE12_COORDINATOR_PREPARATION),
        stage_name(Stage.STAGE12_OUTPUT_PLANNING),
        stage_name(Stage.STAGE12_CHILD_INPUT_PLANNING),
        stage_name(Stage.STAGE12_CHILD_INPUT_PLANNING),
    ]
    assert runtime.spans == [
        stage_name(Stage.STAGE12_CHILD_DISPATCH),
        stage_name(Stage.STAGE12_CHILD_DISPATCH),
        stage_name(Stage.STAGE12_CHILD_WAIT),
        stage_name(Stage.STAGE12_ARTIFACT_READ),
        stage_name(Stage.STAGE12_AGGREGATION),
    ]


def test_returned_report_claims_nothing_so_a_repeated_dispatch_runs_again(
    forbid_database,
) -> None:
    # The caller deduplicates; the coordinator keeps no record of the report.
    artifacts = FakeArtifacts()
    first_invoker = ConcurrentInvoker(artifacts)
    repeated_invoker = ConcurrentInvoker(artifacts)

    first = _coordinate_returned_report(first_invoker, artifacts=artifacts)
    repeated = _coordinate_returned_report(repeated_invoker, artifacts=artifacts)

    assert first_invoker.events == ["spawn:baseline", "spawn:reform"]
    assert repeated_invoker.events == ["spawn:baseline", "spawn:reform"]
    assert repeated["aggregate"] == first["aggregate"]


def test_returned_report_checks_the_dispatch_context_before_starting(
    forbid_database,
) -> None:
    invoker = ConcurrentInvoker(FakeArtifacts())

    with pytest.raises(ValueError, match="application differs"):
        coordinate_report_for_caller(
            _report().model_dump(mode="json"),
            _returning_context().model_dump(mode="json"),
            invocation=CoordinatorInvocation(
                application_name="policyengine-simulation-v2-py9-9-9",
                invocation_id="coordinator-1",
            ),
            dependencies=CoordinatorDependencies(
                store=NoDatabaseStore(),
                artifacts=FakeArtifacts(),
                invoker=invoker,
            ),
        )

    assert invoker.events == []


def test_returned_report_coordinator_refuses_a_stage12_context(
    forbid_database,
) -> None:
    invoker = ConcurrentInvoker(FakeArtifacts())

    with pytest.raises(ValueError, match="return_to_caller"):
        _coordinate_returned_report(invoker, context=_context())

    assert invoker.events == []


def test_stage12_coordinator_refuses_a_returned_report_context() -> None:
    store = FakeStore()
    invoker = ConcurrentInvoker(FakeArtifacts())

    with pytest.raises(ValueError, match="coordinate_report_for_caller"):
        coordinate_report(
            _report().model_dump(mode="json"),
            _returning_context().model_dump(mode="json"),
            _parent().model_dump(mode="json"),
            application_name=_context().modal_application,
            coordinator_invocation_id="coordinator-1",
            store=store,
            artifacts=FakeArtifacts(),
            invoker=invoker,
            aggregator=_macro_aggregate,
        )

    assert store.parent is None
    assert invoker.events == []


@pytest.mark.parametrize("segmented", [True, False])
def test_returned_report_keeps_the_failed_childs_detail_and_stops_its_peer(
    forbid_database,
    segmented,
) -> None:
    child_failure = ReportExecutionFailure(
        evaluation_id=EVALUATION_ID,
        error_code="SPM_YEAR_UNAVAILABLE",
        error_summary="No SPM values for 2026",
    )
    invoker = ScriptedChildInvoker(
        wait_errors={"baseline": ReportExecutionError(child_failure)}
    )
    runtime = StageRecordingRuntime()

    with pytest.raises(ReportExecutionError) as raised:
        _coordinate_returned_report(
            invoker,
            report=_report_with_segmentation(segmented=segmented),
            runtime=runtime,
        )

    assert raised.value.failure == child_failure
    assert raised.value.__cause__ is None
    assert raised.value.__suppress_context__ is True
    # No record can tell a child to stop, segmented or not, so Modal cancels it.
    assert invoker.calls["reform"].cancel_count == 1
    assert invoker.calls["baseline"].cancel_count == 0
    [recorded_cause] = runtime.recorded_exceptions
    assert str(recorded_cause) == "No SPM values for 2026"


def test_returned_report_redacts_a_child_call_failure_without_detail(
    forbid_database,
) -> None:
    invoker = ScriptedChildInvoker(
        wait_errors={"reform": LookupError("worker lost with token=opaque-token")}
    )

    with pytest.raises(ReportExecutionError) as raised:
        _coordinate_returned_report(invoker)

    failure = raised.value.failure
    assert failure.evaluation_id == EVALUATION_ID
    assert failure.error_code == "simulation_invocation_failed"
    assert _failure_cause(failure.error_summary) == (
        "LookupError in stage12_child_wait (reform): worker lost with token=[REDACTED]"
    )
    assert invoker.calls["baseline"].cancel_count == 1
    assert invoker.calls["reform"].cancel_count == 0


@pytest.mark.parametrize("segmented", [True, False])
def test_returned_report_wait_timeout_stops_every_child_through_modal(
    forbid_database,
    segmented,
) -> None:
    invoker = ScriptedChildInvoker()

    with pytest.raises(ReportExecutionError) as raised:
        _coordinate_returned_report(
            invoker,
            report=_report_with_segmentation(segmented=segmented),
            wait_timeout_seconds=0,
        )

    assert raised.value.failure.error_code == "simulation_wait_timeout"
    assert raised.value.failure.error_summary == (
        "Stage 12 simulations did not finish within 0 seconds of the report "
        "coordinator starting."
    )
    assert [call.cancel_count for call in invoker.calls.values()] == [1, 1]


def test_returned_report_dispatch_failure_stops_the_started_child(
    forbid_database,
) -> None:
    invoker = ReformDispatchFailureInvoker()

    with pytest.raises(ReportExecutionError) as raised:
        _coordinate_returned_report(invoker)

    failure = raised.value.failure
    assert failure.error_code == "simulation_dispatch_failed"
    assert _failure_cause(failure.error_summary) == (
        "ConnectionError in stage12_child_dispatch (reform): "
        "dispatch contains sensitive details"
    )
    assert invoker.events == ["spawn:baseline", "spawn:reform"]
    assert invoker.calls["baseline"].cancel_count == 1


def test_returned_report_cancellation_error_does_not_replace_the_failure(
    forbid_database,
    caplog,
) -> None:
    invoker = ScriptedChildInvoker(
        wait_errors={"reform": LookupError("reform worker failed")},
        cancel_errors={"baseline": ConnectionError("cancel contains sensitive data")},
    )

    with (
        caplog.at_level(logging.WARNING, logger=coordination_module.__name__),
        pytest.raises(ReportExecutionError) as raised,
    ):
        _coordinate_returned_report(invoker)

    assert raised.value.failure.error_code == "simulation_invocation_failed"
    assert invoker.calls["baseline"].cancel_count == 1
    _assert_log_identifies_child(
        _coordination_log(caplog, logging.WARNING),
        role="baseline",
        error_type="ConnectionError",
    )


def test_returned_report_aggregation_failure_is_redacted_and_reported_once(
    forbid_database,
) -> None:
    runtime = StageRecordingRuntime()
    artifacts = FakeArtifacts()

    def fail(**_inputs):
        msg = "aggregation read postgresql://runtime:database-secret@db.example/x"
        raise RuntimeError(msg)

    with pytest.raises(ReportExecutionError) as raised:
        _coordinate_returned_report(
            ConcurrentInvoker(artifacts),
            artifacts=artifacts,
            aggregator=fail,
            runtime=runtime,
        )

    failure = raised.value.failure
    assert failure.error_code == "report_coordination_failed"
    assert _failure_cause(failure.error_summary) == (
        "RuntimeError in stage12_aggregation: "
        "aggregation read postgresql://[REDACTED]@db.example/x"
    )
    [recorded_cause] = runtime.recorded_exceptions
    assert str(recorded_cause) == failure.error_summary


def test_returned_report_rejects_an_aggregate_outside_the_macro_output_schema(
    forbid_database,
) -> None:
    artifacts = FakeArtifacts()

    with pytest.raises(ReportExecutionError) as raised:
        _coordinate_returned_report(
            ConcurrentInvoker(artifacts),
            artifacts=artifacts,
            aggregator=lambda **_: _aggregate_payload({"budget": {}}),
        )

    failure = raised.value.failure
    assert failure.error_code == "report_coordination_failed"
    assert _failure_cause(failure.error_summary).startswith(
        "ValidationError in stage12_aggregation: "
    )
    assert artifacts.aggregate_writes == []


def test_returned_report_rejects_an_aggregate_for_another_report(
    forbid_database,
) -> None:
    artifacts = FakeArtifacts()

    def aggregate_for_another_report(**_inputs):
        return {**_macro_aggregate(), "evaluation_id": str(uuid4())}

    with pytest.raises(ReportExecutionError) as raised:
        _coordinate_returned_report(
            ConcurrentInvoker(artifacts),
            artifacts=artifacts,
            aggregator=aggregate_for_another_report,
        )

    assert raised.value.failure.error_code == "report_coordination_failed"
    assert "aggregate does not describe this report" in (
        raised.value.failure.error_summary
    )


def test_returned_report_rejects_artifacts_that_do_not_follow_the_plan(
    forbid_database,
) -> None:
    artifacts = FakeArtifacts()

    with pytest.raises(ReportExecutionError) as raised:
        _coordinate_returned_report(
            ConcurrentInvoker(
                artifacts, faults=InjectedChildFaults(wrong_plan_digest=True)
            ),
            artifacts=artifacts,
            aggregator=lambda **_: pytest.fail("aggregation must not start"),
        )

    assert raised.value.failure.error_code == "report_coordination_failed"
    assert "do not satisfy the report output plan" in (
        raised.value.failure.error_summary
    )
