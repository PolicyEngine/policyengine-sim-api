"""Start a run or household job again when its Modal call never started.

The simulation service creates a household job, a report run or a standalone
simulation run ``pending`` with no Modal call ID, starts the call that
calculates it, then records the call's ID. When the service stops between
the insert and the start (a Cloud Run instance shut down, for example),
nothing records a call, and the stuck-run sweep would fail the run 15
minutes after its creation. The restart check, a scheduled Modal function
beside the sweep, starts such a row's call once more:

1. it lists the rows created 3 to 12 minutes ago that are still pending with
   no call and that nothing has changed since their insert;
2. for each, it reads back what the service built the call from and builds
   the same call, on the worker of the .py version the row records;
3. it marks the row, so it is never restarted twice, starts the call, and
   records the call's ID while the row is still pending with none.

A row whose call can't be built again (its worker left the version manifest,
or its stored rows changed), or whose new call Modal refuses, is failed at
once with ``run_not_started``, as the sweep would fail it later. A row whose
rows or manifest can't be read now is left unmarked, for the next check. A
call the service started after all claims the row only while the row records
no call or that call's own ID, so once the restart records its call, the
earlier call's claim is refused; if the earlier call claimed it first, the
restart's call is refused instead.

Each listed row is one telemetry scope, ``restart_scope``, with its age, and
its outcome is reported through ``record_outcome``, so how often runs need a
restart, and how each restart ended, can be counted.
"""

from __future__ import annotations

import asyncio
import uuid
from collections import Counter
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import TYPE_CHECKING, Final, Protocol

from policyengine_simulation_persistence import RestartKind
from sqlalchemy.exc import SQLAlchemyError

