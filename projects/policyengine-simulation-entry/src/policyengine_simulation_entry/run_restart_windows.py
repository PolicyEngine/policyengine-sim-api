"""Start a budget window again when its waiting years never started.

The simulation service creates a budget window with every year pending with
no Modal call, then starts its first years; each year's coordinator starts
the next when its year ends. When the service stops before starting the
first years, or a coordinator before starting the next, the window stops:
no year is under way, and years wait for a start nothing will make. The
stuck-run sweep fails such a window as stalled once nothing has changed for
its minimum age.

The restart check starts a stopped window again, as the service starts a
new window's first years. ``WindowRestarter`` rebuilds the earliest waiting
year's coordinator call from its stored rows, marks that year, so the window
is restarted once each time it stops, and advances the window
(``BudgetWindowStore.advance_window``) with the service's own year starter
(``YearStarts``). That starter derives each later year's call from the
first's, and shares one wait for Modal among them; under the window's row
lock, the earliest years are started, at most five under way, and their
calls recorded. A year Modal refuses fails the window, as it would at the
service's start. A window whose first call can't be rebuilt is marked and
left, and the sweep fails it as stalled.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from policyengine_simulation_persistence import RestartKind, UnstartedRun
from policyengine_stage12_persistence import RunStatus
from sqlalchemy.exc import SQLAlchemyError

from policyengine_simulation_entry.budget_window_submissions import YearStarts
from policyengine_simulation_entry.run_restart_calls import (
    CallNotRebuiltError,
    DeferredRestartError,
)
from policyengine_simulation_entry.run_restart_outcomes import RestartOutcome

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import datetime

    from policyengine_simulation_persistence import WaitingYear
    from policyengine_stage12_persistence import WindowAdvance

    from policyengine_simulation_entry.budget_window_submissions import (
        BudgetWindowAdvancer,
    )
    from policyengine_simulation_entry.economy_calculation_inputs import (
        RunCalculation,
    )
    from policyengine_simulation_entry.economy_workers import (
        EconomyRunCaller,
        EconomyWorker,
    )
    from policyengine_simulation_entry.run_restart_calls import (
        RestartCallBuilder,
        UnstartedRunSource,
    )
    from policyengine_simulation_entry.run_restarts import RestartTelemetry


class _RestartEnded(Exception):  # noqa: N818  # it names an outcome, not a fault
    """The window's restart ended early, with this outcome."""

    def __init__(self, outcome: RestartOutcome, error_type: str | None = None) -> None:
        """Keep the outcome and the error type that ended it, if any."""
        super().__init__(outcome.value)
        self.outcome = outcome
        self.error_type = error_type


class WindowRestarter:
    """Start a stopped budget window's earliest waiting years again."""

    def __init__(  # noqa: PLR0913  # the service's seams this restart shares
        self,
        *,
        restarts: UnstartedRunSource,
        windows: BudgetWindowAdvancer,
        calls: RestartCallBuilder,
        caller: EconomyRunCaller,
        telemetry: RestartTelemetry,
        clock: Callable[[], datetime],
        out_of_time: Callable[[], bool],
        call_start_timeout_seconds: float,
    ) -> None:
        """Use the restart service's own stores, builder, caller and clocks."""
        self._restarts = restarts
        self._windows = windows
        self._calls = calls
        self._caller = caller
        self._telemetry = telemetry
        self._clock = clock
        self._out_of_time = out_of_time
        self._call_start_timeout_seconds = call_start_timeout_seconds

    async def restart(self, window: UnstartedRun) -> tuple[RestartOutcome, str | None]:
        """Start the window's earliest waiting years; return how it ended."""
        try:
            waiting, built = await self._first_year_call(window)
            worker, calculation = await self._mark(window, waiting, built)
            advance = await self._advance(window, waiting, worker, calculation)
        except _RestartEnded as ended:
            return ended.outcome, ended.error_type
        if advance.status is RunStatus.FAILED:
            return RestartOutcome.FAILED, None
        if advance.started_years:
            return RestartOutcome.RESTARTED, None
        return RestartOutcome.LEFT, None

    async def _first_year_call(
        self, window: UnstartedRun
    ) -> tuple[WaitingYear, tuple[EconomyWorker, RunCalculation] | CallNotRebuiltError]:
        """Return the earliest waiting year and its rebuilt call, or why there's none.

        A call that can't be rebuilt is returned, not raised, so the year is
        still marked.
        """
        try:
            waiting = await asyncio.to_thread(
                self._restarts.read_earliest_waiting_year, window.run_id
            )
        except SQLAlchemyError as error:
            raise _RestartEnded(RestartOutcome.DEFERRED, type(error).__name__) from None
        if waiting is None:
            raise _RestartEnded(RestartOutcome.LEFT)
        try:
            built = await asyncio.to_thread(
                self._calls.report_calculation, waiting.report_run_id
            )
        except DeferredRestartError as deferred:
            raise _RestartEnded(RestartOutcome.DEFERRED, str(deferred)) from None
        except CallNotRebuiltError as cannot:
            return waiting, cannot
        if built is None:
            raise _RestartEnded(RestartOutcome.LEFT)
        return waiting, built

    async def _mark(
        self,
        window: UnstartedRun,
        waiting: WaitingYear,
        built: tuple[EconomyWorker, RunCalculation] | CallNotRebuiltError,
    ) -> tuple[EconomyWorker, RunCalculation]:
        """Mark the earliest waiting year, so the window restarts once per stop.

        Return the year's rebuilt call. Raise _RestartEnded when time is up,
        the year changed meanwhile, the mark can't be written, or the year's
        call can't be rebuilt (once marked).
        """
        if self._out_of_time():
            raise _RestartEnded(RestartOutcome.DEFERRED)
        first_year = UnstartedRun(
            kind=RestartKind.REPORT_RUN,
            run_id=waiting.report_run_id,
            created_at=window.created_at,
        )
        try:
            marked = await asyncio.to_thread(self._restarts.mark_restart, first_year)
        except SQLAlchemyError as error:
            raise _RestartEnded(RestartOutcome.DEFERRED, type(error).__name__) from None
        if not marked:
            raise _RestartEnded(RestartOutcome.LEFT)
        if isinstance(built, CallNotRebuiltError):
            raise _RestartEnded(RestartOutcome.LEFT, built.error_type)
        return built

    async def _advance(
        self,
        window: UnstartedRun,
        waiting: WaitingYear,
        worker: EconomyWorker,
        calculation: RunCalculation,
    ) -> WindowAdvance:
        """Advance the window with the service's year starter, from the first year."""
        start_year = YearStarts(
            worker=worker,
            caller=self._caller,
            first_year=waiting.year,
            first_calculation=calculation,
            observability_context=self._telemetry.capture_context(),
            loop=asyncio.get_running_loop(),
            timeout_seconds=self._call_start_timeout_seconds,
            clock=self._clock,
        )
        try:
            return await asyncio.to_thread(
                self._windows.advance_window,
                window.run_id,
                now=self._clock(),
                start_year=start_year,
            )
        except SQLAlchemyError as error:
            # A year whose call Modal accepted claims itself.
            raise _RestartEnded(
                RestartOutcome.CALL_NOT_RECORDED, type(error).__name__
            ) from None
        except LookupError:
            raise _RestartEnded(RestartOutcome.LEFT) from None
