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
year's coordinator call from its stored rows, then advances the window
(``BudgetWindowStore.advance_window``) with the service's own year starter
(``YearStarts``). That starter derives each later year's call from the
first's, and shares one wait for Modal among them; under the window's row
lock, the earliest years are started, at most five under way, and their
calls recorded.

Just before the advance's first start, the restart marks the earliest
waiting year, so the window is restarted once each time it stops. An
advance whose lock or reads fail starts and marks nothing, so the next
check takes the window up again. A year Modal refuses fails the window, as
it would at the service's start. A window whose first call can't be
rebuilt is failed at once, through the same advance: that year records
``run_not_started`` and why, as a row the check can't rebuild is failed,
unless it changed before the advance took the window's lock.
"""

from __future__ import annotations

import asyncio
from functools import partial
from typing import TYPE_CHECKING

from policyengine_stage12_persistence import (
    RunStatus,
    YearStartDeferred,
    YearStartRefused,
)
from sqlalchemy.exc import SQLAlchemyError

from policyengine_simulation_entry.budget_window_submissions import YearStarts
from policyengine_simulation_entry.run_restart_calls import (
    CallNotRebuiltError,
    DeferredRestartError,
)
from policyengine_simulation_entry.run_restart_outcomes import (
    NOT_STARTED_MESSAGE,
    RestartOutcome,
    log_unexpected_error,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import datetime
    from uuid import UUID

    from policyengine_simulation_persistence import IdleBudgetWindow
    from policyengine_stage12_persistence import WindowAdvance, YearToStart

    from policyengine_simulation_entry.budget_window_submissions import (
        BudgetWindowAdvancer,
    )
    from policyengine_simulation_entry.economy_workers import EconomyRunCaller
    from policyengine_simulation_entry.run_restart_calls import (
        RestartCallBuilder,
        UnstartedRunSource,
    )
    from policyengine_simulation_entry.run_restarts import RestartTelemetry

type _Ended = tuple[RestartOutcome, str | None]


class _MarkedYearStarts:
    """Start a window's years with ``YearStarts``, marking the restart first.

    Before the first start, it marks the earliest waiting year. A mark that
    changes nothing (the year changed meanwhile) or raises defers the year,
    so the advance starts nothing more; an error other than the database's
    is logged with its traceback. It records what happened, so the restart
    can say how it ended.
    """

    def __init__(
        self,
        *,
        window: IdleBudgetWindow,
        mark: Callable[[], bool],
        start_year: YearStarts,
    ) -> None:
        """Mark with ``mark``, then start each of the window's years."""
        self._window = window
        self._mark = mark
        self._start_year = start_year
        self.marked = False
        self.mark_error_type: str | None = None
        self.start_error_type: str | None = None
        self.calls_started = 0

    def __call__(self, year_to_start: YearToStart, /) -> str:
        """Start the year's coordinator and return its Modal call ID."""
        if not self.marked:
            try:
                self.marked = self._mark()
            except SQLAlchemyError as error:
                self.mark_error_type = type(error).__name__
                raise YearStartDeferred from None
            # Any other error is no refusal by Modal: nothing was started.
            except Exception as error:  # noqa: BLE001  # logged with its traceback by log_unexpected_error
                log_unexpected_error(self._window)
                self.mark_error_type = type(error).__name__
                raise YearStartDeferred from None
            if not self.marked:
                raise YearStartDeferred
        try:
            call_id = self._start_year(year_to_start)
        except YearStartDeferred:
            raise
        except Exception as error:
            # The advance fails the year as refused, and logs it.
            self.start_error_type = type(error).__name__
            raise
        self.calls_started += 1
        return call_id

    def ended(self, advance: WindowAdvance) -> _Ended:
        """Return how the restart ended, from the advance it made."""
        if self.mark_error_type is not None:
            return RestartOutcome.DEFERRED, self.mark_error_type
        if not self.marked:
            # Nothing was started: the window finished, or a failed year
            # failed it, or its year changed before the mark.
            return RestartOutcome.LEFT, None
        if advance.status is RunStatus.FAILED:
            return RestartOutcome.FAILED, self.start_error_type
        if advance.started_years:
            return RestartOutcome.RESTARTED, None
        return RestartOutcome.LEFT, None

    def ended_by_error(self) -> RestartOutcome:
        """Return how the restart ended when its advance raised."""
        if self.calls_started:
            # A year whose call Modal accepted claims itself.
            return RestartOutcome.CALL_NOT_RECORDED
        if self.marked:
            # Its failure wasn't written; the sweep fails the window.
            return RestartOutcome.LEFT
        return RestartOutcome.DEFERRED


