"""Submit, re-run and schedule budget-window reports: one report run per year.

A budget-window report (``simulation.reports.budget_window_size``) compares
its two simulations in each year of its window. Each calculation of it is a
budget-window run on one catalog version (``simulation.budget_window_runs``),
whose years are report runs that ``EconomyReports.run_year`` starts exactly as
it starts a single-year report's: it reuses each year's simulation runs that
exist (the current-law baseline, most often) unless the run is forced, starts
one Modal call per run it created, and starts the year's comparison once both
runs have succeeded.

At most ``max_concurrent_years`` of a run's years are calculated at once, so
one window cannot take every Modal container (a US national year is two
simulations of 20 segment calls each). The next years start when a year's
call reports its end (``start_years_after_call``) and on each safety-check
pass (``resume_waiting_work``). Whoever starts years first claims the run, a
lease of ``SCHEDULING_LEASE``, so two callers do not start the same year;
the database allows one report run per year of a run in any case.

The first year that fails fails the run, with that year's error, and no more
years start; years already running finish and keep their results. Once every
year has succeeded, the run has succeeded. The run stores no totals: they are
the sums of its years' budget rows, added up when it is read.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import timedelta
from typing import TYPE_CHECKING, Protocol

from policyengine_simulation_persistence import (
    BudgetWindowStatus,
    NewBudgetWindowRun,
    ReportKind,
    RunError,
    RunErrorCode,
    RunTrigger,
)
from policyengine_simulation_persistence.run_store import utc_now
from sqlalchemy.exc import IntegrityError

from policyengine_simulation_entry.economy_report_types import (
    EconomyRequestError,
    ReportNotFoundError,
)
from policyengine_simulation_entry.economy_routes import report_run_progress
from policyengine_simulation_entry.economy_schemas import ReportStatus
from policyengine_simulation_entry.run_recording import SAFETY_CHECK_INTERVAL

if TYPE_CHECKING:
    from collections.abc import Callable, Collection
    from datetime import datetime
    from uuid import UUID

    from policyengine_simulation_persistence import (
        BudgetWindowRunRecord,
        BudgetWindowYear,
        CatalogVersion,
        ReportRecord,
        ReportRunRecord,
        StartedBudgetWindowRun,
        YearBudget,
    )

    from policyengine_simulation_entry.economy_report_types import (
        EconomyCatalog,
        EconomyReportSubmission,
        ReportDefinitions,
    )
    from policyengine_simulation_entry.economy_reports import EconomyReports

logger = logging.getLogger(__name__)

# How many of a run's years are calculated at once. A US national year is up
# to two simulation calls of 20 segment calls each, so five years use at most
# 200 of the US segment function's containers.
DEFAULT_MAX_CONCURRENT_YEARS = 5
# A caller starting a run's years holds the run this long; one that stopped
# loses the claim afterwards, and the safety check carries on.
SCHEDULING_LEASE = timedelta(minutes=2)
# A running run is checked by the safety check this long after it was last
# scheduled: its years' calls report their ends, so this only catches a run
# whose next years a stopped caller never started.
BUDGET_WINDOW_CHECK_INTERVAL = SAFETY_CHECK_INTERVAL
RERUN_TRIGGERS = frozenset({RunTrigger.MANUAL, RunTrigger.SYSTEM})
WAITING_WORK_BATCH_SIZE = 50
# A caller tries again after releasing its claim, in case a year finished
# while it held the claim (its own read was then too early); at most this
# many times.
SCHEDULING_ATTEMPTS = 3


class BudgetWindowRuns(Protocol):
    """``BudgetWindowRunStore``'s writes and reads the scheduler needs."""

    def create_run(
        self, new_run: NewBudgetWindowRun, *, first_check_at: datetime
    ) -> BudgetWindowRunRecord:
        """Add a running budget-window run."""
        ...

    def create_first_run(
        self, new_run: NewBudgetWindowRun, *, first_check_at: datetime
    ) -> StartedBudgetWindowRun:
        """Add the report's first budget-window run, or return its newest."""
        ...

    def get_run(self, run_id: UUID) -> BudgetWindowRunRecord | None:
        """Return the run, or None."""
        ...

    def latest_run(self, report_id: UUID) -> BudgetWindowRunRecord | None:
        """Return the report's newest run, or None."""
        ...

    def runs(self, report_id: UUID) -> list[BudgetWindowRunRecord]:
        """Return the report's runs, newest first."""
        ...

    def due_runs(self, *, now: datetime, limit: int) -> list[BudgetWindowRunRecord]:
        """Return running runs whose next check is due."""
        ...

    def years(self, run_id: UUID) -> list[BudgetWindowYear]:
        """Return the years the run has started, with their runs."""
        ...

    def year_budgets(self, run_id: UUID) -> list[YearBudget]:
        """Return each stored year's budget figures, in year order."""
        ...

    def claim_scheduling(self, run_id: UUID, *, lease: timedelta) -> bool:
        """Claim a running run to start its years."""
        ...

    def release_scheduling(self, run_id: UUID, *, next_check_at: datetime) -> None:
        """Release the claim and set the next check."""
        ...

    def finish_run(
        self,
        run_id: UUID,
        status: BudgetWindowStatus,
        error: RunError | None = None,
    ) -> bool:
        """Mark a running run succeeded or failed."""
        ...


