"""Starting a budget window's years and recording the window's outcome.

A budget-window run is one calculation of a budget-window report; its years
are report runs naming it and their year. The simulation service creates
every year pending with no Modal call, then starts the first ones. When a
year's coordinator ends its year, it starts the earliest years still
waiting, so that at most ``BUDGET_WINDOW_MAX_RUNNING_YEARS`` are under way at
once. A failed year fails the window and every year still waiting, with no
further years started, and once every year has succeeded the window is
marked succeeded.

Each advance locks the budget-window run's row while it starts years and
records their calls, so coordinators ending at once start each year once.
The lock is held across those Modal calls; claims and the window's start
are separate one-statement transactions that never wait on each other, so
other years' coordinators wait only for the lock to be released.
The Modal worker role and the simulation service role may both advance a
window: each may update the columns written here. Only a year's coordinator
call marks a window running, before it claims its year, because only the
Modal worker role may set the window's start time.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from policyengine_simulation_contract.stage12_execution import ReportRunIds
from sqlalchemy import and_, exists, func, or_, select, update

from policyengine_stage12_persistence.report_run_store import (
    ACTIVE_STATUSES,
    RunStatus,
    no_active_report_run_names,
)
from policyengine_stage12_persistence.report_run_tables import (
    budget_window_runs,
    report_runs,
    simulation_runs,
)
from policyengine_stage12_persistence.store import (
    ConnectionOpener,
    create_stage12_engine,
    open_connection_once,
)

if TYPE_CHECKING:
    from collections.abc import Iterator
    from datetime import datetime
    from uuid import UUID

    from sqlalchemy import ColumnElement, Engine
    from sqlalchemy.engine import Connection

logger = logging.getLogger(__name__)

# The most years of one budget window under way at once: running, or started
# with a Modal call recorded and waiting for Modal to run it.
BUDGET_WINDOW_MAX_RUNNING_YEARS = 5

# Each failure records a fixed code, then a summary that holds no
# calculation detail; a failed year's own message stays on that year's run.
YEAR_FAILED_MESSAGE = (
    "budget_window_year_failed: The budget window's {year} report run failed."
)
WINDOW_FAILED_MESSAGE = (
    "budget_window_failed: Another year of the budget window failed before "
    "this year started."
)
CALL_REFUSED_MESSAGE = (
    "modal_call_refused: The call that would have started this year could not "
    "be started ({error_type})."
)
WINDOW_STALLED_MESSAGE = (
    "budget_window_stalled: No year of the budget window was under way to "
    "start the years still waiting."
)


@dataclass(frozen=True)
class YearToStart:
    """A waiting year of a window: its year and the three runs it records on."""

    year: int
    run_ids: ReportRunIds


class YearStarter(Protocol):
    """Starts a year's coordinator call and returns the call's Modal ID.

    It raises when Modal refuses the call.
    """

    def __call__(self, year_to_start: YearToStart, /) -> str:
        """Start the year's coordinator call and return its Modal call ID."""
        ...


@dataclass(frozen=True)
class WindowAdvance:
    """What one advance of a budget-window run did.

    ``status`` is the window's status afterwards, ``changed`` whether this
    advance finished it, and ``started_years`` the years it started, earliest
    first.
    """

    status: RunStatus
    changed: bool
    started_years: tuple[int, ...] = ()


@dataclass(frozen=True)
class _WindowYear:
    """One year of a window as its report run records it."""

    run_ids: ReportRunIds
    year: int
    status: RunStatus
    modal_call_id: str | None
    updated_at: datetime

    @property
    def report_run_id(self) -> UUID:
        """The year's report run."""
        return self.run_ids.report_run_id

    @property
    def waiting(self) -> bool:
        """Whether the year is pending with no call: nothing has started it."""
        return self.status is RunStatus.PENDING and self.modal_call_id is None

    @property
    def under_way(self) -> bool:
        """Whether the year is running, or started and waiting for Modal to run it."""
        return self.status is RunStatus.RUNNING or (
            self.status is RunStatus.PENDING and self.modal_call_id is not None
        )


@dataclass(frozen=True)
class _LockedWindow:
    """A budget-window run's status and last change, read under its row lock."""

    status: RunStatus
    updated_at: datetime

    @property
    def finished(self) -> bool:
        """Whether the window has succeeded or failed."""
        return self.status.value not in ACTIVE_STATUSES


