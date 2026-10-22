"""The background loop that records each Modal call's outcome on its runs.

Each Simulation Entrypoint instance runs one loop. On every pass it:

1. fails runs and comparison steps left ``dispatching`` for ten minutes, so
   they can be submitted again, and pending comparison steps whose baseline
   or reform run failed, since they can never start;
2. claims the running calls that are due a check, by moving their next check
   one lease ahead, so instances do not check the same call at once;
3. asks Modal about each claimed call without waiting, and records what it
   found with conditional writes: a finished call's result is validated and
   its result rows are written in the transaction that marks its runs
   ``succeeded``; a failed or timed-out call is marked ``failed`` with its
   error; a call still running gets a later check, backing off.

Pre-emption is never a failure: Modal re-runs a pre-empted call under the
same call ID, so it reads as still running. No caller polls, and Modal holds
no database credentials: this loop writes every outcome. Cloud Run keeps the
instance's CPU allocated between requests, so the loop runs then too.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass, field
from datetime import timedelta
from typing import TYPE_CHECKING, Protocol

from policyengine_simulation_persistence import RunError, RunErrorCode
from policyengine_simulation_persistence.run_store import utc_now

from policyengine_simulation_entry.modal_calls import (
    CallFailed,
    CallInspectionError,
    CallRunning,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping
    from datetime import datetime
    from uuid import UUID

    from policyengine_simulation_persistence import (
        AbandonedDispatches,
        DueSimulationCall,
        ReportKind,
        ReportRunRecord,
        SimulationKind,
        SimulationRunRecord,
    )
    from sqlalchemy import Connection

    from policyengine_simulation_entry.modal_calls import CallInspector, CallOutcome

logger = logging.getLogger(__name__)

# A claimed call is not checked by another instance for this long; if the
# claiming instance stops, the call becomes due again afterwards.
CHECK_LEASE = timedelta(minutes=1)
# A run left dispatching this long was claimed by an instance that stopped.
DISPATCH_ABANDONED_AFTER = timedelta(minutes=10)
FIRST_BACKOFF = timedelta(seconds=2)
LONGEST_BACKOFF = timedelta(seconds=60)
CLAIM_BATCH_SIZE = 50
INSPECTION_CONCURRENCY = 10
BUSY_PASS_INTERVAL_SECONDS = 1.0
IDLE_PASS_INTERVAL_SECONDS = 5.0


def next_check_delay(check_count: int) -> timedelta:
    """Return the wait before the next check of a call checked ``check_count`` times."""
    return min(FIRST_BACKOFF * 2 ** max(check_count - 1, 0), LONGEST_BACKOFF)


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
    """The result recorders by kind of run; a kind with none fails its runs."""

    simulations: Mapping[SimulationKind, SimulationResultRecorder] = field(
        default_factory=dict
    )
    comparisons: Mapping[ReportKind, ComparisonResultRecorder] = field(
        default_factory=dict
    )


class RecordingTransitions(Protocol):
    """The ``RunTransitions`` writes the loop needs."""

    def claim_due_simulation_calls(
        self, *, limit: int, lease: timedelta
    ) -> list[DueSimulationCall]:
        """Claim running simulation calls that are due a check."""
        ...

    def record_simulation_call_running(
        self, modal_call_id: str, *, next_check_at: datetime
    ) -> bool:
        """Set a running call's next check."""
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

    def record_comparison_running(
        self, report_run_id: UUID, *, next_check_at: datetime
    ) -> bool:
        """Set a running comparison step's next check."""
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

    @property
    def found_work(self) -> bool:
        """Whether the pass checked or failed anything."""
        return bool(self.checked or self.abandoned or self.comparisons_without_runs)


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
    check_count: int
    result_steps: _ResultSteps | None
    fail: Callable[[RunError], bool]
    keep_running: Callable[[datetime], bool]


def _invalid_result(error: ValueError) -> RunError:
    count = getattr(error, "error_count", None)
    detail = f" with {count()} errors" if callable(count) else ""
    return RunError(
        RunErrorCode.INVALID_RESULT,
        f"The Modal call's output failed validation{detail} ({type(error).__name__}).",
    )


