"""In-memory run tables, and the inputs of a detailed-save call, for runtime tests.

``FakeReportRuns`` applies the same conditions as ``ReportRunStore``'s
conditional updates, which the persistence library tests against Postgres.
A detailed-save call names its runs by their IDs: its report input's
evaluation ID is the report run's, and each simulation's execution ID is
its simulation run's.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from threading import Lock
from uuid import UUID

from fixtures.macro_output import CURRENT_SINGLE_YEAR_MACRO_RESULT
from policyengine_simulation_contract.stage12_execution import (
    PlannedSimulationExecutionInput,
    ReportExecutionInput,
    ReportRunIds,
    SimulationExecutionInput,
    SimulationRole,
    Stage12InvocationContext,
)
from policyengine_stage12_persistence import RunStatus, RunWrite, SimulationRunState
from stage12_runtime_support import (
    aggregate_payload,
    invocation_context,
    report_execution_input,
    shared_output_plan,
    simulation_execution_input,
)

REPORT_RUN_ID = UUID("00000000-0000-0000-0000-0000000000a1")
BASELINE_RUN_ID = UUID("00000000-0000-0000-0000-0000000000b1")
REFORM_RUN_ID = UUID("00000000-0000-0000-0000-0000000000c1")
REPORT_RUN_IDS = ReportRunIds(
    report_run_id=REPORT_RUN_ID,
    baseline_simulation_run_id=BASELINE_RUN_ID,
    reform_simulation_run_id=REFORM_RUN_ID,
)
SIMULATION_RUN_IDS = {
    SimulationRole.BASELINE: BASELINE_RUN_ID,
    SimulationRole.REFORM: REFORM_RUN_ID,
}
# Where a detailed-save call's planned inputs go in the artifact bucket.
REPORT_RUN_PREFIX = f"stage-12-runs/staging/2026/09/{REPORT_RUN_ID}"
ACTIVE = (RunStatus.PENDING, RunStatus.RUNNING)
# What the shared report's simulations calculate, which its simulation runs
# record: its year and bundle versions, and one catalog dataset and region.
SHARED_SIMULATION = simulation_execution_input(SimulationRole.BASELINE)
SHARED_DATASET_ID = UUID("00000000-0000-0000-0000-0000000000d1")
SHARED_REGION_ID = UUID("00000000-0000-0000-0000-0000000000e1")


# A reform the real worker accepts: an effective period with dates.
DATED_REFORM_POLICY = {"reform": {"2026-01-01.2026-12-31": 1}}


def report_run_simulation(role: SimulationRole) -> SimulationExecutionInput:
    """Return the shared report's ``role`` simulation, named by its runs."""
    simulation = simulation_execution_input(role)
    return simulation.model_copy(
        update={
            "evaluation_id": REPORT_RUN_ID,
            "simulation_execution_id": SIMULATION_RUN_IDS[role],
            "policy": (
                DATED_REFORM_POLICY
                if role is SimulationRole.REFORM
                else simulation.policy
            ),
        }
    )


def report_run_input() -> ReportExecutionInput:
    """Return the shared report, named by its report run and simulation runs."""
    return report_execution_input().model_copy(
        update={
            "evaluation_id": REPORT_RUN_ID,
            "baseline": report_run_simulation(SimulationRole.BASELINE),
            "reform": report_run_simulation(SimulationRole.REFORM),
        }
    )


def report_run_planned_simulation(
    role: SimulationRole,
) -> PlannedSimulationExecutionInput:
    """Return the input a report run's coordinator gives its ``role`` worker."""
    return PlannedSimulationExecutionInput.model_validate(
        {
            **report_run_simulation(role).model_dump(mode="json"),
            "output_plan": shared_output_plan().model_dump(mode="json"),
        }
    )


def report_run_context() -> Stage12InvocationContext:
    """Return the coordinator's context, naming the report run and its runs."""
    return invocation_context().model_copy(
        update={"report_run": REPORT_RUN_IDS, "artifact_prefix": REPORT_RUN_PREFIX}
    )


def simulation_run_context(run_id: UUID = BASELINE_RUN_ID) -> Stage12InvocationContext:
    """Return a worker's context, naming the simulation run its call claims."""
    return invocation_context().model_copy(
        update={"simulation_run_id": run_id, "artifact_prefix": REPORT_RUN_PREFIX}
    )


def economy_aggregate(**_values):
    """Aggregate the report into a complete single-year economy output."""
    return aggregate_payload(result=CURRENT_SINGLE_YEAR_MACRO_RESULT)


class ComparisonStoreNotUsed:
    """A comparison store that fails any use: detailed-save calls write none."""

    def __getattr__(self, name):
        msg = f"a detailed-save call used the comparison store's {name}"
        raise AssertionError(msg)


@dataclass
class FakeRun:
    """One report run's or simulation run's mutable columns.

    A simulation run also records what it calculates, by default the shared
    report's simulation.
    """

    status: RunStatus = RunStatus.PENDING
    modal_call_id: str | None = None
    error_message: str | None = None
    output_uri: str | None = None
    output_sha256: str | None = None
    result: object | None = None
    writes: list[str] = field(default_factory=list)
    year: int = SHARED_SIMULATION.year
    country_package_version: str = SHARED_SIMULATION.bundle.country_package_version
    policyengine_version: str = SHARED_SIMULATION.bundle.policyengine_version
    dataset_id: UUID | None = SHARED_DATASET_ID
    region_id: UUID | None = SHARED_REGION_ID