class BudgetWindowStore:
    """Budget-window runs' starts and outcomes in the Public API's run tables."""

    def __init__(
        self,
        database_url: str | None = None,
        *,
        engine: Engine | None = None,
        open_connection: ConnectionOpener = open_connection_once,
    ) -> None:
        """Use ``engine`` if given, else build one from ``database_url``.

        Raises ValueError when neither is given or the URL is invalid.
        """
        if engine is None:
            if database_url is None:
                msg = "budget window database URL is required"
                raise ValueError(msg)
            engine = create_stage12_engine(database_url)
        self.engine = engine
        self._open_connection = open_connection

    @contextmanager
    def _transaction(self, operation_name: str) -> Iterator[Connection]:
        # The opener only opens the connection; every statement and the
        # COMMIT run after it returns.
        connection = self._open_connection(
            self.engine.connect,
            operation_name=operation_name,
        )
        with connection, connection.begin():
            yield connection

    def window_of_year(self, report_run_id: UUID) -> UUID | None:
        """Return the budget-window run a report run is a year of.

        None when the report run is no window's year, or does not exist.
        """
        statement = select(report_runs.c.budget_window_run_id).where(
            report_runs.c.id == report_run_id
        )
        with self._transaction("window_of_year") as connection:
            return connection.execute(statement).scalar_one_or_none()

    def start_window(self, report_run_id: UUID, *, started_at: datetime) -> bool:
        """Mark the window a claimed year belongs to running; return whether it changed.

        Only a pending window changes, so the first year claimed sets its
        start time. A report run that is no budget window's year changes
        nothing.
        """
        window_of_run = (
            select(report_runs.c.budget_window_run_id)
            .where(report_runs.c.id == report_run_id)
            .scalar_subquery()
        )
        statement = (
            update(budget_window_runs)
            .where(
                budget_window_runs.c.id == window_of_run,
                budget_window_runs.c.status == RunStatus.PENDING.value,
            )
            .values(
                status=RunStatus.RUNNING.value,
                started_at=func.coalesce(budget_window_runs.c.started_at, started_at),
                updated_at=started_at,
            )
            .returning(budget_window_runs.c.id)
        )
        with self._transaction("start_window") as connection:
            return connection.execute(statement).scalar_one_or_none() is not None

    def advance_window(
        self,
        window_run_id: UUID,
        *,
        now: datetime,
        start_year: YearStarter | None = None,
        max_running_years: int = BUDGET_WINDOW_MAX_RUNNING_YEARS,
    ) -> WindowAdvance:
        """Finish the window if its years have ended, else start its waiting years.

        A finished window is left as it is. A failed year fails the window
        and its waiting years, and every year succeeded marks it succeeded.
        Otherwise, with ``start_year``, the earliest waiting years are
        started until ``max_running_years`` are under way, and each started
        year's call is recorded while the year is still pending with no call;
        a call its coordinator already claimed under keeps that claim. A
        start that raises fails that year as refused by Modal, and so the
        window. Raises LookupError when the window run does not exist.
        """
        with self._transaction("advance_window") as connection:
            window = _lock_window(connection, window_run_id)
            if window.finished:
                return WindowAdvance(status=window.status, changed=False)
            years = _window_years(connection, window_run_id)
            finished = _finish_ended_window(connection, window_run_id, years, now=now)
            if finished is not None:
                return finished
            if start_year is None:
                return WindowAdvance(status=window.status, changed=False)
            return _start_waiting_years(
                connection,
                window_run_id,
                years,
                status=window.status,
                start_year=start_year,
                free_places=max_running_years - sum(year.under_way for year in years),
                now=now,
            )

    def list_idle_windows(self, *, idle_since_before: datetime) -> tuple[UUID, ...]:
        """Return the unfinished windows no year is advancing, oldest first.

        A window is idle when none of its years is under way and neither it
        nor any of its years has changed since ``idle_since_before``: its
        last year ended, or the simulation service created it, without
        anything recording what follows.
        """
        year_of_window = report_runs.c.budget_window_run_id == budget_window_runs.c.id
        last_year_change = (
            select(func.max(report_runs.c.updated_at))
            .where(year_of_window)
            .scalar_subquery()
        )
        statement = (
            select(budget_window_runs.c.id)
            .where(
                budget_window_runs.c.status.in_(ACTIVE_STATUSES),
                ~exists().where(year_of_window, _under_way()),
                func.greatest(
                    budget_window_runs.c.updated_at,
                    func.coalesce(last_year_change, budget_window_runs.c.updated_at),
                )
                < idle_since_before,
            )
            .order_by(budget_window_runs.c.created_at, budget_window_runs.c.id)
        )
        with self._transaction("list_idle_windows") as connection:
            return tuple(connection.execute(statement).scalars())

    def settle_idle_window(
        self,
        window_run_id: UUID,
        *,
        idle_since_before: datetime,
        now: datetime,
    ) -> WindowAdvance:
        """Finish a window that is still idle, as ``list_idle_windows`` judged it.

        A window whose years have all ended is finished as an advance would
        finish it; one with years still waiting fails as stalled, with those
        years. A window that finished, has a year under way, or changed since
        ``idle_since_before`` is left as it is. Raises LookupError when the
        window run does not exist.
        """
        with self._transaction("settle_idle_window") as connection:
            window = _lock_window(connection, window_run_id)
            if window.finished:
                return WindowAdvance(status=window.status, changed=False)
            years = _window_years(connection, window_run_id)
            last_change = max([window.updated_at, *(year.updated_at for year in years)])
            if any(year.under_way for year in years) or (
                last_change >= idle_since_before
            ):
                return WindowAdvance(status=window.status, changed=False)
            finished = _finish_ended_window(connection, window_run_id, years, now=now)
            if finished is not None:
                return finished
            _fail_window(
                connection,
                window_run_id,
                WINDOW_STALLED_MESSAGE,
                now=now,
                waiting_years_message=WINDOW_STALLED_MESSAGE,
            )
            return WindowAdvance(status=RunStatus.FAILED, changed=True)