class ReportRunLookups(Protocol):
    """``RunStore``'s reads of the report runs a reported call finished."""

    def get_report_run(self, report_run_id: UUID) -> ReportRunRecord | None:
        """Return the report run, or None."""
        ...

    def report_runs_using(self, simulation_run_id: UUID) -> list[ReportRunRecord]:
        """Return the report runs comparing a simulation run."""
        ...


@dataclass(frozen=True)
class BudgetWindowReportSubmission:
    """A new budget-window report: an economy report's fields and a window.

    ``report.year`` is the window's first year.
    """

    report: EconomyReportSubmission
    window_size: int


@dataclass(frozen=True)
class _WindowState:
    """What a run's started years say about it."""

    failed: RunError | None
    succeeded: bool
    in_flight: int
    started_years: frozenset[int]


def _run_error_code(code: str) -> RunErrorCode:
    try:
        return RunErrorCode(code)
    except ValueError:  # pragma: no cover - sim writes only these codes
        return RunErrorCode.WORKER_ERROR


def window_state(report: ReportRecord, years: list[BudgetWindowYear]) -> _WindowState:
    """Return whether a year failed, whether every year succeeded, and how many run.

    A year is failed, succeeded or running as its report run is
    (``report_run_progress``); the first failed year, in year order, is the
    run's error.
    """
    failed: RunError | None = None
    succeeded_years = 0
    in_flight = 0
    for year in years:
        status, _, error = report_run_progress(
            year.report_run, (year.baseline_run, year.reform_run)
        )
        if status is ReportStatus.FAILED and failed is None:
            detail = "" if error is None or not error.message else f": {error.message}"
            failed = RunError(
                _run_error_code(error.code if error is not None else ""),
                f"The {year.report_run.year} calculation failed{detail}",
            )
        elif status is ReportStatus.SUCCEEDED:
            succeeded_years += 1
        elif status is not ReportStatus.FAILED:
            in_flight += 1
    return _WindowState(
        failed=failed,
        succeeded=succeeded_years == len(report.window_years),
        in_flight=in_flight,
        started_years=frozenset(year.report_run.year for year in years),
    )


