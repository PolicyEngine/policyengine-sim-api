"""Recording the household report run a v2 household function's call names.

The simulation service creates a household report's simulation runs, one
for its baseline and one for any reform, and its report run naming them,
all pending with no Modal call; it starts one household function call with
the report run's ID, then records the call's ID on the report run. The
function claims the report run under its own Modal call ID, then each side:
it claims the side's simulation run, calculates it, and records the
``HouseholdExecutionResult``, or the ``HouseholdExecutionFailure`` with a
one-line summary, in the run's ``household_result``. Both sides are always
calculated, so each run has its own outcome. The report run then succeeds
only if every side's run did; otherwise it fails with the first such side's
reason: its calculation's failure summary, or that another call holds its
run, or that another writer finished the run first. Everything is written
as the Modal worker role.

A report run another call holds, or one already finished, is not calculated
again. A call Modal restarts after a pre-emption runs under the same call
ID, so it resumes its own claims, and keeps a side it had already recorded.

Identical requests share simulation runs, so a side's run may be another
household report run's too. A side whose run another call holds is waited
for once the sides this call can claim are done, as a report run's
coordinator waits (``shared_simulation_runs``): the run is used once it
succeeds, its failure is the side's, and it is failed only once its call
ended without an outcome; a side still held when the wait window closes
(``HOUSEHOLD_SIDE_WAIT_SECONDS`` from the call's start) fails the report run
and is left to its call. A run named on both sides is calculated once. A
failing call fails only the runs it holds.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from functools import partial
from typing import TYPE_CHECKING, Any, Protocol

from policyengine_simulation_contract.household_execution import (
    HouseholdExecutionFailure,
)
from policyengine_simulation_contract.household_report_execution import (
    HouseholdReportRunCalculation,
)
from policyengine_stage12_persistence import HouseholdReportRunStore, RunStatus

from policyengine_simulation_executor.stage12_timeouts import (
    HOUSEHOLD_SIDE_WAIT_SECONDS,
)

from .database_retry import open_connection_with_retry
from .dependencies import ModalChildInvoker
from .failure_recording import persist_failure_record
from .household_job_recording import (
    household_failure_summary,
    unexpected_failure_summary,
)
from .report_run_access import MODAL_WORKER_DATABASE_URL_ENV
from .run_sweep import modal_call_state
from .shared_simulation_runs import (
    HeldRunWait,
    HeldRunWaitTimeoutError,
    WaitedRun,
    WaitedRunFailedError,
    WaitedRunSucceeded,
    wait_for_held_run,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from uuid import UUID

    from policyengine_observability import ObservabilityRuntime
    from policyengine_simulation_contract.household_execution import (
        HouseholdExecutionRequest,
        HouseholdExecutionResult,
    )
    from policyengine_simulation_contract.household_report_execution import (
        HouseholdReportSide,
    )
    from policyengine_stage12_persistence import RunWrite, SimulationRunState

logger = logging.getLogger(__name__)

# A report run whose side failed records this code, then the side's summary.
SIDE_FAILED_CODE = "household_calculation_failed"
# A report run whose side another call holds records this code.
SIDE_UNAVAILABLE_CODE = "household_simulation_run_unavailable"

type HouseholdCalculation = Callable[
    [HouseholdExecutionRequest],
    HouseholdExecutionResult | HouseholdExecutionFailure,
]


class HouseholdReportRunWriter(Protocol):
    """A household report run's writes; ``HouseholdReportRunStore`` implements it.

    Each write is conditional on its row's status and, but for
    ``fail_simulation_run`` given no call, on its recorded Modal call; each
    returns whether it changed the row, and the row's status afterwards.
    """

    def claim_report_run(
        self, report_run_id: UUID, *, modal_call_id: str, claimed_at: datetime
    ) -> RunWrite:
        """Mark a pending or running report run that records no other call running."""
        ...

    def household_report_simulation_runs(
        self, report_run_id: UUID
    ) -> tuple[UUID, UUID | None]:
        """Return the baseline and reform simulation runs the report run names."""
        ...

    def claim_simulation_run(
        self, simulation_run_id: UUID, *, modal_call_id: str, claimed_at: datetime
    ) -> RunWrite:
        """Mark a pending or running run that records no other call running."""
        ...

    def get_simulation_run(self, simulation_run_id: UUID) -> SimulationRunState:
        """Return a simulation run's state, with its recorded call and failure."""
        ...

    def complete_household_simulation_run(
        self,
        simulation_run_id: UUID,
        *,
        modal_call_id: str,
        household_result: Mapping[str, Any],
        completed_at: datetime,
    ) -> RunWrite:
        """Mark the run succeeded with its household while this call holds it."""
        ...

    def fail_household_simulation_run(
        self,
        simulation_run_id: UUID,
        *,
        modal_call_id: str,
        error_message: str,
        failed_at: datetime,
        household_result: Mapping[str, Any] | None = None,
    ) -> RunWrite:
        """Mark the run failed while this call holds it."""
        ...

    def fail_simulation_run(
        self,
        simulation_run_id: UUID,
        *,
        error_message: str,
        failed_at: datetime,
        modal_call_id: str | None = None,
    ) -> RunWrite:
        """Mark the run failed: while ``modal_call_id`` holds it, or while active."""
        ...

    def fail_simulation_run_held_by(
        self,
        simulation_run_id: UUID,
        *,
        modal_call_id: str,
        error_message: str,
        failed_at: datetime,
    ) -> RunWrite:
        """Mark the run failed while it is unfinished and records ``modal_call_id``."""
        ...

    def complete_household_report_run(
        self, report_run_id: UUID, *, modal_call_id: str, completed_at: datetime
    ) -> RunWrite:
        """Mark the report run succeeded while this call holds it."""
        ...

    def fail_report_run(
        self,
        report_run_id: UUID,
        *,
        modal_call_id: str,
        error_message: str,
        failed_at: datetime,
    ) -> RunWrite:
        """Mark the report run failed while this call holds it."""
        ...