def _lock_window(connection: Connection, window_run_id: UUID) -> _LockedWindow:
    """Lock the window run's row until the transaction ends, and read it."""
    locked = connection.execute(
        select(budget_window_runs.c.status, budget_window_runs.c.updated_at)
        .where(budget_window_runs.c.id == window_run_id)
        .with_for_update()
    ).one_or_none()
    if locked is None:
        msg = f"budget-window run {window_run_id} does not exist"
        raise LookupError(msg)
    return _LockedWindow(status=RunStatus(locked.status), updated_at=locked.updated_at)


def _window_years(connection: Connection, window_run_id: UUID) -> list[_WindowYear]:
    """Return the window's years, earliest first."""
    statement = (
        select(
            report_runs.c.id,
            report_runs.c.baseline_simulation_run_id,
            report_runs.c.reform_simulation_run_id,
            report_runs.c.year,
            report_runs.c.status,
            report_runs.c.modal_call_id,
            report_runs.c.updated_at,
        )
        .where(report_runs.c.budget_window_run_id == window_run_id)
        .order_by(report_runs.c.year)
    )
    return [
        _WindowYear(
            run_ids=ReportRunIds(
                report_run_id=row.id,
                baseline_simulation_run_id=row.baseline_simulation_run_id,
                reform_simulation_run_id=row.reform_simulation_run_id,
            ),
            year=row.year,
            status=RunStatus(row.status),
            modal_call_id=row.modal_call_id,
            updated_at=row.updated_at,
        )
        for row in connection.execute(statement)
    ]


def _finish_ended_window(
    connection: Connection,
    window_run_id: UUID,
    years: list[_WindowYear],
    *,
    now: datetime,
) -> WindowAdvance | None:
    """Fail the window for its earliest failed year, or mark it succeeded.

    Returns None while no year has failed and not every year has succeeded.
    """
    failed_year = next(
        (year.year for year in years if year.status is RunStatus.FAILED), None
    )
    if failed_year is not None:
        _fail_window(
            connection,
            window_run_id,
            YEAR_FAILED_MESSAGE.format(year=failed_year),
            now=now,
        )
        return WindowAdvance(status=RunStatus.FAILED, changed=True)
    if years and all(year.status is RunStatus.SUCCEEDED for year in years):
        connection.execute(
            update(budget_window_runs)
            .where(
                budget_window_runs.c.id == window_run_id,
                budget_window_runs.c.status.in_(ACTIVE_STATUSES),
            )
            .values(
                status=RunStatus.SUCCEEDED.value,
                error_message=None,
                completed_at=now,
                updated_at=now,
            )
        )
        return WindowAdvance(status=RunStatus.SUCCEEDED, changed=True)
    return None


