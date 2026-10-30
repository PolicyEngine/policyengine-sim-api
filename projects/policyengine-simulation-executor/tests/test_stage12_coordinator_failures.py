"""Tests for how the Stage 12 coordinator handles a failing child or artifact."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import override

import pytest
from policyengine_simulation_contract.spm import SPMComparisonProvenance
from policyengine_simulation_contract.stage12_execution import (
    ComparisonRunLifecycleStatus,
    PlannedSimulationExecutionInput,
    SimulationRole,
)
from policyengine_simulation_executor.stage12_runtime import (
    coordination as coordination_module,
)
from sqlalchemy.exc import OperationalError
from stage12_child_call_support import (
    ConcurrentInvoker,
    ImmediateCall,
    InjectedChildFaults,
    TimeoutCall,
)
from stage12_fake_stores import FakeArtifacts, FakeStore, simulation_frames
from stage12_runtime_support import (
    BASELINE_ID,
    NOW,
    REFORM_ID,
    RecordingRuntime,
    aggregate_payload,
    coordinate_report,
    invocation_context,
    parent_record,
    report_execution_input,
    summary_failure_cause,
)


def test_coordinator_never_writes_partial_aggregate_when_a_child_fails() -> None:
    store = FakeStore()
    artifacts = FakeArtifacts()
    invoker = ConcurrentInvoker(
        artifacts, faults=InjectedChildFaults(fail_role="reform")
    )

    with pytest.raises(
        RuntimeError, match="Stage 12 report coordination failed"
    ) as error:
        coordinate_report(
            report_execution_input().model_dump(mode="json"),
            invocation_context().model_dump(mode="json"),
            parent_record().model_dump(mode="json"),
            application_name=invocation_context().modal_application,
            coordinator_invocation_id="coordinator-1",
            store=store,
            artifacts=artifacts,
            invoker=invoker,
            aggregator=lambda **_: aggregate_payload({"must": "not run"}),
        )

    assert "child calculation failed" not in str(error.value)

    assert artifacts.aggregate_writes == []
    assert store.parent.status is ComparisonRunLifecycleStatus.FAILED
    assert summary_failure_cause(store.parent.error_summary) == (
        "RuntimeError in stage12_child_wait (reform): child calculation failed"
    )
    failed_child = store.children[REFORM_ID]
    assert failed_child.status is ComparisonRunLifecycleStatus.FAILED
    assert failed_child.error_code == "simulation_invocation_failed"
    assert failed_child.error_summary == store.parent.error_summary


@pytest.mark.parametrize(
    ("failed_role", "failed_id", "pending_role", "pending_id"),
    [
        ("reform", REFORM_ID, "baseline", BASELINE_ID),
        ("baseline", BASELINE_ID, "reform", REFORM_ID),
    ],
)
def test_coordinator_observes_one_role_failure_while_the_peer_is_pending(
    failed_role,
    failed_id,
    pending_role,
    pending_id,
) -> None:
    store = FakeStore()
    artifacts = FakeArtifacts()

    class PendingCall:
        object_id = f"call-{pending_role}"

        def __init__(self):
            self.timeouts = []
            self.cancel_count = 0

        def get(self, *, timeout=None):
            self.timeouts.append(timeout)
            raise TimeoutError

        def cancel(self):
            self.cancel_count += 1

    class FailedCall:
        object_id = f"call-{failed_role}"

        def __init__(self):
            self.timeouts = []

        def get(self, *, timeout=None):
            self.timeouts.append(timeout)
            child = store.children[failed_id]
            failed_at = datetime.now(UTC)
            store.replace_simulation(
                child.model_copy(
                    update={
                        "status": ComparisonRunLifecycleStatus.FAILED,
                        "error_code": "invalid_policy_period",
                        "error_summary": "The reform period is invalid.",
                        "updated_at": failed_at,
                        "completed_at": failed_at,
                    }
                )
            )
            msg = "remote wrapper"
            raise RuntimeError(msg)

        def cancel(self):
            return None

    baseline_call = PendingCall()
    failed_call = FailedCall()

    class ConcurrentFailureInvoker:
        def spawn(self, request):
            role = PlannedSimulationExecutionInput.model_validate(
                request.simulation
            ).role
            return failed_call if role.value == failed_role else baseline_call

        def restore(self, invocation_id):
            msg = f"unexpected restored invocation {invocation_id}"
            raise AssertionError(msg)

    with pytest.raises(RuntimeError, match="Stage 12 report coordination failed"):
        coordinate_report(
            report_execution_input().model_dump(mode="json"),
            invocation_context().model_dump(mode="json"),
            parent_record().model_dump(mode="json"),
            application_name=invocation_context().modal_application,
            coordinator_invocation_id="coordinator-1",
            store=store,
            artifacts=artifacts,
            invoker=ConcurrentFailureInvoker(),
            aggregator=lambda **_: aggregate_payload({"must": "not run"}),
            sleep=lambda _: pytest.fail("failure should be observed before sleeping"),
        )

    assert failed_call.timeouts == [0]
    assert baseline_call.timeouts == ([0] if pending_role == "baseline" else [])
    # A segmented peer reads its cancelled record and stops its own
    # region-group calls, so it is not cancelled through Modal.
    assert baseline_call.cancel_count == 0
    pending = store.children[pending_id]
    assert pending.status is ComparisonRunLifecycleStatus.FAILED
    assert pending.error_code == "simulation_cancelled"
    assert pending.error_summary == (
        f"Cancelled after the {failed_role} simulation failed."
    )
    assert store.parent.error_code == "invalid_policy_period"
    assert store.parent.error_summary == "The reform period is invalid."
    assert artifacts.aggregate_writes == []


def test_dispatch_failure_cancels_an_already_started_peer() -> None:
    store = FakeStore()
    baseline_call = TimeoutCall("call-baseline")

    class DispatchFailureInvoker:
        def spawn(self, request):
            role = PlannedSimulationExecutionInput.model_validate(
                request.simulation
            ).role
            if role is SimulationRole.REFORM:
                msg = "dispatch contains sensitive details"
                raise ConnectionError(msg)
            return baseline_call

        def restore(self, invocation_id):
            msg = f"unexpected restored invocation {invocation_id}"
            raise AssertionError(msg)

    with pytest.raises(RuntimeError, match="Stage 12 report coordination failed"):
        coordinate_report(
            report_execution_input().model_dump(mode="json"),
            invocation_context().model_dump(mode="json"),
            parent_record().model_dump(mode="json"),
            application_name=invocation_context().modal_application,
            coordinator_invocation_id="coordinator-1",
            store=store,
            artifacts=FakeArtifacts(),
            invoker=DispatchFailureInvoker(),
        )

    baseline = store.children[BASELINE_ID]
    reform = store.children[REFORM_ID]
    assert baseline_call.cancelled == 0
    assert baseline.status is ComparisonRunLifecycleStatus.FAILED
    assert baseline.error_code == "simulation_cancelled"
    assert baseline.error_summary == "Cancelled after the reform simulation failed."
    assert reform.status is ComparisonRunLifecycleStatus.FAILED
    assert reform.error_code == "simulation_dispatch_failed"
    assert store.parent.error_code == "simulation_dispatch_failed"


def test_coordinator_propagates_an_already_persisted_child_failure() -> None:
    store = FakeStore()
    artifacts = FakeArtifacts()

    class PersistedFailureCall:
        object_id = "call-reform"

        def get(self, *, timeout=None):
            child = store.children[REFORM_ID]
            failed_at = datetime.now(UTC)
            store.replace_simulation(
                child.model_copy(
                    update={
                        "status": ComparisonRunLifecycleStatus.FAILED,
                        "error_code": "invalid_policy_period",
                        "error_summary": "The reform period is invalid.",
                        "updated_at": failed_at,
                        "completed_at": failed_at,
                    }
                )
            )
            msg = "remote wrapper must not replace the child failure"
            raise RuntimeError(msg)

    class PersistedFailureInvoker:
        def spawn(self, request):
            parsed = PlannedSimulationExecutionInput.model_validate(request.simulation)
            if parsed.role is SimulationRole.REFORM:
                return PersistedFailureCall()
            descriptor = artifacts.add_simulation(parsed, simulation_frames(100.0))
            return ImmediateCall(
                "call-baseline", descriptor.model_dump(mode="json"), []
            )

        def restore(self, invocation_id):
            msg = f"unexpected restored invocation {invocation_id}"
            raise AssertionError(msg)

    with pytest.raises(RuntimeError, match="Stage 12 report coordination failed"):
        coordinate_report(
            report_execution_input().model_dump(mode="json"),
            invocation_context().model_dump(mode="json"),
            parent_record().model_dump(mode="json"),
            application_name=invocation_context().modal_application,
            coordinator_invocation_id="coordinator-1",
            store=store,
            artifacts=artifacts,
            invoker=PersistedFailureInvoker(),
            aggregator=lambda **_: aggregate_payload({"must": "not run"}),
        )

    assert store.children[REFORM_ID].error_code == "invalid_policy_period"
    assert store.children[REFORM_ID].error_summary == "The reform period is invalid."
    assert store.parent.error_code == "invalid_policy_period"
    assert store.parent.error_summary == "The reform period is invalid."


@pytest.mark.parametrize(
    "invoker",
    [
        lambda artifacts: ConcurrentInvoker(
            artifacts, faults=InjectedChildFaults(wrong_plan_digest=True)
        ),
        lambda artifacts: ConcurrentInvoker(
            artifacts, faults=InjectedChildFaults(missing_column=True)
        ),
    ],
)
def test_coordinator_rejects_artifacts_that_do_not_satisfy_the_shared_plan(
    invoker,
) -> None:
    store = FakeStore()
    artifacts = FakeArtifacts()

    with pytest.raises(RuntimeError, match="Stage 12 report coordination failed"):
        coordinate_report(
            report_execution_input().model_dump(mode="json"),
            invocation_context().model_dump(mode="json"),
            parent_record().model_dump(mode="json"),
            application_name=invocation_context().modal_application,
            coordinator_invocation_id="coordinator-1",
            store=store,
            artifacts=artifacts,
            invoker=invoker(artifacts),
            aggregator=lambda **_: aggregate_payload({"must": "not run"}),
        )

    assert artifacts.aggregate_writes == []
    assert store.parent.status is ComparisonRunLifecycleStatus.FAILED


def test_coordinator_records_failure_when_child_persistence_fails() -> None:
    class FailingChildStore(FakeStore):
        @override
        def create_or_resolve_simulation(self, record):
            msg = "database rejected the child record"
            raise RuntimeError(msg)

    store = FailingChildStore()

    with pytest.raises(RuntimeError, match="Stage 12 report coordination failed"):
        coordinate_report(
            report_execution_input().model_dump(mode="json"),
            invocation_context().model_dump(mode="json"),
            parent_record().model_dump(mode="json"),
            application_name=invocation_context().modal_application,
            coordinator_invocation_id="coordinator-1",
            store=store,
            artifacts=FakeArtifacts(),
            invoker=ConcurrentInvoker(FakeArtifacts()),
        )

    assert store.parent.status is ComparisonRunLifecycleStatus.FAILED
    assert store.parent.error_code == "report_coordination_failed"
    assert summary_failure_cause(store.parent.error_summary) == (
        "RuntimeError in stage12_child_state_create: database rejected the child record"
    )


class WorkerRecordedFailureCall:
    """A child whose worker persisted its cause before Modal raised."""

    def __init__(self, object_id, store, simulation_execution_id, worker_summary):
        self.object_id = object_id
        self.store = store
        self.simulation_execution_id = simulation_execution_id
        self.worker_summary = worker_summary

    def get(self, *, timeout=None):
        child = self.store.children[self.simulation_execution_id]
        self.store.children[self.simulation_execution_id] = child.model_copy(
            update={
                "status": ComparisonRunLifecycleStatus.FAILED,
                "error_code": "simulation_execution_failed",
                "error_summary": self.worker_summary,
                "completed_at": NOW,
            }
        )
        msg = "Stage 12 simulation execution failed"
        raise RuntimeError(msg)


class UnreachedCall:
    def __init__(self, object_id):
        self.object_id = object_id

    def get(self, *, timeout=None):
        msg = "the coordinator observes baseline first"
        raise AssertionError(msg)

    def cancel(self):
        return None


class WorkerRecordedFailureInvoker:
    def __init__(self, store, worker_summary):
        self.store = store
        self.worker_summary = worker_summary

    def spawn(self, request):
        planned = PlannedSimulationExecutionInput.model_validate(request.simulation)
        if planned.role is SimulationRole.BASELINE:
            return WorkerRecordedFailureCall(
                "call-baseline",
                self.store,
                planned.simulation_execution_id,
                self.worker_summary,
            )
        return UnreachedCall("call-reform")

    def restore(self, invocation_id):
        msg = f"unexpected restored invocation {invocation_id}"
        raise AssertionError(msg)


def test_coordinator_keeps_the_cause_recorded_by_a_failed_worker() -> None:
    store = FakeStore()
    worker_summary = (
        "ValueError in stage12_calculation (baseline): "
        "Variable 'country' not found in household data "
        "(correlation_id=0123456789abcdef0123456789abcdef)"
    )
    runtime = RecordingRuntime()

    with pytest.raises(RuntimeError, match="Stage 12 report coordination failed"):
        coordinate_report(
            report_execution_input().model_dump(mode="json"),
            invocation_context().model_dump(mode="json"),
            parent_record().model_dump(mode="json"),
            application_name=invocation_context().modal_application,
            coordinator_invocation_id="coordinator-1",
            store=store,
            artifacts=FakeArtifacts(),
            invoker=WorkerRecordedFailureInvoker(store, worker_summary),
            aggregator=lambda **_: aggregate_payload({"must": "not run"}),
            runtime=runtime,
        )

    failed_child = store.children[BASELINE_ID]
    assert failed_child.error_code == "simulation_execution_failed"
    assert failed_child.error_summary == worker_summary
    assert store.parent.error_code == "simulation_execution_failed"
    assert store.parent.error_summary == worker_summary
    [recorded_cause] = runtime.recorded_exceptions
    assert str(recorded_cause) == worker_summary


def test_coordinator_names_the_stage_that_rejected_the_aggregate() -> None:
    class RejectingAggregateArtifacts(FakeArtifacts):
        def write_aggregate(self, *, prefix, payload):
            SPMComparisonProvenance.model_validate(payload["result"]["spm_provenance"])

    store = FakeStore()
    artifacts = RejectingAggregateArtifacts()

    with pytest.raises(RuntimeError, match="Stage 12 report coordination failed"):
        coordinate_report(
            report_execution_input().model_dump(mode="json"),
            invocation_context().model_dump(mode="json"),
            parent_record().model_dump(mode="json"),
            application_name=invocation_context().modal_application,
            coordinator_invocation_id="coordinator-1",
            store=store,
            artifacts=artifacts,
            invoker=ConcurrentInvoker(artifacts),
            aggregator=lambda **_: {"result": {"spm_provenance": None}},
        )

    assert summary_failure_cause(store.parent.error_summary) == (
        "ValidationError in stage12_aggregate_artifact_write: "
        "1 validation error for SPMComparisonProvenance: "
        "Input should be a valid dictionary or instance "
        "of SPMComparisonProvenance [model_type]"
    )


def test_coordinator_builds_its_store_with_the_coordinator_runtime(
    monkeypatch,
) -> None:
    store = FakeStore()
    store_runtimes = []

    def record_store_runtime(runtime):
        store_runtimes.append(runtime)
        return store

    monkeypatch.setattr(coordination_module, "runtime_store", record_store_runtime)
    artifacts = FakeArtifacts()
    runtime = RecordingRuntime()

    coordinate_report(
        report_execution_input().model_dump(mode="json"),
        invocation_context().model_dump(mode="json"),
        parent_record().model_dump(mode="json"),
        application_name=invocation_context().modal_application,
        coordinator_invocation_id="coordinator-1",
        artifacts=artifacts,
        invoker=ConcurrentInvoker(artifacts),
        aggregator=lambda **_: aggregate_payload(),
        runtime=runtime,
    )

    # The runtime store reports each connection retry through this runtime.
    assert store_runtimes == [runtime]
    assert store.parent.status is ComparisonRunLifecycleStatus.SUCCEEDED


def test_coordinator_does_not_deduplicate_against_its_own_claim_after_a_lost_commit() -> (  # noqa: E501  # the test name spells out the lost-commit scenario
    None
):
    # SQLAlchemy raises a failed COMMIT without a statement, like a refused
    # connection, although the claim may already be durable.
    lost_commit_reply = OperationalError(
        None,
        None,
        Exception("server closed the connection unexpectedly"),
    )

    class LostCommitReplyStore(FakeStore):
        def __init__(self):
            super().__init__()
            self.claim_attempts = 0

        def create_or_resolve_report(self, record):
            self.claim_attempts += 1
            claim = super().create_or_resolve_report(record)
            if self.claim_attempts == 1:
                raise lost_commit_reply
            return claim

    store = LostCommitReplyStore()
    artifacts = FakeArtifacts()
    invoker = ConcurrentInvoker(artifacts)

    with pytest.raises(OperationalError) as error:
        coordinate_report(
            report_execution_input().model_dump(mode="json"),
            invocation_context().model_dump(mode="json"),
            parent_record().model_dump(mode="json"),
            application_name=invocation_context().modal_application,
            coordinator_invocation_id="coordinator-1",
            store=store,
            artifacts=artifacts,
            invoker=invoker,
        )

    # A retry would find this coordinator's own running claim and return a
    # deduplicated success without dispatching either simulation.
    assert error.value is lost_commit_reply
    assert store.claim_attempts == 1
    assert invoker.events == []
