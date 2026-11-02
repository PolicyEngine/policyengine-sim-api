"""Fake child calls and invokers, and checks on them, for Stage 12 coordinator tests."""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import TYPE_CHECKING

from policyengine_simulation_contract.stage12_execution import (
    ComparisonRunLifecycleStatus,
    PlannedSimulationExecutionInput,
    ReportAggregate,
    ReportExecutionInput,
    RowIdentity,
    SimulationExecutionInput,
    SimulationRole,
)
from policyengine_simulation_executor.stage12_runtime import (
    coordination as coordination_module,
)
from stage12_fake_stores import FakeArtifacts, FakeStore, simulation_frames
from stage12_runtime_support import (
    BASELINE_ID,
    EVALUATION_ID,
    REFORM_ID,
    aggregate_payload,
    coordinate_report,
    invocation_context,
    parent_record,
    planned_simulation,
    report_execution_input,
    simulation_execution_input,
    summary_failure_cause,
)

if TYPE_CHECKING:
    from uuid import UUID


class FutureCall:
    def __init__(self, object_id, future):
        self.object_id = object_id
        self.future = future

    def get(self, *, timeout=None):
        return self.future.result(timeout=timeout)

    def cancel(self):
        self.future.cancel()


class ImmediateCall:
    def __init__(self, object_id, result, observed_timeouts):
        self.object_id = object_id
        self.result = result
        self.observed_timeouts = observed_timeouts
        self.cancel_count = 0

    def get(self, *, timeout=None):
        self.observed_timeouts.append(timeout)
        return self.result

    def cancel(self):
        self.cancel_count += 1


@dataclass(frozen=True)
class InjectedChildFaults:
    """Faults a ConcurrentInvoker injects into the child simulations it runs."""

    fail_role: str | None = None
    incompatible: bool = False
    wrong_plan_digest: bool = False
    missing_column: bool = False


class ConcurrentInvoker:
    def __init__(
        self,
        artifacts,
        *,
        faults: InjectedChildFaults | None = None,
        production_result=None,
    ):
        faults = faults or InjectedChildFaults()
        self.artifacts = artifacts
        self.fail_role = faults.fail_role
        self.incompatible = faults.incompatible
        self.wrong_plan_digest = faults.wrong_plan_digest
        self.missing_column = faults.missing_column
        self.executor = ThreadPoolExecutor(max_workers=2)
        self.intervals = {}
        self.events = []
        self.environments = []
        self.production_result = production_result
        self.production_wait_timeouts = []
        self.output_plans = []

    def spawn(self, request):
        parsed = PlannedSimulationExecutionInput.model_validate(request.simulation)
        role = parsed.role.value
        self.events.append(f"spawn:{role}")
        self.environments.append(request.environment)
        self.output_plans.append(parsed.output_plan)

        return self._submit(parsed, role)

    def restore(self, invocation_id):
        if invocation_id == "production-job-1":
            self.events.append("restore:production")
            return ImmediateCall(
                invocation_id,
                self.production_result,
                self.production_wait_timeouts,
            )
        role = invocation_id.removeprefix("call-")
        parsed = planned_simulation(SimulationRole(role))
        self.events.append(f"restore:{role}")
        return self._submit(parsed, role)

    def _submit(self, parsed, role):

        def run():
            started = time.monotonic()
            time.sleep(0.05)
            if role == self.fail_role:
                msg = "child calculation failed"
                raise RuntimeError(msg)
            frames = simulation_frames(100.0 if role == "baseline" else 120.0)
            if self.missing_column and role == "reform":
                frames["household"] = frames["household"].drop(
                    columns=["household_net_income"]
                )
            descriptor = self.artifacts.add_simulation(parsed, frames)
            if self.incompatible and role == "reform":
                descriptor = descriptor.model_copy(
                    update={
                        "row_identity": RowIdentity(
                            identifier_columns=("household.household_id",),
                            row_count=1,
                            identity_sha256="f" * 64,
                        )
                    }
                )
            if self.wrong_plan_digest and role == "reform":
                descriptor = descriptor.model_copy(
                    update={"output_plan_sha256": "f" * 64}
                )
            self.intervals[role] = (started, time.monotonic())
            return descriptor.model_dump(mode="json")

        return FutureCall(f"call-{role}", self.executor.submit(run))


