"""Record each Modal call's outcome on its runs, one pass of due checks at a time.

A pass:

1. fails runs and comparison steps left ``dispatching`` for ten minutes, so
   they can be submitted again, and pending comparison steps whose baseline
   or reform run failed, since they can never start;
2. claims the running calls that are due a check, by moving their next check
   one lease ahead, so two passes do not check the same call at once;
3. asks Modal about each claimed call without waiting, and records what it
   found with conditional writes: a finished call's result is validated and
   its result rows are written in the transaction that marks its runs
   ``succeeded``; a failed or timed-out call is marked ``failed`` with its
   error; a call still running, or finished but of a kind this revision has
   no recorder for, is checked again once its claim's lease ends.

Pre-emption is never a failure: Modal re-runs a pre-empted call under the
same call ID, so it reads as still running. No caller polls, and Modal holds
no database credentials: sim writes every outcome.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import timedelta
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol

from policyengine_simulation_contract.call_reports import CallReportStatus
from policyengine_simulation_persistence import (
    DueSimulationCall,
    RunError,
    RunErrorCode,
    RunStatus,
)
from policyengine_simulation_persistence.run_store import utc_now

from policyengine_simulation_entry.modal_calls import (
    OWN_PROCESS_SIGNALS,
    CallFailed,
    CallRunning,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from datetime import datetime
    from uuid import UUID

    from policyengine_simulation_contract.call_reports import CallEndedReport
    from policyengine_simulation_persistence import (
        AbandonedDispatches,
        ReportKind,
        ReportRunRecord,
        SimulationKind,
        SimulationRunRecord,
    )
    from sqlalchemy import Connection

    from policyengine_simulation_entry.modal_calls import CallInspector, CallOutcome

logger = logging.getLogger(__name__)


def _event_name(prefix: str, outcome: str) -> str:
    """Name a log event for a simulation run or a comparison step."""
    return f"{prefix}_{outcome}"


# Cloud Scheduler starts the safety check this often (the deploy's
# schedule): a call that has not reported is checked about this long after
# its dispatch, then once per interval while it runs.
SAFETY_CHECK_INTERVAL = timedelta(minutes=5)
# A claimed call is not checked by another pass for this long, so each
# execution of the safety check checks a running call once; if the pass
# stops, the call becomes due again afterwards.
CHECK_LEASE = SAFETY_CHECK_INTERVAL
# A run left dispatching this long was claimed by an instance that stopped.
DISPATCH_ABANDONED_AFTER = timedelta(minutes=10)
CLAIM_BATCH_SIZE = 50
# Each check writes through the runtime engine's small pool, which requests
# share (policyengine_simulation_persistence.engine: 2 connections plus 3
# overflow), so a pass uses at most three at once.
INSPECTION_CONCURRENCY = 3


class SimulationResultRecorder(Protocol):
    """Validate one kind of simulation call's output and write its result rows."""

    def validate(self, output: object) -> object:
        """Return the validated result; raise ValueError when it is invalid."""
        ...

    def write(
        self,
        connection: Connection,
        runs: tuple[SimulationRunRecord, ...],
        result: object,
    ) -> None:
        """Write the result rows of ``runs`` in the transaction that succeeds them."""
        ...


class ComparisonResultRecorder(Protocol):
    """Validate one kind of comparison's output and write its result rows."""

    def validate(self, output: object) -> object:
        """Return the validated result; raise ValueError when it is invalid."""
        ...

    def write(
        self, connection: Connection, report_run: ReportRunRecord, result: object
    ) -> None:
        """Write the comparison's result rows in the transaction that succeeds it."""
        ...


@dataclass(frozen=True)
class ResultRecorders:
    """The result recorders by kind of run.

    A finished call of a kind with no recorder here is checked again later,
    so that a revision which has one records it.
    """

    simulations: Mapping[SimulationKind, SimulationResultRecorder] = field(
        default_factory=dict
    )
    comparisons: Mapping[ReportKind, ComparisonResultRecorder] = field(
        default_factory=dict
    )