@dataclass
class BudgetWindowReports:
    """Budget-window reports: submission, re-runs and their years' scheduling."""

    economy: EconomyReports
    reports: ReportDefinitions
    windows: BudgetWindowRuns
    runs: ReportRunLookups
    catalog: EconomyCatalog
    max_concurrent_years: int = DEFAULT_MAX_CONCURRENT_YEARS
    clock: Callable[[], datetime] = field(default=utc_now)

    # --- Reports -----------------------------------------------------------

    async def submit(
        self, submission: BudgetWindowReportSubmission
    ) -> tuple[ReportRecord, BudgetWindowRunRecord]:
        """Create a budget-window report and its first run, and start its years.

        Raise EconomyRequestError, before anything is written, as
        ``EconomyReports.prepare_report`` does.
        """
        setup = await self.economy.prepare_report(
            submission.report, budget_window_size=submission.window_size
        )
        run = await asyncio.to_thread(
            lambda: self.windows.create_run(
                self._new_run(setup.report, setup.version, RunTrigger.INITIAL),
                first_check_at=self._next_check(),
            )
        )
        return setup.report, await self._started(run)

    async def run_report(
        self,
        report_id: UUID,
        *,
        policyengine_version: str,
        trigger: RunTrigger,
        force: bool,
    ) -> BudgetWindowRunRecord:
        """Calculate a budget-window report again on a catalog version.

        ``trigger`` is ``manual`` for a user's re-run and ``system`` for a
        run the caller starts itself; a ``system`` run returns the report's
        newest run when it has one, and simultaneous ones of a report
        without one add a single run. Each year's simulation runs are
        reused unless ``force`` is set. Raise ReportNotFoundError for a
        report that is not a budget-window report, and EconomyRequestError
        for one that cannot run.
        """
        if trigger not in RERUN_TRIGGERS:
            msg = "a report is run again with the manual or system trigger"
            raise EconomyRequestError(msg)
        report = await asyncio.to_thread(self.reports.get, report_id)
        if (
            report is None
            or report.kind is not ReportKind.ECONOMY
            or report.budget_window_size is None
        ):
            msg = f"no budget-window report {report_id}"
            raise ReportNotFoundError(msg)
        if report.options:
            msg = "economy reports take no options yet"
            raise EconomyRequestError(msg)
        version = await self.economy.catalog_version(
            report.country, policyengine_version
        )
        new_run = self._new_run(report, version, trigger, force=force)
        if trigger is RunTrigger.SYSTEM:
            first = await asyncio.to_thread(
                lambda: self.windows.create_first_run(
                    new_run, first_check_at=self._next_check()
                )
            )
            if not first.created:
                return first.run
            return await self._started(first.run)
        run = await asyncio.to_thread(
            lambda: self.windows.create_run(new_run, first_check_at=self._next_check())
        )
        return await self._started(run)

    def _new_run(
        self,
        report: ReportRecord,
        version: CatalogVersion,
        trigger: RunTrigger,
        *,
        force: bool = False,
    ) -> NewBudgetWindowRun:
        return NewBudgetWindowRun(
            report_id=report.id,
            tax_benefit_model_version_id=version.id,
            trigger=trigger,
            max_concurrent_years=self.max_concurrent_years,
            force=force,
        )

    async def _started(self, run: BudgetWindowRunRecord) -> BudgetWindowRunRecord:
        """Start the new run's first years; return it as it then stands."""
        await self.start_years(run.id)
        current = await asyncio.to_thread(self.windows.get_run, run.id)
        return run if current is None else current

    def _next_check(self) -> datetime:
        return self.clock() + BUDGET_WINDOW_CHECK_INTERVAL

    # --- Scheduling --------------------------------------------------------

    async def start_years(self, budget_window_run_id: UUID) -> int:
        """Start the run's next years up to its cap; return how many started.

        A run whose year failed is failed, and one whose years all succeeded
        is succeeded, instead. A run another caller has claimed is left to
        it. The claim is released, with the run's next check, however
        starting ends.
        """
        started = 0
        for _ in range(SCHEDULING_ATTEMPTS):
            claimed = await asyncio.to_thread(
                lambda: self.windows.claim_scheduling(
                    budget_window_run_id, lease=SCHEDULING_LEASE
                )
            )
            if not claimed:
                break
            try:
                started += await self._schedule(budget_window_run_id)
            finally:
                await asyncio.to_thread(
                    lambda: self.windows.release_scheduling(
                        budget_window_run_id, next_check_at=self._next_check()
                    )
                )
            if not await self._has_room(budget_window_run_id):
                break
        return started

    async def _has_room(self, budget_window_run_id: UUID) -> bool:
        """Whether a running run could start a year now, or should finish.

        A year that finished while this caller held the claim, after it read
        the run's years, leaves room it did not use.
        """
        run = await asyncio.to_thread(self.windows.get_run, budget_window_run_id)
        if run is None or run.finished:
            return False
        report = await asyncio.to_thread(self.reports.get, run.report_id)
        if report is None:  # pragma: no cover - a foreign key keeps it
            return False
        state = window_state(
            report, await asyncio.to_thread(self.windows.years, run.id)
        )
        queued = len(report.window_years) - len(state.started_years)
        return (
            state.failed is not None
            or state.succeeded
            or (queued > 0 and state.in_flight < run.max_concurrent_years)
        )

    async def _schedule(self, budget_window_run_id: UUID) -> int:
        """Finish the claimed run, or start its next years; return how many started."""
        run = await asyncio.to_thread(self.windows.get_run, budget_window_run_id)
        if run is None or run.finished:
            return 0
        report = await asyncio.to_thread(self.reports.get, run.report_id)
        if report is None:
            msg = f"budget-window run {run.id} has no report"
            raise LookupError(msg)
        state = window_state(
            report, await asyncio.to_thread(self.windows.years, run.id)
        )
        if state.failed is not None:
            await self._finish(run, BudgetWindowStatus.FAILED, state.failed)
            return 0
        if state.succeeded:
            await self._finish(run, BudgetWindowStatus.SUCCEEDED, None)
            return 0
        room = run.max_concurrent_years - state.in_flight
        queued = [
            year for year in report.window_years if year not in state.started_years
        ]
        if room <= 0 or not queued:
            return 0
        return await self._start_queued(run, report, queued[:room])

    async def _start_queued(
        self, run: BudgetWindowRunRecord, report: ReportRecord, years: list[int]
    ) -> int:
        """Start these years of the run in order; return how many started.

        A year that cannot start fails the run, and one another caller
        started ends this pass.
        """
        version = await asyncio.to_thread(
            self.catalog.catalog_version_by_id, run.tax_benefit_model_version_id
        )
        if version is None:  # pragma: no cover - a foreign key keeps it
            msg = f"budget-window run {run.id} has no catalog version"
            raise LookupError(msg)
        setup = await self.economy.report_setup(report, version)
        started = 0
        for year in years:
            try:
                await self.economy.run_year(
                    setup,
                    year=year,
                    trigger=run.trigger,
                    force=run.force,
                    budget_window_run_id=run.id,
                )
            except EconomyRequestError as error:
                await self._finish(
                    run,
                    BudgetWindowStatus.FAILED,
                    RunError(
                        RunErrorCode.INVALID_INPUT,
                        f"The {year} calculation cannot start: {error}",
                    ),
                )
                return started
            # Another caller started this year after its claim's lease ended.
            except IntegrityError:
                logger.warning(
                    "budget_window_year_already_started",
                    extra={"budget_window_run_id": str(run.id), "year": year},
                )
                return started
            started += 1
        return started

    async def _finish(
        self,
        run: BudgetWindowRunRecord,
        status: BudgetWindowStatus,
        error: RunError | None,
    ) -> None:
        if await asyncio.to_thread(self.windows.finish_run, run.id, status, error):
            logger.info(
                "budget_window_run_finished",
                extra={
                    "budget_window_run_id": str(run.id),
                    "budget_window_status": status.value,
                    "error_code": None if error is None else error.code.value,
                },
            )

    # --- After a call ended, and the safety check --------------------------

    async def start_years_after_call(
        self,
        *,
        simulation_run_ids: Collection[UUID] | None,
        report_run_id: UUID | None,
    ) -> int:
        """Start the years that a reported call's end lets start.

        A finished comparison frees its year's place in its budget-window
        run; a failed simulation run fails the years comparing it, and so
        their runs. Return the years started.
        """
        report_runs: list[ReportRunRecord] = []
        if report_run_id is not None:
            report_run = await asyncio.to_thread(
                self.runs.get_report_run, report_run_id
            )
            if report_run is not None:
                report_runs.append(report_run)
        for simulation_run_id in simulation_run_ids or ():
            report_runs.extend(
                await asyncio.to_thread(self.runs.report_runs_using, simulation_run_id)
            )
        window_ids = sorted(
            {
                report_run.budget_window_run_id
                for report_run in report_runs
                if report_run.budget_window_run_id is not None
            },
            key=str,
        )
        started = 0
        for window_id in window_ids:
            started += await self.start_years(window_id)
        return started

    async def resume_waiting_work(self, *, now: datetime) -> int:
        """Start the years of running runs whose check is due; return how many.

        A run left waiting by a stopped caller, or by a lost report, carries
        on. A run that fails to resume is logged and tried again on the next
        pass, so it cannot stop the others.
        """
        due = await asyncio.to_thread(
            lambda: self.windows.due_runs(now=now, limit=WAITING_WORK_BATCH_SIZE)
        )
        started = 0
        for run in due:
            started += await self._resume(run.id)
        return started

    async def _resume(self, budget_window_run_id: UUID) -> int:
        """Start one due run's years; a failure is logged and leaves it due."""
        try:
            return await self.start_years(budget_window_run_id)
        except Exception as error:  # noqa: BLE001  # logged; the next pass tries again
            logger.warning(
                "budget_window_years_not_resumed",
                extra={
                    "budget_window_run_id": str(budget_window_run_id),
                    "error_type": type(error).__name__,
                },
            )
            return 0