class RefusingDispatchInvoker:
    def __init__(self, dispatch_error):
        self.dispatch_error = dispatch_error
        self.events = []

    def spawn(self, request):
        role = PlannedSimulationExecutionInput.model_validate(
            request.simulation
        ).role.value
        self.events.append(f"spawn:{role}")
        raise self.dispatch_error

    def restore(self, invocation_id):
        msg = f"unexpected restored invocation {invocation_id}"
        raise AssertionError(msg)


class TimeoutCall:
    def __init__(self, object_id):
        self.object_id = object_id
        self.cancelled = 0

    def get(self, *, timeout=None):
        msg = "contains sensitive timeout details"
        raise TimeoutError(msg)

    def cancel(self):
        self.cancelled += 1


class TimeoutInvoker:
    def __init__(self):
        self.events = []

    def spawn(self, request):
        role = PlannedSimulationExecutionInput.model_validate(
            request.simulation
        ).role.value
        self.events.append(f"spawn:{role}")
        return TimeoutCall(f"call-{role}")

    def restore(self, invocation_id):
        msg = f"unexpected restored invocation {invocation_id}"
        raise AssertionError(msg)


class StartupWriteError(Exception):
    """A coordinator persistence failure after a child may have started."""


@dataclass(frozen=True)
class ChildCallScript:
    """What a scripted child call does when probed or cancelled.

    It returns ``result`` or raises ``wait_error`` once probed (``during_wait``
    runs first), and raises ``cancel_error`` when cancelled; with none set, it
    keeps running.
    """

    result: object = None
    wait_error: Exception | None = None
    cancel_error: Exception | None = None
    during_wait: object = None


class ScriptedChildCall:
    """A started child that keeps running unless the test scripts an outcome."""

    def __init__(self, object_id, *, clock=None, script=None):
        script = script or ChildCallScript()
        self.object_id = object_id
        self.clock = clock
        self.result = script.result
        self.wait_error = script.wait_error
        self.cancel_error = script.cancel_error
        self.during_wait = script.during_wait
        self.probed_at = []
        self.cancel_count = 0

    def get(self, *, timeout=None):
        self.probed_at.append(
            self.clock.monotonic() if self.clock is not None else None
        )
        if self.during_wait is not None:
            self.during_wait()
        if self.wait_error is not None:
            raise self.wait_error
        if self.result is not None:
            return self.result
        msg = "still running"
        raise TimeoutError(msg)

    def cancel(self):
        self.cancel_count += 1
        if self.cancel_error is not None:
            raise self.cancel_error


class ScriptedChildInvoker:
    def __init__(
        self,
        *,
        clock=None,
        results=None,
        wait_errors=None,
        cancel_errors=None,
        during_wait=None,
    ):
        self.clock = clock
        self.results = results or {}
        self.wait_errors = wait_errors or {}
        self.cancel_errors = cancel_errors or {}
        self.during_wait = during_wait or {}
        self.events = []
        self.calls = {}

    def spawn(self, request):
        role = PlannedSimulationExecutionInput.model_validate(
            request.simulation
        ).role.value
        self.events.append(f"spawn:{role}")
        return self._call(role)

    def restore(self, invocation_id):
        role = invocation_id.removeprefix("call-")
        self.events.append(f"restore:{role}")
        return self._call(role)

    def _call(self, role):
        call = ScriptedChildCall(
            f"call-{role}",
            clock=self.clock,
            script=ChildCallScript(
                result=self.results.get(role),
                wait_error=self.wait_errors.get(role),
                cancel_error=self.cancel_errors.get(role),
                during_wait=self.during_wait.get(role),
            ),
        )
        self.calls[role] = call
        return call