class RecordingTransitions(Protocol):
    """The ``RunTransitions`` writes a recording pass needs."""

    def claim_due_simulation_calls(
        self, *, limit: int, lease: timedelta
    ) -> list[DueSimulationCall]:
        """Claim running simulation calls that are due a check."""
        ...

    def complete_simulation_call(
        self,
        modal_call_id: str,
        write_results: Callable[[Connection, tuple[SimulationRunRecord, ...]], None],
    ) -> bool:
        """Mark a call's runs succeeded and write their results."""
        ...

    def fail_simulation_call(self, modal_call_id: str, error: RunError) -> bool:
        """Mark a call's runs failed."""
        ...

    def claim_due_comparisons(
        self, *, limit: int, lease: timedelta
    ) -> list[ReportRunRecord]:
        """Claim running comparison steps that are due a check."""
        ...

    def complete_comparison(
        self,
        report_run_id: UUID,
        write_results: Callable[[Connection, ReportRunRecord], None],
    ) -> bool:
        """Mark a comparison step succeeded and write its results."""
        ...

    def fail_comparison(self, report_run_id: UUID, error: RunError) -> bool:
        """Mark a comparison step failed."""
        ...

    def fail_abandoned_dispatches(
        self, *, claimed_before: datetime
    ) -> AbandonedDispatches:
        """Fail runs and comparison steps left dispatching."""
        ...

    def fail_comparisons_of_failed_runs(self) -> tuple[UUID, ...]:
        """Fail pending comparison steps whose simulation runs failed."""
        ...


class RunReader(Protocol):
    """The ``RunStore`` reads a reported call needs."""

    def get_simulation_run(self, run_id: UUID) -> SimulationRunRecord | None:
        """Return the simulation run with this ID, or None."""
        ...

    def get_report_run(self, report_run_id: UUID) -> ReportRunRecord | None:
        """Return the report run with this ID, or None."""
        ...


class _Recorded(StrEnum):
    """What recording one call's outcome did."""

    SUCCEEDED = "succeeded"
    FAILED = "failed"
    STILL_RUNNING = "still_running"
    WITHOUT_RECORDER = "without_recorder"
    NOT_WRITTEN = "not_written"
    # Another writer recorded the outcome first.
    UNCHANGED = "unchanged"


_REPORT_STATUSES = {
    _Recorded.SUCCEEDED: CallReportStatus.RECORDED,
    _Recorded.FAILED: CallReportStatus.RECORDED,
    _Recorded.STILL_RUNNING: CallReportStatus.STILL_RUNNING,
    _Recorded.WITHOUT_RECORDER: CallReportStatus.NO_RESULT_RECORDER,
    _Recorded.NOT_WRITTEN: CallReportStatus.NOT_WRITTEN,
    _Recorded.UNCHANGED: CallReportStatus.ALREADY_RECORDED,
}
# A run or comparison step in these statuses may not hold its call ID yet.
_NOT_YET_DISPATCHED = frozenset({RunStatus.PENDING, RunStatus.DISPATCHING})


@dataclass
class RecordingPass:
    """What one pass found and recorded."""

    checked: int = 0
    succeeded: int = 0
    failed: int = 0
    still_running: int = 0
    not_reached: int = 0
    abandoned: int = 0
    comparisons_without_runs: int = 0
    without_recorder: int = 0

    @property
    def found_work(self) -> bool:
        """Whether the pass checked or failed anything."""
        return bool(self.checked or self.abandoned or self.comparisons_without_runs)

    def count(self, recorded: _Recorded) -> None:
        """Count what recording one checked call did."""
        counters = {
            _Recorded.SUCCEEDED: "succeeded",
            _Recorded.FAILED: "failed",
            _Recorded.STILL_RUNNING: "still_running",
            _Recorded.WITHOUT_RECORDER: "without_recorder",
            _Recorded.NOT_WRITTEN: "not_reached",
        }
        counter = counters.get(recorded)
        if counter is not None:
            setattr(self, counter, getattr(self, counter) + 1)


@dataclass(frozen=True)
class _ResultSteps:
    """Validate a finished call's output, then record it with its result rows."""

    validate: Callable[[object], object]
    complete: Callable[[object], bool]