def household_report_run_store(
    runtime: ObservabilityRuntime | None = None,
) -> HouseholdReportRunStore:
    """Build the household report run store from ``MODAL_WORKER_DATABASE_URL``.

    Raises ``ValueError`` when the variable is unset or not a complete
    ``postgresql://`` URL.
    """
    return HouseholdReportRunStore(
        os.environ.get(MODAL_WORKER_DATABASE_URL_ENV, ""),
        open_connection=partial(open_connection_with_retry, runtime=runtime),
    )


@dataclass(frozen=True)
class SideOutcome:
    """What one side's simulation run ended as, and why it didn't succeed.

    ``reason`` is the report run's failure for a side whose run did not
    succeed, and empty for one that did.
    """

    side: HouseholdReportSide
    simulation_run_id: UUID
    status: RunStatus
    reason: str = ""

    def as_json(self) -> dict[str, str]:
        """Return the side's outcome as the call answers it."""
        return {
            "side": self.side,
            "simulation_run_id": str(self.simulation_run_id),
            "status": self.status.value,
        }


@dataclass
class _ReportRunCall:
    """One call's writes to the report run and its simulation runs."""

    writer: HouseholdReportRunWriter
    report_run_id: UUID
    modal_call_id: str
    runtime: ObservabilityRuntime | None

    def side(
        self,
        side: HouseholdReportSide,
        request: HouseholdExecutionRequest,
        calculate: HouseholdCalculation,
        held: list[UUID],
    ) -> SideOutcome | None:
        """Claim, calculate and record one side; return what its run ended as.

        ``held`` gains the run before its claim, since a claim that raises may
        have applied, and loses it once the run is finished or another call
        holds it. None means another call holds the run: the side is waited
        for once the others are done (``waited_side``).
        """
        run_id = request.calculation_id
        held.append(run_id)
        claimed = self.writer.claim_simulation_run(
            run_id, modal_call_id=self.modal_call_id, claimed_at=datetime.now(UTC)
        )
        if not claimed.changed:
            held.remove(run_id)
            state = self.writer.get_simulation_run(run_id)
            if state.status in {RunStatus.PENDING, RunStatus.RUNNING}:
                return None
            return self._side_already_held(side, run_id)
        outcome = calculate(request)
        document = outcome.model_dump(mode="json")
        if isinstance(outcome, HouseholdExecutionFailure):
            summary = household_failure_summary(outcome)
            recorded = self.writer.fail_household_simulation_run(
                run_id,
                modal_call_id=self.modal_call_id,
                error_message=summary,
                failed_at=datetime.now(UTC),
                household_result=document,
            )
        else:
            summary = None
            recorded = self.writer.complete_household_simulation_run(
                run_id,
                modal_call_id=self.modal_call_id,
                household_result=document,
                completed_at=datetime.now(UTC),
            )
        held.remove(run_id)
        if recorded.changed:
            reason = "" if summary is None else _side_failure(side, summary)
        else:
            # Another writer finished the run this call held, so the call's
            # outcome isn't the run's.
            logger.warning(
                "Simulation run %s was %s before its calculation was recorded",
                run_id,
                recorded.status.value,
            )
            reason = (
                f"{SIDE_UNAVAILABLE_CODE}: The {side} simulation run was "
                f"{recorded.status.value} before its calculation was recorded."
            )
        return SideOutcome(
            side=side, simulation_run_id=run_id, status=recorded.status, reason=reason
        )

    def waited_side(  # noqa: PLR0913  # the side, its calculation, then the wait
        self,
        side: HouseholdReportSide,
        request: HouseholdExecutionRequest,
        calculate: HouseholdCalculation,
        held: list[UUID],
        *,
        wait: HeldRunWait,
        deadline: float,
    ) -> SideOutcome:
        """Wait for a side's run another call holds; return what it ended as.

        A run left pending with no call is claimed and calculated here, and
        one that moves to another call is waited for under it, until the
        wait window closes.
        """
        run_id = request.calculation_id
        while True:
            waited = WaitedRun(
                simulation_run_id=run_id,
                writer=self.writer,
                restore=wait.restore,
                call_state=wait.call_state,
                object_id=self.writer.get_simulation_run(run_id).modal_call_id or "",
                # The holder is another household function, which goes on
                # after recording the run.
                read_run_first=True,
            )
            try:
                result = wait_for_held_run(waited, wait, deadline=deadline)
            except WaitedRunFailedError:
                return self._side_already_held(side, run_id)
            except HeldRunWaitTimeoutError:
                return SideOutcome(
                    side=side,
                    simulation_run_id=run_id,
                    status=self.writer.get_simulation_run(run_id).status,
                    reason=(
                        f"{SIDE_UNAVAILABLE_CODE}: The {side} simulation run is "
                        "still running under another call after "
                        f"{wait.timeout_seconds:g} seconds."
                    ),
                )
            if isinstance(result, WaitedRunSucceeded):
                return SideOutcome(
                    side=side, simulation_run_id=run_id, status=RunStatus.SUCCEEDED
                )
            outcome = self.side(side, request, calculate, held)
            if outcome is not None:
                return outcome

    def _side_already_held(
        self, side: HouseholdReportSide, run_id: UUID
    ) -> SideOutcome:
        """Return a side's run this call could not claim, as it stands.

        A restarted call finds a side it finished: a succeeded run is kept,
        and a failed one keeps its recorded failure. A run still pending or
        running holds another call, which this call leaves alone.
        """
        state = self.writer.get_simulation_run(run_id)
        if state.status is RunStatus.SUCCEEDED:
            return SideOutcome(side=side, simulation_run_id=run_id, status=state.status)
        if state.status is RunStatus.FAILED:
            reason = _side_failure(
                side, state.error_message or "no failure was recorded"
            )
        else:
            reason = (
                f"{SIDE_UNAVAILABLE_CODE}: The {side} simulation run is "
                f"{state.status.value} under another call."
            )
        return SideOutcome(
            side=side, simulation_run_id=run_id, status=state.status, reason=reason
        )

    def finish(self, outcomes: list[SideOutcome]) -> RunStatus:
        """Mark the report run succeeded, or failed with its first side that wasn't.

        A side succeeds only when its run did: one with any other status fails
        the report run, with the side's reason.
        """
        unsucceeded = next(
            (
                outcome
                for outcome in outcomes
                if outcome.status is not RunStatus.SUCCEEDED
            ),
            None,
        )
        if unsucceeded is None:
            recorded = self.writer.complete_household_report_run(
                self.report_run_id,
                modal_call_id=self.modal_call_id,
                completed_at=datetime.now(UTC),
            )
            status = RunStatus.SUCCEEDED
        else:
            recorded = self.writer.fail_report_run(
                self.report_run_id,
                modal_call_id=self.modal_call_id,
                error_message=unsucceeded.reason,
                failed_at=datetime.now(UTC),
            )
            status = RunStatus.FAILED
        if not recorded.changed:
            logger.warning(
                "Report run %s was %s before its outcome was recorded",
                self.report_run_id,
                recorded.status.value,
            )
            # Another writer finished it first: answer the run as it stands.
            return recorded.status
        return status

    def fail_unfinished(self, error: BaseException, *, held: list[UUID]) -> None:
        """Fail each run this call may hold, logging rather than raising.

        ``held`` holds the report run's simulation runs this call claimed, or
        tried to, and has neither finished nor found held by another call
        (``side`` keeps it). Each is failed only while this call holds it
        running, so a claim that rolled back, and a run other report runs
        share, are left alone. Then the report run is failed while this call
        holds it.
        """
        summary = unexpected_failure_summary(error)
        for run_id in held:
            persist_failure_record(
                partial(
                    self.writer.fail_simulation_run,
                    run_id,
                    error_message=summary,
                    failed_at=datetime.now(UTC),
                    modal_call_id=self.modal_call_id,
                ),
                runtime=self.runtime,
                failure_scope="household_simulation_run",
            )
        persist_failure_record(
            partial(
                self.writer.fail_report_run,
                self.report_run_id,
                modal_call_id=self.modal_call_id,
                error_message=summary,
                failed_at=datetime.now(UTC),
            ),
            runtime=self.runtime,
            failure_scope="report_run",
        )


