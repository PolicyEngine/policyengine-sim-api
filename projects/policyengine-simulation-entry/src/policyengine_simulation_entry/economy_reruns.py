"""Re-run a stored economy report or simulation on the current .py version.

A re-run calculates a stored record again on the v2 version manifest's
default worker, as a new run, and keeps every earlier run. The service:

1. reads what the record calculates: its country, year, region code,
   dataset name, stored options and each simulation's policy and dynamic.
   A record this service does not re-run, or one missing a row it
   calculates on, is refused (409 ``rerun_not_supported``), as is one that
   does not exist (404), and a report stored before C1 · 27 asking for
   cliff impacts (422 ``cliffs_not_available``) until they are calculated
   per run (C1 · 24's interim answer A);
2. finds the default worker's economy functions for the country, and reads,
   in one transaction, the country's catalog version for that worker's .py
   version, its region and dataset with the record's code and name, and each
   simulation's policy and dynamic values on that catalog version;
3. for a simulation or a keyed economy report, joins, in one transaction,
   the record's run on the current versions that hasn't failed and wasn't
   forced, pending, running or succeeded (C1 · 24's answers 2 and 3), or
   else creates the new run (a report's with trigger ``manual``); a keyed
   report's sides join their simulations' runs on those versions the same
   way, or create them. A report whose simulations have no key, created
   before C1 · 27, may have runs whose plans depended on the report that
   calculated them, so its re-run, like a forced re-run, always creates its
   runs, marked forced: extra runs that nothing joins (interim answer B). A
   budget window has no keys yet, so its re-run always creates its runs,
   marked forced. Every new run is pending with no Modal call ID, and a
   forced re-run is never refused while a run is under way;
4. starts and records the call of a run it created as a new record's: the
   report run's coordinator, or the simulation function, by run ID (C1 · 8's
   coordination), failing the new runs when Modal refuses the call. A
   joined run gets no call: whoever created it started it.

A budget-window report's re-run is a new budget-window run with a report
run per year, each year on the simulations that year's report run of the
report's newest window run names; its first years start as a new window's
do. A household report's re-run is the household report service's
(``household_report_reruns``): a new run of each side's own household
simulation, marked forced as a household report has no keys yet, started
by one household function call. A household simulation is re-run only
through its report. Whatever is missing or cannot be calculated is refused
in steps 1 and 2, before anything is written.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Final, Protocol
from uuid import uuid4

from policyengine_simulation_contract.stage12_execution import ReportRunIds
from policyengine_simulation_persistence import (
    EconomyRequest,
    NewReportRerun,
    NewSimulationRerun,
    NewWindowRerun,
    RerunSide,
    WindowRerunYear,
)

from policyengine_simulation_entry.budget_window_api_models import (
    BudgetWindowReportCreated,
)
from policyengine_simulation_entry.budget_window_submissions import (
    BudgetWindowRuns,
    ReportSubmissionService,
    year_created,
    year_run_ids,
)
from policyengine_simulation_entry.economy_api_models import (
    EconomyReportCreated,
    EconomySimulationCreated,
)
from policyengine_simulation_entry.economy_calculation_inputs import (
    report_run_calculation,
    simulation_run_calculation,
)
from policyengine_simulation_entry.economy_requests import refuse_stored_cliffs
from policyengine_simulation_entry.economy_submission_errors import (
    STORED_REPORT,
    STORED_SIMULATION,
    EconomyRecordNotFoundError,
    EconomySubmissionUnavailableError,
    rerun_refusals,
)
from policyengine_simulation_entry.economy_submissions import (
    CALL_START_TIMEOUT_SECONDS,
)
from policyengine_simulation_entry.rerunnable_records import (
    StoredReportRows,
    complete_window_years,
    rerun_scope,
    rerunnable_household_report,
    rerunnable_report,
    rerunnable_simulation,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from uuid import UUID

    from policyengine_simulation_persistence import (
        BudgetWindowInputs,
        EconomyReportInputs,
        FoundReportRun,
        FoundSimulationRun,
        ReportDefinition,
        RunReuse,
        SideDefinition,
        SimulationChoice,
        SimulationDefinition,
        WindowYearDefinition,
    )

    from policyengine_simulation_entry.budget_window_submissions import (
        BudgetWindowAdvancer,
    )
    from policyengine_simulation_entry.economy_api_models import (
        ReportRerunRequest,
    )
    from policyengine_simulation_entry.economy_submission_errors import (
        RecordName,
        RefusalSource,
    )
    from policyengine_simulation_entry.economy_submissions import ServiceSettings
    from policyengine_simulation_entry.economy_workers import (
        EconomyRunCaller,
        EconomyWorker,
        EconomyWorkerSource,
    )
    from policyengine_simulation_entry.household_report_api_models import (
        HouseholdReportCreated,
    )
    from policyengine_simulation_entry.household_report_reruns import (
        HouseholdReportReruns,
    )

_REPORT: Final[RecordName] = "report"
_SIMULATION: Final[RecordName] = "simulation"


class EconomyRerunRuns(BudgetWindowRuns, Protocol):
    """The economy store's reads and writes, with budget windows' and re-runs'.

    ``EconomyRunStore`` is one.
    """

    def read_report_definition(self, report_id: UUID) -> ReportDefinition | None:
        """Return what the stored report calculates, or None."""
        ...

    def read_simulation_definition(
        self, simulation_id: UUID
    ) -> SimulationDefinition | None:
        """Return what the stored simulation calculates, or None."""
        ...

    def read_report_rerun_inputs(
        self,
        request: EconomyRequest,
        *,
        baseline: SideDefinition,
        reform: SideDefinition,
        reuse: RunReuse | None,
    ) -> EconomyReportInputs:
        """Read a re-run's catalog rows, both sides' values and reusable runs."""
        ...

    def read_window_rerun_inputs(
        self,
        request: EconomyRequest,
        *,
        baseline: SimulationChoice,
        reform: SimulationChoice,
        years: Sequence[WindowYearDefinition],
        reuse: RunReuse | None,
    ) -> BudgetWindowInputs:
        """Read a window re-run's catalog rows, both sides' values and years' runs."""
        ...

    def create_window_rerun(self, rerun: NewWindowRerun) -> None:
        """Create the window's new run, each year's report run and new runs."""
        ...

    def create_report_rerun(self, rerun: NewReportRerun) -> FoundReportRun:
        """Join the report's run on the re-run's versions, or create it."""
        ...

    def create_simulation_rerun(self, rerun: NewSimulationRerun) -> FoundSimulationRun:
        """Join the simulation's run on the re-run's versions, or create it."""
        ...