@dataclass(frozen=True)
class _CallHandlers:
    """How to record one claimed call's outcome, bound to its runs.

    ``result_steps`` is None when no recorder handles the runs' kind.
    """

    subject: dict[str, object]
    # "simulation_run" or "report_run_comparison": the log event names' start.
    event_prefix: str
    result_steps: _ResultSteps | None
    fail: Callable[[RunError], bool]


def _invalid_result(error: ValueError) -> RunError:
    count = getattr(error, "error_count", None)
    detail = f" with {count()} errors" if callable(count) else ""
    return RunError(
        RunErrorCode.INVALID_RESULT,
        f"The Modal call's output failed validation{detail} ({type(error).__name__}).",
    )


@dataclass
class RunRecorder:
    """Record Modal call outcomes: one reported call, or a pass of due checks."""

    transitions: RecordingTransitions
    runs: RunReader
    inspector: CallInspector
    recorders: ResultRecorders = field(default_factory=ResultRecorders)
    clock: Callable[[], datetime] = utc_now

    async def record_due_outcomes(self) -> RecordingPass:
        """Run one pass: fail abandoned dispatches, then check every due call."""
        recording = RecordingPass()
        await self._fail_stopped_work(recording)
        calls = await asyncio.to_thread(
            lambda: self.transitions.claim_due_simulation_calls(
                limit=CLAIM_BATCH_SIZE, lease=CHECK_LEASE
            )
        )
        comparisons = await asyncio.to_thread(
            lambda: self.transitions.claim_due_comparisons(
                limit=CLAIM_BATCH_SIZE, lease=CHECK_LEASE
            )
        )
        handlers = [
            *(self._simulation_handlers(call) for call in calls),
            *(self._comparison_handlers(report_run) for report_run in comparisons),
        ]
        limit = asyncio.Semaphore(INSPECTION_CONCURRENCY)

        async def check(modal_call_id: str, call_handlers: _CallHandlers) -> None:
            async with limit:
                await self._check(modal_call_id, call_handlers, recording)

        await asyncio.gather(
            *(
                check(modal_call_id, call_handlers)
                for modal_call_id, call_handlers in handlers
            )
        )
        return recording

    async def _fail_stopped_work(self, recording: RecordingPass) -> None:
        """Fail dispatches that stopped, and comparisons that can never start."""
        abandoned = await asyncio.to_thread(
            lambda: self.transitions.fail_abandoned_dispatches(
                claimed_before=self.clock() - DISPATCH_ABANDONED_AFTER
            )
        )
        recording.abandoned = len(abandoned.simulation_run_ids) + len(
            abandoned.report_run_ids
        )
        for event_prefix, field_name, ids in (
            ("simulation_run", "simulation_run_ids", abandoned.simulation_run_ids),
            ("report_run_comparison", "report_run_ids", abandoned.report_run_ids),
        ):
            if ids:
                logger.warning(
                    _event_name(event_prefix, "dispatches_abandoned"),
                    extra={field_name: [str(identifier) for identifier in ids]},
                )
        without_runs = await asyncio.to_thread(
            self.transitions.fail_comparisons_of_failed_runs
        )
        recording.comparisons_without_runs = len(without_runs)
        if without_runs:
            logger.warning(
                "report_run_comparisons_failed_with_their_runs",
                extra={
                    "report_run_ids": [
                        str(report_run_id) for report_run_id in without_runs
                    ]
                },
            )

    def _simulation_handlers(
        self, call: DueSimulationCall
    ) -> tuple[str, _CallHandlers]:
        kinds = {run.simulation_kind for run in call.runs}
        recorder = (
            self.recorders.simulations.get(next(iter(kinds)))
            if len(kinds) == 1
            else None
        )
        call_id = call.modal_call_id
        result_steps = None
        if recorder is not None:
            writer = recorder
            result_steps = _ResultSteps(
                validate=writer.validate,
                complete=lambda result: self.transitions.complete_simulation_call(
                    call_id,
                    lambda connection, runs: writer.write(connection, runs, result),
                ),
            )
        return call_id, _CallHandlers(
            subject={
                "modal_call_id": call_id,
                "simulation_run_ids": [str(run.id) for run in call.runs],
            },
            event_prefix="simulation_run",
            result_steps=result_steps,
            fail=lambda error: self.transitions.fail_simulation_call(call_id, error),
        )

    def _comparison_handlers(
        self, report_run: ReportRunRecord
    ) -> tuple[str, _CallHandlers]:
        recorder = self.recorders.comparisons.get(report_run.report_kind)
        report_run_id = report_run.id
        call_id = report_run.comparison_modal_call_id or ""
        result_steps = None
        if recorder is not None:
            writer = recorder
            result_steps = _ResultSteps(
                validate=writer.validate,
                complete=lambda result: self.transitions.complete_comparison(
                    report_run_id,
                    lambda connection, record: writer.write(connection, record, result),
                ),
            )
        return call_id, _CallHandlers(
            subject={"modal_call_id": call_id, "report_run_id": str(report_run_id)},
            event_prefix="report_run_comparison",
            result_steps=result_steps,
            fail=lambda error: self.transitions.fail_comparison(report_run_id, error),
        )

    async def _check(
        self, modal_call_id: str, handlers: _CallHandlers, recording: RecordingPass
    ) -> None:
        recording.checked += 1
        try:
            outcome = await self.inspector.inspect(modal_call_id)
            recorded = await self._record(outcome, handlers)
        # Modal could not be reached, or a database write failed (for example
        # a dropped connection). Each check ends here, whatever was raised
        # (only a signal to this process passes), so a pass returns only once
        # all of its checks have, and its concurrency limit holds. The claim's
        # lease makes the call due again; nothing was recorded.
        except BaseException as error:
            if isinstance(error, OWN_PROCESS_SIGNALS):
                raise
            recording.not_reached += 1
            logger.warning(
                _event_name(handlers.event_prefix, "check_failed"),
                extra={**handlers.subject, **_error_fields(error)},
            )
            return
        recording.count(recorded)

    async def record_reported_call(self, report: CallEndedReport) -> CallReportStatus:
        """Record the outcome of a call whose Modal function reported its end.

        The report is only a hint: the runs are read, Modal is asked about the
        call without waiting, and the outcome is recorded with the same
        conditional writes as a recording pass, so a repeated report changes
        nothing. While the outcome cannot be recorded yet, the answer is one
        of ``RETRY_LATER_STATUSES`` and nothing is written; the sender
        reports again, and the safety check finds the call otherwise. Only a
        signal to this process propagates.
        """
        try:
            found = await asyncio.to_thread(self._reported_call, report)
        except Exception as error:  # the database could not be read
            logger.exception(
                "call_report_not_read",
                extra={"modal_call_id": report.modal_call_id, **_error_fields(error)},
            )
            return CallReportStatus.NOT_WRITTEN
        if isinstance(found, CallReportStatus):
            return found
        try:
            outcome = await self.inspector.inspect(report.modal_call_id)
        except BaseException as error:
            if isinstance(error, OWN_PROCESS_SIGNALS):
                raise
            logger.warning(
                _event_name(found.event_prefix, "check_failed"),
                extra={**found.subject, **_error_fields(error)},
            )
            return CallReportStatus.MODAL_UNREACHABLE
        try:
            recorded = await self._record(outcome, found)
        except BaseException as error:
            if isinstance(error, OWN_PROCESS_SIGNALS):
                raise
            logger.warning(
                _event_name(found.event_prefix, "outcome_not_written"),
                extra={**found.subject, **_error_fields(error)},
            )
            return CallReportStatus.NOT_WRITTEN
        return _REPORT_STATUSES[recorded]

    def _reported_call(
        self, report: CallEndedReport
    ) -> CallReportStatus | _CallHandlers:
        """Return how to record the reported call, or why it cannot be yet."""
        call_id = report.modal_call_id
        if report.simulation_run_ids is not None:
            runs = [
                self.runs.get_simulation_run(run_id)
                for run_id in report.simulation_run_ids
            ]
            known = [run for run in runs if run is not None]
            if len(known) != len(runs):
                return CallReportStatus.UNKNOWN_RUNS
            state = _reported_state(
                call_id, [(run.modal_call_id, run.status) for run in known]
            )
            if state is not None:
                return state
            due = DueSimulationCall(modal_call_id=call_id, runs=tuple(known))
            return self._simulation_handlers(due)[1]
        report_run = (
            None
            if report.report_run_id is None
            else self.runs.get_report_run(report.report_run_id)
        )
        if report_run is None or report_run.comparison_status is None:
            return CallReportStatus.UNKNOWN_RUNS
        state = _reported_state(
            call_id,
            [(report_run.comparison_modal_call_id, report_run.comparison_status)],
        )
        if state is not None:
            return state
        return self._comparison_handlers(report_run)[1]

    async def _record(self, outcome: CallOutcome, handlers: _CallHandlers) -> _Recorded:
        # A call still running, or of a kind with no recorder here, is checked
        # again once its claim's lease ends; nothing is written.
        if isinstance(outcome, CallRunning):
            return _Recorded.STILL_RUNNING
        if isinstance(outcome, CallFailed):
            return await self._fail(handlers, outcome.error)
        steps = handlers.result_steps
        if steps is None:
            # A revision without a recorder for this kind leaves the call for
            # one that has it; Modal's output retention bounds the wait.
            logger.warning(
                _event_name(handlers.event_prefix, "without_result_recorder"),
                extra=handlers.subject,
            )
            return _Recorded.WITHOUT_RECORDER
        return await self._record_result(outcome.output, steps, handlers)

    async def _record_result(
        self, output: object, steps: _ResultSteps, handlers: _CallHandlers
    ) -> _Recorded:
        """Validate a finished call's output and write it with its success."""
        try:
            result = await asyncio.to_thread(steps.validate, output)
        except ValueError as error:
            return await self._fail(handlers, _invalid_result(error))
        try:
            completed = await asyncio.to_thread(steps.complete, result)
        # Writing the result rows failed, for example because the database
        # was unreachable: nothing was recorded, and the call is checked again.
        except Exception as error:
            logger.exception(
                _event_name(handlers.event_prefix, "result_not_written"),
                extra={**handlers.subject, **_error_fields(error)},
            )
            return _Recorded.NOT_WRITTEN
        if not completed:
            return _Recorded.UNCHANGED
        logger.info(
            _event_name(handlers.event_prefix, "succeeded"), extra=handlers.subject
        )
        return _Recorded.SUCCEEDED

    async def _fail(self, handlers: _CallHandlers, error: RunError) -> _Recorded:
        if not await asyncio.to_thread(handlers.fail, error):
            return _Recorded.UNCHANGED
        logger.warning(
            _event_name(handlers.event_prefix, "failed"),
            extra={**handlers.subject, "error_code": error.code.value},
        )
        return _Recorded.FAILED


def _reported_state(
    modal_call_id: str, executions: list[tuple[str | None, RunStatus]]
) -> CallReportStatus | None:
    """Return why a reported call cannot be recorded, or None if it can.

    ``executions`` holds each reported run's (or the comparison step's) call
    ID and status.
    """
    held_call_ids = {held for held, _ in executions}
    if held_call_ids == {modal_call_id}:
        if any(status is RunStatus.RUNNING for _, status in executions):
            return None
        return CallReportStatus.ALREADY_RECORDED
    if held_call_ids - {modal_call_id, None}:
        return CallReportStatus.OTHER_CALL
    # Some runs hold no call ID yet. While they are being dispatched, the
    # dispatcher is about to store it; a finished run without it (failed as
    # abandoned, say) never will.
    if all(
        status in _NOT_YET_DISPATCHED for held, status in executions if held is None
    ):
        return CallReportStatus.NOT_DISPATCHED_YET
    return CallReportStatus.OTHER_CALL


def _error_fields(error: BaseException) -> dict[str, str]:
    """Name an error, and the error it was raised from, for a log record."""
    fields = {"error_type": type(error).__name__}
    if error.__cause__ is not None:
        fields["error_cause_type"] = type(error.__cause__).__name__
    return fields
