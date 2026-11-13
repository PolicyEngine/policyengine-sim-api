"""Creating budget-window reports and starting their first years.

A budget-window report is created as an economy report with one report run
per year of its window (``NewBudgetWindowReport``), all pending with no
Modal call, in one transaction. The service then advances the window as its
own role (``BudgetWindowStore.advance_window``): under the window run's row
lock, up to the earliest five years' coordinators are started and their
calls recorded. Each year's coordinator starts the next years when its year ends.

The first year's coordinator input is built as a single-year report's is;
every later year's is derived from it (``year_calculation``), so all years
are calculated alike. A year Modal refuses fails the window, and so does
the first year when Modal doesn't confirm it in time; the request is then
answered 503 naming the report, as an economy report whose run Modal
refused is. The first years share one wait for Modal, and a later year not
confirmed within it is left waiting for a started year's coordinator.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Protocol
from uuid import UUID, uuid4

from policyengine_simulation_contract.stage12_execution import (
    ReportExecutionInput,
    ReportRunIds,
    Stage12InvocationContext,
    year_calculation,
)
from policyengine_simulation_persistence import (
    BudgetWindowYear,
    NewBudgetWindowReport,
    ReportRunStatus,
    RunReuse,
    SimulationChoice,
)
from policyengine_stage12_persistence import RunStatus, YearStartDeferred
from sqlalchemy.exc import SQLAlchemyError

from policyengine_simulation_entry.budget_window_api_models import (
    BudgetWindowReportCreated,
    BudgetWindowYearCreated,
)
from policyengine_simulation_entry.economy_calculation_inputs import (
    report_run_calculation,
)
from policyengine_simulation_entry.economy_submission_errors import call_refused
from policyengine_simulation_entry.economy_submissions import (
    CALL_START_TIMEOUT_SECONDS,
    EconomyRuns,
    EconomySubmissionService,
    ServiceSettings,
    calculation_scope,
    economy_request_for,
    simulation_choice,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from policyengine_simulation_persistence import (
        BudgetWindowInputs,
        EconomyRequest,
        ReusableRun,
        SimulationInputs,
        WindowYearRuns,
    )
    from policyengine_stage12_persistence import (
        WindowAdvance,
        YearStarter,
        YearToStart,
    )

    from policyengine_simulation_entry.budget_window_api_models import (
        BudgetWindowReportRequest,
    )
    from policyengine_simulation_entry.economy_calculation_inputs import (
        RunCalculation,
    )
    from policyengine_simulation_entry.economy_workers import (
        EconomyRunCaller,
        EconomyWorker,
        EconomyWorkerSource,
    )

logger = logging.getLogger(__name__)


class BudgetWindowRuns(EconomyRuns, Protocol):
    """The economy store's reads and writes, with budget windows'."""

    def read_budget_window_inputs(
        self,
        request: EconomyRequest,
        *,
        baseline: SimulationChoice,
        reform: SimulationChoice,
        reuse: RunReuse | None,
        years: Sequence[int],
    ) -> BudgetWindowInputs:
        """Read the window's catalog rows, both sides' values and each year's runs."""
        ...

    def create_budget_window_report(self, report: NewBudgetWindowReport) -> None:
        """Create the window's report, its budget-window run and each year's runs."""
        ...


class BudgetWindowAdvancer(Protocol):
    """Starts a window's waiting years (``BudgetWindowStore`` as the service)."""

    def advance_window(
        self,
        window_run_id: UUID,
        *,
        now: datetime,
        start_year: YearStarter | None = None,
    ) -> WindowAdvance:
        """Start the earliest waiting years, at most five under way."""
        ...