class FakeReportRuns:
    """Report runs and simulation runs held in memory, written conditionally."""

    def __init__(self, events: list[str] | None = None) -> None:
        self.report_runs = {REPORT_RUN_ID: FakeRun()}
        self.simulation_runs = {BASELINE_RUN_ID: FakeRun(), REFORM_RUN_ID: FakeRun()}
        # Each report run's baseline and reform simulation runs.
        self.simulation_run_pairs = {REPORT_RUN_ID: (BASELINE_RUN_ID, REFORM_RUN_ID)}
        # Shared with other fakes when a test checks the order of writes.
        self.events = events if events is not None else []
        # Workers write from their own threads.
        self.lock = Lock()

    def require_simulation_runs(self, run_ids: ReportRunIds) -> None:
        if run_ids.report_run_id not in self.simulation_run_pairs:
            msg = f"report run {run_ids.report_run_id} does not exist"
            raise LookupError(msg)
        baseline_run_id, reform_run_id = self.simulation_run_pairs[
            run_ids.report_run_id
        ]
        if (baseline_run_id, reform_run_id) != (
            run_ids.baseline_simulation_run_id,
            run_ids.reform_simulation_run_id,
        ):
            msg = (
                f"report run {run_ids.report_run_id} names other simulation runs "
                f"than its call: baseline {baseline_run_id} and "
                f"reform {reform_run_id}"
            )
            raise ValueError(msg)

    def get_simulation_run(self, simulation_run_id):
        """Read a simulation run; a read is not a write, so it records nothing."""
        run = self._run(self.simulation_runs, simulation_run_id, "simulation run")
        return SimulationRunState(
            status=run.status,
            year=run.year,
            country_package_version=run.country_package_version,
            policyengine_version=run.policyengine_version,
            dataset_id=run.dataset_id,
            region_id=run.region_id,
            output_uri=run.output_uri,
            output_sha256=run.output_sha256,
            modal_call_id=run.modal_call_id,
            error_message=run.error_message,
        )

    def claim_report_run(self, report_run_id, *, modal_call_id, claimed_at):
        run = self._run(self.report_runs, report_run_id, "report run")
        return self._apply(
            run,
            "claim_report_run",
            condition=lambda: self._claimable(run, modal_call_id),
            status=RunStatus.RUNNING,
            modal_call_id=modal_call_id,
        )

    def complete_report_run(self, run_ids, *, modal_call_id, completed_at, result=None):
        run = self._run(self.report_runs, run_ids.report_run_id, "report run")
        return self._apply(
            run,
            "complete_report_run",
            condition=lambda: self._holds(run, modal_call_id),
            status=RunStatus.SUCCEEDED,
            result=result,
        )

    def fail_report_run(
        self, report_run_id, *, modal_call_id, error_message, failed_at
    ):
        run = self._run(self.report_runs, report_run_id, "report run")
        return self._apply(
            run,
            "fail_report_run",
            condition=lambda: self._holds(run, modal_call_id),
            status=RunStatus.FAILED,
            error_message=error_message,
        )

    def claim_simulation_run(self, simulation_run_id, *, modal_call_id, claimed_at):
        run = self._run(self.simulation_runs, simulation_run_id, "simulation run")
        return self._apply(
            run,
            "claim_simulation_run",
            condition=lambda: self._claimable(run, modal_call_id),
            status=RunStatus.RUNNING,
            modal_call_id=modal_call_id,
        )

    def complete_simulation_run(
        self,
        simulation_run_id,
        *,
        modal_call_id,
        output_uri,
        output_sha256,
        completed_at,
    ):
        run = self._run(self.simulation_runs, simulation_run_id, "simulation run")
        return self._apply(
            run,
            "complete_simulation_run",
            condition=lambda: self._holds(run, modal_call_id),
            status=RunStatus.SUCCEEDED,
            output_uri=output_uri,
            output_sha256=output_sha256,
        )

    def fail_simulation_run(
        self, simulation_run_id, *, error_message, failed_at, modal_call_id=None
    ):
        run = self._run(self.simulation_runs, simulation_run_id, "simulation run")
        return self._apply(
            run,
            "fail_simulation_run",
            condition=lambda: (
                run.status in ACTIVE
                if modal_call_id is None
                else self._holds(run, modal_call_id)
            ),
            status=RunStatus.FAILED,
            error_message=error_message,
        )

    def release_simulation_run(self, simulation_run_id, *, modal_call_id, released_at):
        run = self._run(self.simulation_runs, simulation_run_id, "simulation run")
        return self._apply(
            run,
            "release_simulation_run",
            condition=lambda: (
                run.status in ACTIVE and run.modal_call_id == modal_call_id
            ),
            status=RunStatus.PENDING,
            modal_call_id=None,
        )

    @staticmethod
    def _claimable(run: FakeRun, modal_call_id: str) -> bool:
        return run.status in ACTIVE and run.modal_call_id in {None, modal_call_id}

    @staticmethod
    def _holds(run: FakeRun, modal_call_id: str) -> bool:
        return run.status is RunStatus.RUNNING and run.modal_call_id == modal_call_id

    @staticmethod
    def _run(runs: dict[UUID, FakeRun], run_id: UUID, noun: str) -> FakeRun:
        if run_id not in runs:
            msg = f"{noun} {run_id} does not exist"
            raise LookupError(msg)
        return runs[run_id]

    def _apply(self, run: FakeRun, operation: str, *, condition, **values):
        """Apply ``values`` if ``condition`` holds, checked under the lock."""
        with self.lock:
            changed = condition()
            run.writes.append(operation)
            self.events.append(operation)
            if changed:
                for name, value in values.items():
                    setattr(run, name, value)
            return RunWrite(changed=changed, status=run.status)