def _require_named_runs(
    report_run_id: UUID,
    named: tuple[UUID, UUID | None],
    calculation: HouseholdReportRunCalculation,
) -> None:
    """Raise ValueError unless the payload's runs are the ones the report run names."""
    payload_runs = (
        calculation.baseline.calculation_id,
        None if calculation.reform is None else calculation.reform.calculation_id,
    )
    if payload_runs != named:
        baseline, reform = named
        msg = (
            f"report run {report_run_id} names other simulation runs than its "
            f"call: baseline {baseline} and reform {reform}"
        )
        raise ValueError(msg)


def _side_failure(side: HouseholdReportSide, summary: str) -> str:
    """Return the report run's failure for a side that failed with ``summary``."""
    return f"{SIDE_FAILED_CODE}: The {side} simulation run failed: {summary}"


def run_household_report(  # noqa: PLR0913  # the run, its payload and the call's seams
    report_run_id: UUID,
    payload: Mapping[str, Any],
    calculate: HouseholdCalculation,
    *,
    modal_call_id: str | None,
    runtime: ObservabilityRuntime | None = None,
    writer: HouseholdReportRunWriter | None = None,
    wait: HeldRunWait | None = None,
) -> dict[str, Any]:
    """Claim the report run, calculate and record each side, then finish the run.

    ``payload`` is the call's ``HouseholdReportRunCalculation`` as sent; it
    is validated once the call holds the report run, so a payload the
    contract refuses fails the run at once. Returns the report run's and
    each side's status, or, for a report run another call holds or one
    already finished, a summary of the report run without calculating.
    ``writer`` defaults to the store the Modal worker role's database URL
    names, and ``wait``, how a side another call holds is waited for, to
    Modal's (``household_side_wait``). An error or Modal's cancellation fails
    the report run, and each of its simulation runs this call holds, with a
    generic summary, then propagates; a pre-emption doesn't, because Modal
    runs the call again under the same call ID. The simulation runs are known once the
    call has read them, so an error in the report run's claim, or in that
    read, fails the report run if the claim applied before the error, and
    leaves its simulation runs to the stuck-run sweep.
    Raises RuntimeError, before any write, when ``modal_call_id`` is None.
    """
    if modal_call_id is None:
        msg = "Modal household function call identifier is unavailable"
        raise RuntimeError(msg)
    call = _ReportRunCall(
        writer=writer or household_report_run_store(runtime),
        report_run_id=report_run_id,
        modal_call_id=modal_call_id,
        runtime=runtime,
    )
    # The window for a side another call holds opens as the call starts.
    held_wait = wait or household_side_wait()
    deadline = held_wait.monotonic() + held_wait.timeout_seconds
    # The simulation runs this call may hold, and so fail if it stops.
    held: list[UUID] = []
    try:
        claimed = call.writer.claim_report_run(
            report_run_id, modal_call_id=modal_call_id, claimed_at=datetime.now(UTC)
        )
        if not claimed.changed:
            return {
                "deduplicated": True,
                "report_run_id": str(report_run_id),
                "status": claimed.status.value,
            }
        named = call.writer.household_report_simulation_runs(report_run_id)
        calculation = HouseholdReportRunCalculation.model_validate(payload)
        sides = calculation.sides()
        _require_named_runs(report_run_id, named, calculation)
        outcomes = _side_outcomes(
            call, sides, calculate, held, wait=held_wait, deadline=deadline
        )
        status = call.finish(outcomes)
    except KeyboardInterrupt:
        # Pre-emption: Modal runs the call again, and it resumes its claims.
        raise
    except BaseException as error:
        call.fail_unfinished(error, held=held)
        raise
    return {
        "report_run_id": str(report_run_id),
        "status": status.value,
        "simulation_runs": [outcome.as_json() for outcome in outcomes],
    }