class ReportSubmissionService[Store: BudgetWindowRuns = BudgetWindowRuns](
    EconomySubmissionService[Store]
):
    """Create economy and budget-window reports and simulations, and start them.

    ``Store`` is the store's type, as for ``EconomySubmissionService``.
    """

    def __init__(  # noqa: PLR0913  # the window seams, then the economy service's
        self,
        *,
        windows: BudgetWindowAdvancer,
        store: Store,
        workers: EconomyWorkerSource,
        caller: EconomyRunCaller,
        settings: ServiceSettings,
        call_start_timeout_seconds: float = CALL_START_TIMEOUT_SECONDS,
        new_id: Callable[[], UUID] = uuid4,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        """Use these seams; tests replace each of them."""
        super().__init__(
            store=store,
            workers=workers,
            caller=caller,
            settings=settings,
            call_start_timeout_seconds=call_start_timeout_seconds,
            new_id=new_id,
            clock=clock,
        )
        self._windows = windows

    async def submit_budget_window(
        self,
        request: BudgetWindowReportRequest,
        *,
        request_id: str,
        observability_context: dict[str, Any] | None,
    ) -> BudgetWindowReportCreated:
        """Create the window's report and its years' runs, and start its first years.

        Raise the errors ``submit_report`` raises; the 503 after a write
        names the report, whose window was failed.
        """
        worker = await self._default_worker(request.country)
        options = request.options()
        reuse_prefix = self._settings.reuse_prefix
        reuse = (
            None
            if reuse_prefix is None
            else RunReuse(
                versions=worker.versions,
                inputs=options,
                output_uri_prefix=reuse_prefix,
            )
        )
        economy_request = economy_request_for(request, worker)
        inputs = await self._read_inputs(
            lambda: self._store.read_budget_window_inputs(
                economy_request,
                baseline=simulation_choice(request.baseline),
                reform=simulation_choice(request.reform),
                reuse=reuse,
                years=request.years,
            ),
            request=economy_request,
        )
        report = NewBudgetWindowReport(
            id=self._new_id(),
            window_run_id=self._new_id(),
            country=request.country,
            label=request.label,
            description=request.description,
            tax_benefit_model_id=inputs.catalog.model_id,
            population=inputs.catalog.population(request.year),
            versions=worker.versions,
            inputs=options,
            years=tuple(self._window_year(inputs, year) for year in request.years),
        )
        first_year = report.first_year
        first_calculation = report_run_calculation(
            worker,
            calculation_scope(request, worker, inputs.catalog, spm=options.get("spm")),
            self._origin(request_id),
            run_ids=year_run_ids(first_year),
            baseline_policy=inputs.baseline.reform,
            reform_policy=inputs.reform.reform,
        )
        await self._create(
            "report", lambda: self._store.create_budget_window_report(report)
        )
        status = await self._start_first_years(
            worker,
            first_calculation,
            report_id=report.id,
            window_run_id=report.window_run_id,
            first_year=first_year.year,
            observability_context=observability_context,
        )
        return BudgetWindowReportCreated(
            report_id=report.id,
            report_run_id=first_year.report_run_id,
            budget_window_run_id=report.window_run_id,
            status=status,
            years=[year_created(window_year) for window_year in report.years],
        )

    def _window_year(self, inputs: BudgetWindowInputs, year: int) -> BudgetWindowYear:
        """Return a year naming its own year's reusable runs, or new ones."""
        baseline_run, reform_run = inputs.reusable_runs[year]
        return BudgetWindowYear(
            year=year,
            report_run_id=self._new_id(),
            baseline=self._side(_with_reusable(inputs.baseline, baseline_run)),
            reform=self._side(_with_reusable(inputs.reform, reform_run)),
        )

    async def _start_first_years(  # noqa: PLR0913  # the call, then the window it starts
        self,
        worker: EconomyWorker,
        first_calculation: RunCalculation,
        *,
        report_id: UUID,
        window_run_id: UUID,
        first_year: int,
        observability_context: dict[str, Any] | None,
        rerun: bool = False,
    ) -> ReportRunStatus:
        """Advance the new window run, starting its first years; return its status.

        Raise EconomySubmissionUnavailableError naming the report when the
        window failed because Modal refused a year, or didn't confirm the
        first in time; a later year not confirmed within the shared wait is
        left waiting. An advance that can't be written leaves the window pending:
        the report exists, so it is answered as created, and a year Modal
        started claims itself. A window no year advances is started again by
        the restart check (``run_restart_windows``), and failed as stalled by
        the stuck-run sweep when that start is lost too. ``rerun`` says the
        report existed before the window run, which the 503 then names.
        """
        start_year = YearStarts(
            worker=worker,
            caller=self._caller,
            first_year=first_year,
            first_calculation=first_calculation,
            observability_context=observability_context,
            loop=asyncio.get_running_loop(),
            timeout_seconds=self._call_start_timeout_seconds,
            clock=self._clock,
        )
        try:
            advance = await asyncio.to_thread(
                self._windows.advance_window,
                window_run_id,
                now=self._clock(),
                start_year=start_year,
            )
        except SQLAlchemyError as error:
            logger.warning(
                "budget_window_not_started",
                extra={
                    "record": "report",
                    "record_id": str(report_id),
                    "error_type": type(error).__name__,
                },
            )
            return ReportRunStatus.PENDING
        if advance.status is RunStatus.FAILED:
            # The budget-window store logs the refused call's error type.
            logger.error(
                "budget_window_call_unconfirmed",
                extra={
                    "record": "report",
                    "record_id": str(report_id),
                    "started_years": list(advance.started_years),
                },
            )
            refused = call_refused(
                "report", report_id, rerun_id=window_run_id if rerun else None
            )
            raise refused from None
        return ReportRunStatus(advance.status.value)


class YearStarts:
    """Start a window year's coordinator from the advance's thread; return its call.

    The first year's call takes the input built for it; each later year's is
    derived from the first's. The call starts on the service's event loop,
    which waits for the advance meanwhile. All the years share one wait for
    Modal, as long as one call's, so the request never waits longer than an
    economy report's would. Any failure of the first year's call, or Modal
    not confirming it in time, raises, so the advance fails the year and the
    window. A later year not confirmed within the shared wait is deferred
    (``YearStartDeferred``): it waits, with the years after it, for a started
    year's coordinator to start it.
    """

    def __init__(  # noqa: PLR0913  # what each year's call is started with
        self,
        *,
        worker: EconomyWorker,
        caller: EconomyRunCaller,
        first_year: int,
        first_calculation: RunCalculation,
        observability_context: dict[str, Any] | None,
        loop: asyncio.AbstractEventLoop,
        timeout_seconds: float,
        clock: Callable[[], datetime],
    ) -> None:
        """Keep what every year's call needs."""
        self._worker = worker
        self._caller = caller
        self._first_year = first_year
        self._first_calculation = first_calculation
        self._observability_context = observability_context
        self._loop = loop
        self._clock = clock
        self._timeout_seconds = timeout_seconds
        # The shared wait starts with the first start, after the advance's
        # lock and reads, so only Modal's time counts against it.
        self._wait_ends_at: float | None = None

    def __call__(self, year_to_start: YearToStart, /) -> str:
        """Start the year's coordinator and return its Modal call ID."""
        if self._wait_ends_at is None:
            self._wait_ends_at = time.monotonic() + self._timeout_seconds
        later_year = year_to_start.year != self._first_year
        remaining_seconds = self._wait_ends_at - time.monotonic()
        if later_year and remaining_seconds <= 0:
            logger.info("budget_window_year_deferred", extra=_deferral(year_to_start))
            raise YearStartDeferred
        payload, context = self._calculation(year_to_start)
        started = asyncio.run_coroutine_threadsafe(
            self._caller.start_report_run(
                self._worker,
                payload,
                context,
                observability_context=self._observability_context,
            ),
            self._loop,
        )
        try:
            return started.result(timeout=max(remaining_seconds, 0))
        except TimeoutError:
            started.cancel()
            if later_year:
                logger.info(
                    "budget_window_year_deferred", extra=_deferral(year_to_start)
                )
                raise YearStartDeferred from None
            raise
        except BaseException:
            started.cancel()
            raise

    def _calculation(
        self, year_to_start: YearToStart
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        first = self._first_calculation
        if year_to_start.year == self._first_year:
            return first.payload, first.context
        year_report, year_context = year_calculation(
            ReportExecutionInput.model_validate(first.payload),
            Stage12InvocationContext.model_validate(first.context),
            year=year_to_start.year,
            run_ids=year_to_start.run_ids,
            created_at=self._clock(),
        )
        return year_report.model_dump(mode="json"), year_context.model_dump(mode="json")


def _with_reusable(
    inputs: SimulationInputs, reusable_run: ReusableRun | None
) -> SimulationInputs:
    """Return a side's values naming the year's reusable run, if any."""
    return replace(inputs, reusable_run=reusable_run)


def _deferral(year_to_start: YearToStart) -> dict[str, Any]:
    """Return a deferred year's log fields: its year and its report run."""
    return {
        "year": year_to_start.year,
        "record_id": str(year_to_start.run_ids.report_run_id),
    }


def year_run_ids(window_year: WindowYearRuns) -> ReportRunIds:
    """Return a window year's report run and its sides' runs, as its call names them."""
    return ReportRunIds(
        report_run_id=window_year.report_run_id,
        baseline_simulation_run_id=window_year.baseline.simulation_run_id,
        reform_simulation_run_id=window_year.reform.simulation_run_id,
    )


def year_created(window_year: WindowYearRuns) -> BudgetWindowYearCreated:
    """Return a new window's year, or a re-run's, as the answer lists it."""
    return BudgetWindowYearCreated(
        year=window_year.year,
        report_run_id=window_year.report_run_id,
        baseline_simulation_id=window_year.baseline.simulation_id,
        baseline_simulation_run_id=window_year.baseline.simulation_run_id,
        baseline_reused=window_year.baseline.reused,
        reform_simulation_id=window_year.reform.simulation_id,
        reform_simulation_run_id=window_year.reform.simulation_run_id,
        reform_reused=window_year.reform.reused,
    )
