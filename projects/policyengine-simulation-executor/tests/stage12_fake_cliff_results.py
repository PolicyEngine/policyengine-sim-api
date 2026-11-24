"""In-memory cliff results beside the run tables, for the cliff calculation's tests.

``FakeCliffResults`` applies the same conditions as ``CliffResultStore``'s
conditional updates, which the persistence library tests against Postgres.
A cliff result names the simulation run it is calculated for; the call's
payload is that run's simulation input.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from uuid import UUID

from policyengine_simulation_contract.stage12_execution import (
    SimulationExecutionInput,
    SimulationRole,
    Stage12InvocationContext,
)
from policyengine_stage12_persistence import CliffResultState, RunStatus, RunWrite
from stage12_fake_report_runs import (
    BASELINE_RUN_ID,
    REPORT_RUN_PREFIX,
    FakeReportRuns,
    report_run_simulation,
)
from stage12_runtime_support import invocation_context

CLIFF_RESULT_ID = UUID("00000000-0000-0000-0000-0000000000f1")


def cliff_simulation(
    role: SimulationRole = SimulationRole.BASELINE,
) -> SimulationExecutionInput:
    """Return the simulation input of the run the cliff result is for."""
    return report_run_simulation(role)


def cliff_result_context(
    cliff_result_id: UUID = CLIFF_RESULT_ID,
) -> Stage12InvocationContext:
    """Return a worker's context, naming the cliff result its call claims."""
    return invocation_context().model_copy(
        update={
            "cliff_result_id": cliff_result_id,
            "artifact_prefix": REPORT_RUN_PREFIX,
        }
    )


@dataclass
class FakeCliffResult:
    """One cliff result's mutable columns, and the run it is calculated for."""

    simulation_run_id: UUID = BASELINE_RUN_ID
    status: RunStatus = RunStatus.PENDING
    modal_call_id: str | None = None
    error_message: str | None = None
    cliff_gap: float | None = None
    cliff_share: float | None = None
    writes: list[str] = field(default_factory=list)


class FakeCliffResults(FakeReportRuns):
    """The run tables and their cliff results, held in memory, written conditionally."""

    def __init__(self, events: list[str] | None = None) -> None:
        super().__init__(events)
        self.cliff_results = {CLIFF_RESULT_ID: FakeCliffResult()}

    def get_cliff_result(self, cliff_result_id):
        """Read a cliff result; a read is not a write, so it records nothing."""
        result = self._run(self.cliff_results, cliff_result_id, "cliff result")
        return CliffResultState(
            simulation_run_id=result.simulation_run_id,
            status=result.status,
            modal_call_id=result.modal_call_id,
            cliff_gap=result.cliff_gap,
            cliff_share=result.cliff_share,
            error_message=result.error_message,
        )

    def claim_cliff_result(
        self, cliff_result_id, *, modal_call_id, claimed_at
    ) -> RunWrite:
        result = self._run(self.cliff_results, cliff_result_id, "cliff result")
        return self._apply(
            result,
            "claim_cliff_result",
            condition=lambda: self._claimable(result, modal_call_id),
            status=RunStatus.RUNNING,
            modal_call_id=modal_call_id,
        )

    def complete_cliff_result(
        self, cliff_result_id, *, modal_call_id, cliff_gap, cliff_share, completed_at
    ) -> RunWrite:
        result = self._run(self.cliff_results, cliff_result_id, "cliff result")
        return self._apply(
            result,
            "complete_cliff_result",
            condition=lambda: self._holds(result, modal_call_id),
            status=RunStatus.SUCCEEDED,
            cliff_gap=cliff_gap,
            cliff_share=cliff_share,
            error_message=None,
        )

    def fail_cliff_result(
        self, cliff_result_id, *, modal_call_id, error_message, failed_at
    ) -> RunWrite:
        result = self._run(self.cliff_results, cliff_result_id, "cliff result")
        return self._apply(
            result,
            "fail_cliff_result",
            condition=lambda: self._holds(result, modal_call_id),
            status=RunStatus.FAILED,
            error_message=error_message,
        )