@dataclass
class RunRecorder:
    """Record the outcomes of due Modal calls, one pass at a time."""

    transitions: RecordingTransitions
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
        if recording.abandoned:
            logger.warning(
                "simulation_run_dispatches_abandoned",
                extra={
                    "simulation_run_ids": [
                        str(run_id) for run_id in abandoned.simulation_run_ids
                    ],
                    "report_run_ids": [
                        str(report_run_id) for report_run_id in abandoned.report_run_ids
                    ],
                },
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
            check_count=max(run.check_count for run in call.runs),
            result_steps=result_steps,
            fail=lambda error: self.transitions.fail_simulation_call(call_id, error),
            keep_running=lambda next_check_at: (
                self.transitions.record_simulation_call_running(
                    call_id, next_check_at=next_check_at
                )
            ),
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
            check_count=report_run.comparison_check_count,
            result_steps=result_steps,
            fail=lambda error: self.transitions.fail_comparison(report_run_id, error),
            keep_running=lambda next_check_at: (
                self.transitions.record_comparison_running(
                    report_run_id, next_check_at=next_check_at
                )
            ),
        )

    async def _check(
        self, modal_call_id: str, handlers: _CallHandlers, recording: RecordingPass
    ) -> None:
        recording.checked += 1
        try:
            outcome = await self.inspector.inspect(modal_call_id)
        except CallInspectionError as error:
            # The claim's lease makes the call due again; nothing to record.
            recording.not_reached += 1
            logger.warning(
                "simulation_run_check_failed",
                extra={**handlers.subject, "error_type": str(error)},
            )
            return
        await self._record(outcome, handlers, recording)

    async def _record(
        self, outcome: CallOutcome, handlers: _CallHandlers, recording: RecordingPass
    ) -> None:
        if isinstance(outcome, CallRunning):
            next_check_at = self.clock() + next_check_delay(handlers.check_count)
            await asyncio.to_thread(handlers.keep_running, next_check_at)
            recording.still_running += 1
            return
        if isinstance(outcome, CallFailed):
            await self._fail(handlers, outcome.error, recording)
            return
        steps = handlers.result_steps
        if steps is None:
            await self._fail(
                handlers,
                RunError(
                    RunErrorCode.NO_RESULT_RECORDER,
                    "No result recorder handles this kind of run.",
                ),
                recording,
            )
            return
        try:
            result = await asyncio.to_thread(steps.validate, outcome.output)
        except ValueError as error:
            await self._fail(handlers, _invalid_result(error), recording)
            return
        try:
            completed = await asyncio.to_thread(steps.complete, result)
        # Writing the result rows failed, for example because the database
        # was unreachable: nothing was recorded, and the lease makes the call
        # due again.
        except Exception as error:
            recording.not_reached += 1
            logger.exception(
                "simulation_run_result_not_written",
                extra={**handlers.subject, "error_type": type(error).__name__},
            )
            return
        if completed:
            recording.succeeded += 1
            logger.info("simulation_run_succeeded", extra=handlers.subject)

    async def _fail(
        self, handlers: _CallHandlers, error: RunError, recording: RecordingPass
    ) -> None:
        if await asyncio.to_thread(handlers.fail, error):
            recording.failed += 1
            logger.warning(
                "simulation_run_failed",
                extra={**handlers.subject, "error_code": error.code.value},
            )


class RunRecordingLoop:
    """Run recording passes in the background until stopped.

    A pass that raises is logged and the loop goes on: recording problems
    never stop the instance or fail a request.
    """

    def __init__(
        self,
        recorder: RunRecorder,
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        """Run passes of ``recorder``, waiting between them with ``sleep``."""
        self.recorder = recorder
        self._sleep = sleep
        self._task: asyncio.Task[None] | None = None

    async def run(self) -> None:
        """Record outcomes until cancelled; wait less while there is work."""
        while True:
            await self._sleep(IDLE_PASS_INTERVAL_SECONDS)
            try:
                recording = await self.recorder.record_due_outcomes()
            except Exception as error:
                logger.exception(
                    "simulation_run_recording_pass_failed",
                    extra={"error_type": type(error).__name__},
                )
                continue
            while recording.found_work:
                await self._sleep(BUSY_PASS_INTERVAL_SECONDS)
                try:
                    recording = await self.recorder.record_due_outcomes()
                except Exception as error:
                    logger.exception(
                        "simulation_run_recording_pass_failed",
                        extra={"error_type": type(error).__name__},
                    )
                    break

    def start(self) -> None:
        """Start the loop as a background task of the running event loop."""
        if self._task is None:
            self._task = asyncio.create_task(self.run())

    async def stop(self) -> None:
        """Cancel the loop and wait for it to end."""
        if self._task is None:
            return
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task
        self._task = None