class PlaceholderWriteFailingStore(FakeStore):
    def __init__(self, failing_simulation_id):
        super().__init__()
        self.failing_simulation_id = failing_simulation_id

    def replace_simulation(self, record):
        # The coordinator's only plain replacement is the dispatch placeholder.
        if record.simulation_execution_id == self.failing_simulation_id:
            msg = "placeholder write contains sensitive details"
            raise StartupWriteError(msg)
        return super().replace_simulation(record)


class AttachFailingStore(FakeStore):
    def __init__(self, failing_simulation_id):
        super().__init__()
        self.failing_simulation_id = failing_simulation_id

    def attach_simulation_invocation(self, simulation_execution_id, **values):
        if simulation_execution_id == self.failing_simulation_id:
            msg = "attach contains sensitive details"
            raise StartupWriteError(msg)
        return super().attach_simulation_invocation(simulation_execution_id, **values)


class CancellationStateFailingStore(FakeStore):
    def replace_simulation_if_status(self, record, *, expected_status):
        if record.error_code == "simulation_cancelled":
            msg = "database contains sensitive details"
            raise ConnectionError(msg)
        return super().replace_simulation_if_status(
            record, expected_status=expected_status
        )


def role_simulation_id(role: str) -> UUID:
    return BASELINE_ID if role == "baseline" else REFORM_ID


def report_with_segmentation(*, segmented: bool) -> ReportExecutionInput:
    if segmented:
        return report_execution_input()

    def unsegmented(role: SimulationRole) -> SimulationExecutionInput:
        return simulation_execution_input(role).model_copy(
            update={"options": {"segmented": False}}
        )

    return ReportExecutionInput(
        evaluation_id=EVALUATION_ID,
        baseline=unsegmented(SimulationRole.BASELINE),
        reform=unsegmented(SimulationRole.REFORM),
        requested_aggregates=tuple(ReportAggregate),
    )


def coordinate_with(report, store, invoker, **coordinator_options):
    coordinator_options.setdefault("artifacts", FakeArtifacts())
    return coordinate_report(
        report.model_dump(mode="json"),
        invocation_context().model_dump(mode="json"),
        parent_record().model_dump(mode="json"),
        application_name=invocation_context().modal_application,
        coordinator_invocation_id="coordinator-1",
        store=store,
        invoker=invoker,
        aggregator=lambda **_: aggregate_payload({"must": "not run"}),
        **coordinator_options,
    )


def assert_cancelled(child, *, after: str) -> None:
    assert child.status is ComparisonRunLifecycleStatus.FAILED
    assert child.error_code == "simulation_cancelled"
    assert child.error_summary == f"Cancelled after {after}."
    assert child.completed_at is not None


def assert_original_coordinator_failure(error, store) -> None:
    assert isinstance(error.value.__context__, StartupWriteError)
    assert store.parent.status is ComparisonRunLifecycleStatus.FAILED
    assert store.parent.error_code == "report_coordination_failed"
    original_error = error.value.__context__
    assert summary_failure_cause(store.parent.error_summary).startswith(
        f"{type(original_error).__name__} "
    )
    assert summary_failure_cause(store.parent.error_summary).endswith(
        f": {original_error}"
    )


def coordination_log_record(caplog, level):
    (record,) = [
        record
        for record in caplog.records
        if record.name == coordination_module.__name__ and record.levelno == level
    ]
    return record


def assert_log_identifies_child(record, *, role: str, error_type: str) -> None:
    simulation_execution_id = str(role_simulation_id(role))
    assert record.evaluation_id == str(EVALUATION_ID)
    assert record.simulation_execution_id == simulation_execution_id
    assert record.simulation_role == role
    assert record.error_type == error_type
    message = record.getMessage()
    assert str(EVALUATION_ID) in message
    assert simulation_execution_id in message
    assert "sensitive" not in message
    assert record.exc_info is None