class _RefusedFirstYear:
    """Fail the year whose call can't be rebuilt, with why.

    The advance offers the earliest year still waiting. When that is the
    year the restart read, it is refused, and the advance fails it, and so
    the window, unless the year's own call claimed it meanwhile. Any other
    year is deferred to its coordinator: the year read has changed since,
    so nothing is failed. Nothing is marked, so a failure that isn't written
    leaves the window for the next check.
    """

    def __init__(self, cannot: CallNotRebuiltError, *, report_run_id: UUID) -> None:
        """Refuse the year of ``report_run_id`` with the reason ``cannot`` gives."""
        self._error_message = (
            f"{NOT_STARTED_MESSAGE}, and its call could not be built again: "
            f"{cannot.reason}."
        )
        self._error_type = cannot.error_type
        self._report_run_id = report_run_id
        self.refused = False

    def __call__(self, year_to_start: YearToStart, /) -> str:
        """Refuse the year read, when it is offered first; defer any other."""
        if self.refused or year_to_start.run_ids.report_run_id != self._report_run_id:
            raise YearStartDeferred
        self.refused = True
        raise YearStartRefused(self._error_message)

    def ended(self, advance: WindowAdvance) -> _Ended:
        """Return how the restart ended, from the advance it made."""
        if self.refused and advance.status is RunStatus.FAILED:
            return RestartOutcome.FAILED, self._error_type
        return RestartOutcome.LEFT, None

    def ended_by_error(self) -> RestartOutcome:
        """Return how the restart ended when its advance raised."""
        return RestartOutcome.DEFERRED


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

    async def restart(self, window: IdleBudgetWindow) -> _Ended:
        """Start the window's earliest waiting years; return how it ended.

        Any error no step expects is logged with its traceback, and the
        window is left as its marks and starts so far leave it.
        """
        starts: _MarkedYearStarts | _RefusedFirstYear | None = None
        try:
            prepared = await self._starts(window)
            if isinstance(prepared, tuple):
                return prepared
            starts = prepared
            return await self._advance(window, starts)
        except Exception as error:  # noqa: BLE001  # one window's fault never ends the check
            log_unexpected_error(window)
            outcome = (
                RestartOutcome.DEFERRED if starts is None else starts.ended_by_error()
            )
            return outcome, type(error).__name__

    async def _starts(
        self, window: IdleBudgetWindow
    ) -> _MarkedYearStarts | _RefusedFirstYear | _Ended:
        """Return the earliest waiting year's starter, or how the restart ended."""
        try:
            waiting = await asyncio.to_thread(
                self._restarts.read_earliest_waiting_year, window.run_id
            )
        except SQLAlchemyError as error:
            return RestartOutcome.DEFERRED, type(error).__name__
        if waiting is None:
            return RestartOutcome.LEFT, None
        try:
            built = await asyncio.to_thread(
                self._calls.report_calculation, waiting.report_run_id
            )
        except DeferredRestartError as deferred:
            return RestartOutcome.DEFERRED, str(deferred)
        except CallNotRebuiltError as cannot:
            return _RefusedFirstYear(cannot, report_run_id=waiting.report_run_id)
        if built is None:
            return RestartOutcome.LEFT, None
        worker, calculation = built
        return _MarkedYearStarts(
            window=window,
            mark=partial(self._restarts.mark_waiting_year, waiting),
            start_year=YearStarts(
                worker=worker,
                caller=self._caller,
                first_year=waiting.year,
                first_calculation=calculation,
                observability_context=self._telemetry.capture_context(),
                loop=asyncio.get_running_loop(),
                timeout_seconds=self._call_start_timeout_seconds,
                clock=self._clock,
            ),
        )

    async def _advance(
        self, window: IdleBudgetWindow, starts: _MarkedYearStarts | _RefusedFirstYear
    ) -> _Ended:
        """Advance the window with ``starts``; return how the restart ended."""
        if self._out_of_time():
            return RestartOutcome.DEFERRED, None
        try:
            advance = await asyncio.to_thread(
                self._windows.advance_window,
                window.run_id,
                now=self._clock(),
                start_year=starts,
            )
        except SQLAlchemyError as error:
            return starts.ended_by_error(), type(error).__name__
        except LookupError:
            return RestartOutcome.LEFT, None
        return starts.ended(advance)