class EconomyRerunService(ReportSubmissionService[EconomyRerunRuns]):
    """Create and re-run economy and budget-window reports and simulations.

    A household report's re-run goes to ``household_reports``; without it,
    one the service could re-run answers 503, after the 409s.
    """

    def __init__(  # noqa: PLR0913  # the household service, then the report service's seams
        self,
        *,
        household_reports: HouseholdReportReruns | None = None,
        windows: BudgetWindowAdvancer,
        store: EconomyRerunRuns,
        workers: EconomyWorkerSource,
        caller: EconomyRunCaller,
        settings: ServiceSettings,
        call_start_timeout_seconds: float = CALL_START_TIMEOUT_SECONDS,
        new_id: Callable[[], UUID] = uuid4,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        """Use these seams; tests replace each of them."""
        super().__init__(
            windows=windows,
            store=store,
            workers=workers,
            caller=caller,
            settings=settings,
            call_start_timeout_seconds=call_start_timeout_seconds,
            new_id=new_id,
            clock=clock,
        )
        self._household_reports = household_reports

    async def rerun_report(
        self,
        report_id: UUID,
        request: ReportRerunRequest,
        *,
        request_id: str,
        observability_context: dict[str, Any] | None,
    ) -> EconomyReportCreated | BudgetWindowReportCreated | HouseholdReportCreated:
        """Join or create the stored report's run on the current versions.

        A budget window's new run is a budget-window run with a report run
        per year, whose first years are started (``_rerun_window``); a
        household report's is the household report service's, which
        ``force`` changes nothing for. Raise EconomyRecordNotFoundError (404)
        or EconomyRerunConflictError (409) as the module describes, and
        otherwise the errors ``submit_report`` raises, a 503 for Modal's
        refusal naming the new run as well as the report.
        """
        definition = await self._read_state(
            lambda: self._store.read_report_definition(report_id)
        )
        if definition is None:
            raise EconomyRecordNotFoundError(_REPORT)
        if definition.kind == "household":
            return await self._rerun_household_report(
                definition,
                force=request.force,
                observability_context=observability_context,
            )
        stored = rerunnable_report(definition)
        refuse_stored_cliffs(definition.inputs)
        window_years = (
            complete_window_years(definition)
            if definition.kind == "budget_window"
            else ()
        )
        worker = await self._default_worker(stored.country)
        economy_request = EconomyRequest(
            country=stored.country,
            policyengine_version=worker.policyengine_version,
            region_code=stored.region_code,
            dataset_name=stored.dataset_name,
            year=stored.year,
        )
        # A report with no keys gets extra runs, marked forced, which
        # nothing joins (interim answer B); a keyed report's sides join
        # their simulations' runs on these versions instead.
        forced = request.force or not definition.keyed
        if window_years:
            return await self._rerun_window(
                _WindowRerunRequest(
                    stored=stored,
                    options=definition.inputs,
                    years=window_years,
                    economy_request=economy_request,
                    recalculate=forced,
                ),
                worker,
                request_id=request_id,
                observability_context=observability_context,
            )
        inputs = await self._read_inputs(
            lambda: self._store.read_report_rerun_inputs(
                economy_request,
                baseline=stored.baseline,
                reform=stored.reform,
                # A re-run reuses no run: its sides join or are forced.
                reuse=None,
            ),
            request=economy_request,
            source=STORED_REPORT,
        )
        rerun = NewReportRerun(
            report_id=report_id,
            run_id=self._new_id(),
            population=inputs.catalog.population(stored.year),
            versions=worker.versions,
            baseline=self._new_side(stored.baseline.simulation_id),
            reform=self._new_side(stored.reform.simulation_id),
            forced=forced,
        )
        scope = rerun_scope(
            stored.country,
            economy_request,
            worker,
            inputs.catalog,
            options=definition.inputs,
        )
        found = await self._create_rerun(
            _REPORT,
            lambda: self._store.create_report_rerun(rerun),
            source=STORED_REPORT,
        )
        if not found.inserted:
            return _joined_report_run(stored, found)
        answer = _created_report_run(stored, rerun, found)
        calculation = report_run_calculation(
            worker,
            scope,
            self._origin(request_id),
            run_ids=ReportRunIds(
                report_run_id=answer.report_run_id,
                baseline_simulation_run_id=answer.baseline_simulation_run_id,
                reform_simulation_run_id=answer.reform_simulation_run_id,
            ),
            baseline_policy=inputs.baseline.reform,
            reform_policy=inputs.reform.reform,
        )
        status = await self._start_report_run(
            worker,
            calculation,
            report_id=report_id,
            run_id=rerun.run_id,
            # An unforced re-run's simulation runs may be another report run's.
            new_simulation_run_ids=rerun.new_simulation_run_ids if forced else (),
            observability_context=observability_context,
            rerun=True,
        )
        return answer.model_copy(update={"status": status})

    async def rerun_simulation(
        self,
        simulation_id: UUID,
        *,
        request_id: str,
        observability_context: dict[str, Any] | None,
    ) -> EconomySimulationCreated:
        """Join or create the stored simulation's run on the current versions.

        A run the store created is started; one it joined, pending, running
        or succeeded, is answered as it is, with no call. Raise the errors
        ``rerun_report`` raises, naming the simulation.
        """
        definition = await self._read_state(
            lambda: self._store.read_simulation_definition(simulation_id)
        )
        if definition is None:
            raise EconomyRecordNotFoundError(_SIMULATION)
        stored = rerunnable_simulation(definition)
        worker = await self._default_worker(stored.country)
        economy_request = EconomyRequest(
            country=stored.country,
            policyengine_version=worker.policyengine_version,
            region_code=stored.region_code,
            dataset_name=stored.dataset_name,
            year=stored.year,
        )
        inputs = await self._read_inputs(
            lambda: self._store.read_simulation_inputs(
                economy_request, definition.choice
            ),
            request=economy_request,
            source=STORED_SIMULATION,
        )
        rerun = NewSimulationRerun(
            simulation_id=simulation_id,
            run_id=self._new_id(),
            population=inputs.catalog.population(stored.year),
            versions=worker.versions,
        )
        calculation = simulation_run_calculation(
            worker,
            rerun_scope(
                stored.country, economy_request, worker, inputs.catalog, options={}
            ),
            self._origin(request_id),
            simulation_run_id=rerun.run_id,
            policy=inputs.simulation.reform,
        )
        found = await self._create_rerun(
            _SIMULATION,
            lambda: self._store.create_simulation_rerun(rerun),
            source=STORED_SIMULATION,
        )
        if not found.inserted:
            return EconomySimulationCreated(
                simulation_id=simulation_id,
                simulation_run_id=found.id,
                status=found.status,
            )
        status = await self._start_simulation_run(
            worker,
            calculation,
            simulation_id=simulation_id,
            run_id=rerun.run_id,
            observability_context=observability_context,
            rerun=True,
        )
        return EconomySimulationCreated(
            simulation_id=simulation_id,
            simulation_run_id=rerun.run_id,
            status=status,
        )

    async def _rerun_household_report(
        self,
        definition: ReportDefinition,
        *,
        force: bool,
        observability_context: dict[str, Any] | None,
    ) -> HouseholdReportCreated:
        """Hand a stored household report the service can re-run to its service."""
        stored = rerunnable_household_report(definition)
        if self._household_reports is None:
            raise EconomySubmissionUnavailableError(
                code="not_configured", detail="Household reports are not configured."
            )
        return await self._household_reports.rerun_report(
            stored, force=force, observability_context=observability_context
        )

    async def _rerun_window(
        self,
        window: _WindowRerunRequest,
        worker: EconomyWorker,
        *,
        request_id: str,
        observability_context: dict[str, Any] | None,
    ) -> BudgetWindowReportCreated:
        """Create the window's new run and each year's runs; start its first years.

        Each year's sides are new runs of that year's own simulations, as
        its report run in the report's newest window run names them, marked
        forced: a window has no keys yet (interim answer B). The years start
        as a new window's do (``_start_first_years``).
        """
        stored = window.stored
        inputs = await self._read_inputs(
            lambda: self._store.read_window_rerun_inputs(
                window.economy_request,
                baseline=stored.baseline.choice,
                reform=stored.reform.choice,
                years=window.years,
                reuse=None,
            ),
            request=window.economy_request,
            source=STORED_REPORT,
        )
        window_run_id = self._new_id()
        rerun = NewWindowRerun(
            report_id=stored.report_id,
            window_run_id=window_run_id,
            population=inputs.catalog.population(stored.year),
            versions=worker.versions,
            recalculate=window.recalculate,
            years=tuple(self._window_rerun_year(year) for year in window.years),
        )
        first_year = rerun.years[0]
        first_calculation = report_run_calculation(
            worker,
            rerun_scope(
                stored.country,
                window.economy_request,
                worker,
                inputs.catalog,
                options=window.options,
            ),
            self._origin(request_id),
            run_ids=year_run_ids(first_year),
            baseline_policy=inputs.baseline.reform,
            reform_policy=inputs.reform.reform,
        )
        await self._create_rerun(
            _REPORT,
            lambda: self._store.create_window_rerun(rerun),
            source=STORED_REPORT,
        )
        status = await self._start_first_years(
            worker,
            first_calculation,
            report_id=stored.report_id,
            window_run_id=window_run_id,
            first_year=first_year.year,
            observability_context=observability_context,
            rerun=True,
        )
        return BudgetWindowReportCreated(
            report_id=stored.report_id,
            report_run_id=first_year.report_run_id,
            budget_window_run_id=window_run_id,
            status=status,
            years=[year_created(window_year) for window_year in rerun.years],
        )

    def _window_rerun_year(self, year: WindowYearDefinition) -> WindowRerunYear:
        """Return a year naming new runs of its own simulations."""
        return WindowRerunYear(
            year=year.year,
            report_run_id=self._new_id(),
            baseline=self._new_side(year.baseline_simulation_id),
            reform=self._new_side(year.reform_simulation_id),
        )

    def _new_side(self, simulation_id: UUID) -> RerunSide:
        """Return a side proposing a new run of its own simulation.

        The run is created, unless a keyed report's side joins its
        simulation's run on the re-run's versions.
        """
        return RerunSide(
            simulation_id=simulation_id,
            simulation_run_id=self._new_id(),
            reused=False,
        )

    async def _create_rerun[Created](
        self,
        record: RecordName,
        create: Callable[[], Created],
        *,
        source: RefusalSource,
    ) -> Created:
        """Create or join the re-run's rows, answering the store's refusals.

        Return what ``create`` returns: the run it joined or created. A run
        under way the store refuses the re-run for answers 409
        ``run_in_progress``. A record deleted after its reads is
        answered as not found. Any other row the database refuses is named
        as ``source``'s; for a report, that includes a side's simulation
        deleted with it.
        """
        with rerun_refusals(record):
            return await self._create(record, create, rerun=True, source=source)


def _joined_report_run(
    stored: StoredReportRows, joined: FoundReportRun
) -> EconomyReportCreated:
    """Answer the report's run a re-run joined, as it is; this call created nothing.

    Only a keyed report's unforced re-run joins a run; it names runs of the
    report's own simulations, which this call didn't create, so both sides
    are reused.
    """
    if joined.reform_simulation_run_id is None:
        msg = f"report run {joined.id} of an economy report names no reform run"
        raise ValueError(msg)
    return EconomyReportCreated(
        report_id=stored.report_id,
        report_run_id=joined.id,
        status=joined.status,
        baseline_simulation_id=stored.baseline.simulation_id,
        baseline_simulation_run_id=joined.baseline_simulation_run_id,
        baseline_reused=True,
        reform_simulation_id=stored.reform.simulation_id,
        reform_simulation_run_id=joined.reform_simulation_run_id,
        reform_reused=True,
    )


def _created_report_run(
    stored: StoredReportRows, rerun: NewReportRerun, created: FoundReportRun
) -> EconomyReportCreated:
    """Answer the report run a re-run created, with the side runs it names.

    A side is reused when its run isn't one this re-run inserted: for a
    keyed report, its simulation's run the store joined. The runs this
    re-run may have inserted have the IDs its sides proposed; one
    simulation on both sides gets one run, with the baseline's. The status
    is the new run's, until its call is started.
    """
    if created.reform_simulation_run_id is None:
        msg = f"report run {created.id} of an economy report names no reform run"
        raise ValueError(msg)
    proposed = {rerun.baseline.simulation_run_id, rerun.reform.simulation_run_id}
    return EconomyReportCreated(
        report_id=stored.report_id,
        report_run_id=created.id,
        status=created.status,
        baseline_simulation_id=stored.baseline.simulation_id,
        baseline_simulation_run_id=created.baseline_simulation_run_id,
        baseline_reused=created.baseline_simulation_run_id not in proposed,
        reform_simulation_id=stored.reform.simulation_id,
        reform_simulation_run_id=created.reform_simulation_run_id,
        reform_reused=created.reform_simulation_run_id not in proposed,
    )


@dataclass(frozen=True)
class _WindowRerunRequest:
    """What a budget window's re-run calculates, as the service has read it."""

    stored: StoredReportRows
    options: dict[str, Any]
    years: tuple[WindowYearDefinition, ...]
    economy_request: EconomyRequest
    recalculate: bool