def household_side_wait() -> HeldRunWait:
    """Return how a household function waits for a side another call holds.

    It probes Modal's calls (``ModalChildInvoker.restore``), reads their
    state as the stuck-run sweep does, and waits until
    ``HOUSEHOLD_SIDE_WAIT_SECONDS`` after the call's start.
    """
    return HeldRunWait(
        restore=ModalChildInvoker().restore,
        call_state=modal_call_state,
        timeout_seconds=HOUSEHOLD_SIDE_WAIT_SECONDS,
    )


def _side_outcomes(  # noqa: PLR0913  # the call, its sides and calculation, then the wait
    call: _ReportRunCall,
    sides: tuple[tuple[HouseholdReportSide, HouseholdExecutionRequest], ...],
    calculate: HouseholdCalculation,
    held: list[UUID],
    *,
    wait: HeldRunWait,
    deadline: float,
) -> list[SideOutcome]:
    """Return each side's outcome, in the sides' order.

    The sides whose runs this call can claim are calculated first; then each
    side another call holds is waited for. A run named on both sides is
    calculated, or waited for, once, and its outcome is both sides'.
    """
    by_run: dict[UUID, SideOutcome] = {}
    deferred: list[tuple[HouseholdReportSide, HouseholdExecutionRequest]] = []
    for side, request in sides:
        run_id = request.calculation_id
        if run_id in by_run or any(
            waiting.calculation_id == run_id for _, waiting in deferred
        ):
            continue
        outcome = call.side(side, request, calculate, held)
        if outcome is None:
            deferred.append((side, request))
        else:
            by_run[run_id] = outcome
    for side, request in deferred:
        by_run[request.calculation_id] = call.waited_side(
            side, request, calculate, held, wait=wait, deadline=deadline
        )
    return [
        replace(by_run[request.calculation_id], side=side) for side, request in sides
    ]
