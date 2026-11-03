"""An in-memory stand-in for the report run store, for Stage 12 runtime tests.

It applies the same conditions as ``ReportRunStore``'s conditional updates,
which the persistence library tests against Postgres.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from uuid import UUID

from fixtures.macro_output import CURRENT_SINGLE_YEAR_MACRO_RESULT
from policyengine_simulation_contract.stage12_execution import ReportRunIds
from policyengine_stage12_persistence import RunStatus, RunWrite
from stage12_runtime_support import aggregate_payload, invocation_context

REPORT_RUN_ID = UUID("00000000-0000-0000-0000-0000000000a1")
BASELINE_RUN_ID = UUID("00000000-0000-0000-0000-0000000000b1")
REFORM_RUN_ID = UUID("00000000-0000-0000-0000-0000000000c1")
REPORT_RUN_IDS = ReportRunIds(
    report_run_id=REPORT_RUN_ID,
    baseline_simulation_run_id=BASELINE_RUN_ID,
    reform_simulation_run_id=REFORM_RUN_ID,
)
ACTIVE = (RunStatus.PENDING, RunStatus.RUNNING)


def report_run_context():
    """Return the shared invocation context, naming the report run and its runs."""
    return invocation_context().model_copy(update={"report_run": REPORT_RUN_IDS})


def economy_aggregate(**_values):
    """Aggregate the report into a complete single-year economy output."""
    return aggregate_payload(result=CURRENT_SINGLE_YEAR_MACRO_RESULT)


@dataclass
class FakeRun:
    """One report run's or simulation run's mutable columns."""

    status: RunStatus = RunStatus.PENDING
    modal_call_id: str | None = None
    error_message: str | None = None
    output_uri: str | None = None
    output_sha256: str | None = None
    result: object | None = None
    writes: list[str] = field(default_factory=list)


class FakeReportRuns:
    """Report runs and simulation runs held in memory, written conditionally."""

    def __init__(self, events: list[str] | None = None) -> None:
        self.report_runs = {REPORT_RUN_ID: FakeRun()}
        self.simulation_runs = {BASELINE_RUN_ID: FakeRun(), REFORM_RUN_ID: FakeRun()}
        self.links = {("baseline", BASELINE_RUN_ID), ("reform", REFORM_RUN_ID)}
        # Shared with other fakes when a test checks the order of writes.
        self.events = events if events is not None else []

    def require_simulation_runs(self, run_ids: ReportRunIds) -> None:
        expected = {
            ("baseline", run_ids.baseline_simulation_run_id),
            ("reform", run_ids.reform_simulation_run_id),
        }
        if not expected <= self.links:
            msg = "the report run does not combine the simulation runs its call names"
            raise ValueError(msg)

    def claim_report_run(self, report_run_id, *, modal_call_id, claimed_at):
        run = self._run(self.report_runs, report_run_id, "report run")
        claimable = run.status is RunStatus.PENDING or (
            run.status is RunStatus.RUNNING
            and run.modal_call_id in {None, modal_call_id}
        )
        return self._apply(
            run,
            "claim_report_run",
            changed=claimable,
            status=RunStatus.RUNNING,
            modal_call_id=modal_call_id,
        )

    def complete_report_run(self, run_ids, *, modal_call_id, completed_at, result=None):
        run = self._run(self.report_runs, run_ids.report_run_id, "report run")
        holds_it = self._holds(run, modal_call_id)
        return self._apply(
            run,
            "complete_report_run",
            changed=holds_it,
            status=RunStatus.SUCCEEDED,
            result=result,
        )

    def fail_report_run(
        self, report_run_id, *, modal_call_id, error_message, failed_at
    ):
        run = self._run(self.report_runs, report_run_id, "report run")
        holds_it = self._holds(run, modal_call_id)
        return self._apply(
            run,
            "fail_report_run",
            changed=holds_it,
            status=RunStatus.FAILED,
            error_message=error_message,
        )

    def claim_simulation_run(self, simulation_run_id, *, modal_call_id, claimed_at):
        run = self._run(self.simulation_runs, simulation_run_id, "simulation run")
        return self._apply(
            run,
            "claim_simulation_run",
            changed=run.status in ACTIVE,
            status=RunStatus.RUNNING,
            modal_call_id=modal_call_id,
        )

    def complete_simulation_run(
        self, simulation_run_id, *, output_uri, output_sha256, completed_at
    ):
        run = self._run(self.simulation_runs, simulation_run_id, "simulation run")
        return self._apply(
            run,
            "complete_simulation_run",
            changed=run.status is RunStatus.RUNNING,
            status=RunStatus.SUCCEEDED,
            output_uri=output_uri,
            output_sha256=output_sha256,
        )

    def fail_simulation_run(self, simulation_run_id, *, error_message, failed_at):
        run = self._run(self.simulation_runs, simulation_run_id, "simulation run")
        return self._apply(
            run,
            "fail_simulation_run",
            changed=run.status in ACTIVE,
            status=RunStatus.FAILED,
            error_message=error_message,
        )

    @staticmethod
    def _holds(run: FakeRun, modal_call_id: str) -> bool:
        return run.status is RunStatus.RUNNING and run.modal_call_id == modal_call_id

    @staticmethod
    def _run(runs: dict[UUID, FakeRun], run_id: UUID, noun: str) -> FakeRun:
        if run_id not in runs:
            msg = f"{noun} {run_id} does not exist"
            raise LookupError(msg)
        return runs[run_id]

    def _apply(self, run: FakeRun, operation: str, *, changed: bool, **values):
        run.writes.append(operation)
        self.events.append(operation)
        if changed:
            for name, value in values.items():
                setattr(run, name, value)
        return RunWrite(changed=changed, status=run.status)
