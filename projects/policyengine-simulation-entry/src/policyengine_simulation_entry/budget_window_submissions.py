"""Finding or creating budget-window reports and starting their first years.

A budget-window report is an economy report with one report run per year of
its window. Identical window requests, together or apart, end on the same
report, simulations, window run and year runs (C1 · 24's answer 8): in one
transaction the store finds each year's simulations and the report by
their keys, and the report's window run on the current versions that
hasn't failed and isn't a recalculation, or else creates the window run and
each year's report run on its sides' runs, found or inserted
(``find_or_create_budget_window``), all pending with no Modal call. Only
for a window run it created does the service then advance the window as
its own role (``BudgetWindowStore.advance_window``): under the window
run's row lock, up to the earliest five years' coordinators are started
and their calls recorded. Each year's coordinator starts the next ones when
its year ends. A joined window run is answered as it is.

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
    EconomyReportDefinition,
    KeyedBudgetWindow,
    KeyedWindowYear,
    ReportRunStatus,
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
from policyengine_simulation_entry.economy_requests import stored_options
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
        FoundBudgetWindow,
        RunReuse,
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

    def find_or_create_budget_window(
        self, window: KeyedBudgetWindow
    ) -> FoundBudgetWindow:
        """Find the window by key, with its window run, or create them."""
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
        """Find or create the window and its window run, and start a run it created.

        A window run the request joined is answered as it is, with no new
        call. Raise the errors ``submit_report`` raises; the 503 after a
        write names the report, whose window was failed.
        """
        worker = await self._default_worker(request.country)
        options = request.options()
        spm = options.get("spm")
        economy_request = economy_request_for(request, worker)
        inputs = await self._read_inputs(
            lambda: self._store.read_budget_window_inputs(
                economy_request,
                baseline=simulation_choice(request.baseline),
                reform=simulation_choice(request.reform),
                reuse=None,
                years=request.years,
            ),
            request=economy_request,
        )
        catalog = inputs.catalog
        window = KeyedBudgetWindow(
            definition=EconomyReportDefinition(
                country=request.country,
                baseline=inputs.baseline.choice,
                reform=inputs.reform.choice,
                dataset_name=catalog.dataset.name,
                region_code=catalog.region.code,
                year=request.year,
                budget_window_size=request.budget_window_size,
                spm=spm,
            ),
            report_id=self._new_id(),
            window_run_id=self._new_id(),
            tax_benefit_model_id=catalog.model_id,
            population=catalog.population(request.year),
            versions=worker.versions,
            inputs=stored_options(options),
            years=tuple(
                KeyedWindowYear(
                    year=year,
                    report_run_id=self._new_id(),
                    baseline=self._proposed_side(),
                    reform=self._proposed_side(),
                )
                for year in request.years
            ),
        )
        scope = calculation_scope(request, worker, catalog, spm=spm)
        proposed_first_year = window.years[0]

        def first_year_calculation(run_ids: ReportRunIds) -> RunCalculation:
            return report_run_calculation(
                worker,
                scope,
                self._origin(request_id),
                run_ids=run_ids,
                baseline_policy=inputs.baseline.reform,
                reform_policy=inputs.reform.reform,
            )

        # Refused before anything is written, on the run IDs it proposes.
        first_year_calculation(
            ReportRunIds(
                report_run_id=proposed_first_year.report_run_id,
                baseline_simulation_run_id=proposed_first_year.baseline.simulation_run_id,
                reform_simulation_run_id=proposed_first_year.reform.simulation_run_id,
            )
        )
        found = await self._create(
            "report", lambda: self._store.find_or_create_budget_window(window)
        )
        first_year = found.years[0]
        status = found.window_run.status
        if found.window_run.inserted:
            status = await self._start_first_years(
                worker,
                first_year_calculation(year_run_ids(first_year)),
                report_id=found.report_id,
                window_run_id=found.window_run.id,
                first_year=first_year.year,
                observability_context=observability_context,
            )
        return BudgetWindowReportCreated(
            report_id=found.report_id,
            report_run_id=first_year.report_run_id,
            budget_window_run_id=found.window_run.id,
            status=status,
            years=[year_created(window_year) for window_year in found.years],
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
        started claims itself, while the stuck-run sweep fails a window no
        year advances. ``rerun`` says the report existed before the window
        run, which the 503 then names.
        """
        start_year = _YearStarts(
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


class _YearStarts:
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
    """Return a year of the window run a request ended on, as the answer lists it.

    A side is reused when its run isn't one this request inserted.
    """
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