def _start_waiting_years(  # noqa: PLR0913  # the window's locked state, the starter and the clock
    connection: Connection,
    window_run_id: UUID,
    years: list[_WindowYear],
    *,
    status: RunStatus,
    start_year: YearStarter,
    free_places: int,
    now: datetime,
) -> WindowAdvance:
    """Start the earliest waiting years in the free places and record their calls.

    A start that raises fails that year and the window, and starts nothing
    more.
    """
    started: list[int] = []
    for year in [year for year in years if year.waiting][: max(free_places, 0)]:
        try:
            modal_call_id = start_year(
                YearToStart(year=year.year, run_ids=year.run_ids)
            )
        except Exception as error:  # noqa: BLE001  # any refusal to start the call fails the year and so the window
            # The error's text may hold connection details; its type is logged.
            logger.warning(
                "Could not start budget-window year %s, report run %s (%s)",
                year.year,
                year.report_run_id,
                type(error).__name__,
            )
            _fail_waiting_years(
                connection,
                window_run_id,
                CALL_REFUSED_MESSAGE.format(error_type=type(error).__name__),
                now=now,
                only=year.report_run_id,
            )
            _fail_window(
                connection,
                window_run_id,
                YEAR_FAILED_MESSAGE.format(year=year.year),
                now=now,
            )
            return WindowAdvance(
                status=RunStatus.FAILED, changed=True, started_years=tuple(started)
            )
        connection.execute(
            update(report_runs)
            .where(report_runs.c.id == year.report_run_id, _waiting())
            .values(modal_call_id=modal_call_id, updated_at=now)
        )
        started.append(year.year)
    return WindowAdvance(status=status, changed=False, started_years=tuple(started))


def _fail_window(
    connection: Connection,
    window_run_id: UUID,
    error_message: str,
    *,
    now: datetime,
    waiting_years_message: str = WINDOW_FAILED_MESSAGE,
) -> None:
    """Fail the window run, then its waiting years and their own simulation runs.

    The window records ``error_message``, and its waiting years
    ``waiting_years_message``: by default, that another year failed.
    """
    connection.execute(
        update(budget_window_runs)
        .where(
            budget_window_runs.c.id == window_run_id,
            budget_window_runs.c.status.in_(ACTIVE_STATUSES),
        )
        .values(
            status=RunStatus.FAILED.value,
            error_message=error_message,
            completed_at=now,
            updated_at=now,
        )
    )
    _fail_waiting_years(connection, window_run_id, waiting_years_message, now=now)


def _fail_waiting_years(
    connection: Connection,
    window_run_id: UUID,
    error_message: str,
    *,
    now: datetime,
    only: UUID | None = None,
) -> None:
    """Fail the window's waiting years (or only one) and their unstarted simulations.

    Each failed year's simulation runs get the same message. A simulation run
    is failed only while it is pending with no call and no pending or running
    report run names it, so a run another report shares is left to that
    report.
    """
    conditions = [report_runs.c.budget_window_run_id == window_run_id, _waiting()]
    if only is not None:
        conditions.append(report_runs.c.id == only)
    failed_years = connection.execute(
        update(report_runs)
        .where(*conditions)
        .values(
            status=RunStatus.FAILED.value,
            error_message=error_message,
            completed_at=now,
            updated_at=now,
        )
        .returning(
            report_runs.c.baseline_simulation_run_id,
            report_runs.c.reform_simulation_run_id,
        )
    ).all()
    simulation_run_ids = {
        run_id
        for failed_year in failed_years
        for run_id in failed_year
        if run_id is not None
    }
    if not simulation_run_ids:
        return
    connection.execute(
        update(simulation_runs)
        .where(
            simulation_runs.c.id.in_(simulation_run_ids),
            simulation_runs.c.status == RunStatus.PENDING.value,
            simulation_runs.c.modal_call_id.is_(None),
            no_active_report_run_names(simulation_runs),
        )
        .values(
            status=RunStatus.FAILED.value,
            error_message=error_message,
            completed_at=now,
            updated_at=now,
        )
    )


def _waiting() -> ColumnElement[bool]:
    """Return the condition that a report run is pending with no call recorded."""
    return and_(
        report_runs.c.status == RunStatus.PENDING.value,
        report_runs.c.modal_call_id.is_(None),
    )


def _under_way() -> ColumnElement[bool]:
    """Return the condition that a report run is running, or started and pending."""
    return or_(
        report_runs.c.status == RunStatus.RUNNING.value,
        and_(
            report_runs.c.status == RunStatus.PENDING.value,
            report_runs.c.modal_call_id.is_not(None),
        ),
    )