from policyengine_simulation_entry.run_restart_calls import (
    CallNotRebuiltError,
    DeferredRestartError,
    HouseholdJobRestarts,
    RestartCallBuilder,
    RestartCallers,
    RestartSettings,
    RestartWorkers,
    UnstartedRunSource,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from contextlib import AbstractContextManager
    from uuid import UUID

    from policyengine_simulation_persistence import (
        ReportRunStatus,
        SimulationStatus,
        UnstartedRun,
    )

    from policyengine_simulation_entry.run_restart_calls import CallStart

__all__ = [
    "CALL_START_TIMEOUT_SECONDS",
    "RESTARTS_PER_CHECK",
    "RESTART_AFTER",
    "RESTART_UNTIL",
    "EconomyRunWrites",
    "NoRestartTelemetry",
    "RestartCallers",
    "RestartOutcome",
    "RestartSettings",
    "RestartStores",
    "RestartSummary",
    "RestartTelemetry",
    "RestartWorkers",
    "RunRestartService",
]

# A row is restarted once it has stayed pending with no call this long, which
# is far longer than a submission takes to start and record its call.
RESTART_AFTER: Final = timedelta(minutes=3)
# A row older than this is left to the stuck-run sweep, which fails it at 15
# minutes, so a restart has time to record its call before the sweep judges it.
RESTART_UNTIL: Final = timedelta(minutes=12)
# At most this many rows are restarted by one check; the next takes the rest.
# With every call waited on for its full time, a check still ends within the
# scheduled function's five-minute timeout, so no row it marks is left
# without its call.
RESTARTS_PER_CHECK: Final = 20
# How long the check waits for Modal to accept a restarted call.
CALL_START_TIMEOUT_SECONDS: Final = 10.0
_NOT_STARTED: Final = (
    "run_not_started: The run stayed pending with no Modal call recorded for it"
)
_PENDING: Final = "pending"


class RestartOutcome(StrEnum):
    """How the check ended for one listed row.

    ``restarted``: its new call started and the call's ID was recorded.
    ``call_not_recorded``: its new call started, but the ID couldn't be
    recorded; the call claims the row itself. ``started_meanwhile``: another
    call claimed the row after the check started its own, which that call
    then can't claim. ``left``: the row was gone, or changed before the check
    started a call; also a row whose failure couldn't be written, which the
    sweep fails later. ``deferred``: its rows or the manifest couldn't be
    read, so it is left unmarked for the next check. ``failed``: it couldn't
    be started again and was failed with ``run_not_started``.
    """

    RESTARTED = "restarted"
    CALL_NOT_RECORDED = "call_not_recorded"
    STARTED_MEANWHILE = "started_meanwhile"
    LEFT = "left"
    DEFERRED = "deferred"
    FAILED = "failed"


@dataclass(frozen=True)
class RestartSummary:
    """How many rows one check listed, and how each ended (``RestartOutcome``)."""

    listed: int
    restarted: int = 0
    call_not_recorded: int = 0
    started_meanwhile: int = 0
    left: int = 0
    deferred: int = 0
    failed: int = 0


class EconomyRunWrites(Protocol):
    """The submission's own call writes on economy runs (``EconomyRunStore``)."""

    def record_report_call(
        self, report_run_id: UUID, modal_call_id: str
    ) -> ReportRunStatus | None:
        """Record the report run's call ID while it is pending with none."""
        ...

    def fail_unstarted_report(
        self,
        report_run_id: UUID,
        simulation_run_ids: Sequence[UUID],
        error_message: str,
    ) -> bool:
        """Fail the report run, and its simulation runs, while unstarted."""
        ...

    def record_simulation_call(
        self, simulation_run_id: UUID, modal_call_id: str
    ) -> SimulationStatus | None:
        """Record the simulation run's call ID while it is pending with none."""
        ...

    def fail_unstarted_simulation(
        self, simulation_run_id: UUID, error_message: str
    ) -> bool:
        """Fail the simulation run while it is pending with no call ID."""
        ...


class RestartTelemetry(Protocol):
    """Where the check reports each listed row (``ObservabilityRestartTelemetry``)."""

    def restart_scope(
        self, run: UnstartedRun, *, age_seconds: float
    ) -> AbstractContextManager[None]:
        """Return the scope one row's restart runs in."""
        ...

    def record_outcome(
        self,
        run: UnstartedRun,
        outcome: RestartOutcome,
        *,
        error_type: str | None = None,
    ) -> None:
        """Report how the row's restart ended, and the error type that ended it."""
        ...

    def capture_context(self) -> dict[str, str] | None:
        """Return the context a restarted call continues the check's trace from."""
        ...


class NoRestartTelemetry:
    """Report nothing."""

    def restart_scope(
        self,
        run: UnstartedRun,  # noqa: ARG002  # the protocol's argument
        *,
        age_seconds: float,  # noqa: ARG002  # the protocol's argument
    ) -> AbstractContextManager[None]:
        """Return a scope that does nothing."""
        return nullcontext()

    def record_outcome(
        self,
        run: UnstartedRun,
        outcome: RestartOutcome,
        *,
        error_type: str | None = None,
    ) -> None:
        """Report nothing."""

    def capture_context(self) -> dict[str, str] | None:
        """Return no context."""
        return None


@dataclass(frozen=True)
class RestartStores:
    """The stores a restart reads and writes."""

    restarts: UnstartedRunSource
    economy: EconomyRunWrites
    households: HouseholdJobRestarts


class RunRestartService:
    """Start the calls of the runs and household jobs whose call never started.

    The store reads and writes are synchronous: the check is a batch job of
    its own, so nothing else waits on its event loop.
    """

    def __init__(  # noqa: PLR0913  # each seam tests replace
        self,
        *,
        stores: RestartStores,
        workers: RestartWorkers,
        callers: RestartCallers,
        settings: RestartSettings,
        telemetry: RestartTelemetry | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        call_start_timeout_seconds: float = CALL_START_TIMEOUT_SECONDS,
        new_request_id: Callable[[], str] = lambda: f"run-restart-{uuid.uuid4()}",
    ) -> None:
        """Use these seams; tests replace each of them."""
        self._stores = stores
        self._calls = RestartCallBuilder(
            restarts=stores.restarts,
            households=stores.households,
            workers=workers,
            callers=callers,
            settings=settings,
            new_request_id=new_request_id,
        )
        self._telemetry = telemetry or NoRestartTelemetry()
        self._clock = clock
        self._call_start_timeout_seconds = call_start_timeout_seconds

    async def restart_unstarted_runs(self) -> RestartSummary:
        """Start once more each row created 3 to 12 minutes ago with no call.

        Raise the store's error when the rows can't be listed.
        """
        now = self._clock()
        runs = self._stores.restarts.list_unstarted(
            created_after=now - RESTART_UNTIL,
            created_before=now - RESTART_AFTER,
            limit=RESTARTS_PER_CHECK,
        )
        outcomes: Counter[RestartOutcome] = Counter()
        for run in runs:
            age_seconds = (now - run.created_at).total_seconds()
            with self._telemetry.restart_scope(run, age_seconds=age_seconds):
                outcome, error_type = await self._restart(run)
                self._telemetry.record_outcome(run, outcome, error_type=error_type)
            outcomes[outcome] += 1
        return RestartSummary(
            listed=len(runs),
            **{outcome.value: count for outcome, count in outcomes.items()},
        )

    async def _restart(self, run: UnstartedRun) -> tuple[RestartOutcome, str | None]:
        """Start the row's call once more; return how it ended and any error type."""
        try:
            rebuilt = self._calls.build(run)
        except DeferredRestartError as deferred:
            return RestartOutcome.DEFERRED, str(deferred)
        except CallNotRebuiltError as cannot:
            message = (
                f"{_NOT_STARTED}, and its call could not be built again: "
                f"{cannot.reason}."
            )
            failed = self._fail(run, message, cannot.simulation_run_ids)
            # An unmarked row whose failure couldn't be written is listed again.
            outcome = {
                True: RestartOutcome.FAILED,
                False: RestartOutcome.LEFT,
                None: RestartOutcome.DEFERRED,
            }[failed]
            return outcome, cannot.error_type
        if rebuilt is None:
            return RestartOutcome.LEFT, None
        return await self._start_marked(run, rebuilt.start, rebuilt.simulation_run_ids)

    async def _start_marked(
        self,
        run: UnstartedRun,
        start_call: CallStart,
        simulation_run_ids: tuple[UUID, ...],
    ) -> tuple[RestartOutcome, str | None]:
        """Mark the row, then start its call and record the call's ID."""
        try:
            if not self._stores.restarts.mark_restart(run):
                return RestartOutcome.LEFT, None
        except SQLAlchemyError as error:
            return RestartOutcome.DEFERRED, type(error).__name__
        try:
            call_id = await asyncio.wait_for(
                start_call(self._telemetry.capture_context()),
                timeout=self._call_start_timeout_seconds,
            )
        # Modal raises its own errors; any of them, or the timeout, means the
        # call was not confirmed, and the row is failed unless it has started.
        except Exception as error:  # noqa: BLE001  # every unconfirmed call is answered alike
            message = (
                f"{_NOT_STARTED}, and Modal did not accept the call that started it "
                f"again ({type(error).__name__})."
            )
            failed = self._fail(run, message, simulation_run_ids)
            # A failure that changed nothing means another call claimed the
            # row; one that couldn't be written leaves the row to the sweep.
            outcome = {
                True: RestartOutcome.FAILED,
                False: RestartOutcome.STARTED_MEANWHILE,
                None: RestartOutcome.LEFT,
            }[failed]
            return outcome, type(error).__name__
        return self._recorded(run, call_id)

    def _recorded(
        self, run: UnstartedRun, call_id: str
    ) -> tuple[RestartOutcome, str | None]:
        """Record the started call's ID; return how the restart ended."""
        try:
            status = self._record(run, call_id)
        except SQLAlchemyError as error:
            return RestartOutcome.CALL_NOT_RECORDED, type(error).__name__
        if status is None:
            return RestartOutcome.LEFT, None
        if status == _PENDING:
            return RestartOutcome.RESTARTED, None
        return RestartOutcome.STARTED_MEANWHILE, None

    def _fail(
        self, run: UnstartedRun, message: str, simulation_run_ids: tuple[UUID, ...]
    ) -> bool | None:
        """Fail the row while it is unstarted; return whether it was failed.

        Return None when the failure couldn't be written.
        """
        try:
            if run.kind is RestartKind.REPORT_RUN:
                failed = self._stores.economy.fail_unstarted_report(
                    run.run_id, simulation_run_ids, message
                )
            elif run.kind is RestartKind.SIMULATION_RUN:
                failed = self._stores.economy.fail_unstarted_simulation(
                    run.run_id, message
                )
            else:
                failed = self._stores.households.fail_unstarted(run.run_id, message)
        except SQLAlchemyError:
            return None
        return failed

    def _record(self, run: UnstartedRun, call_id: str) -> str | None:
        """Record the call's ID while the row is unstarted; return its status after."""
        if run.kind is RestartKind.REPORT_RUN:
            status = self._stores.economy.record_report_call(run.run_id, call_id)
        elif run.kind is RestartKind.SIMULATION_RUN:
            status = self._stores.economy.record_simulation_call(run.run_id, call_id)
        else:
            status = self._stores.households.record_call(run.run_id, call_id)
        return None if status is None else str(status)
