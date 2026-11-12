"""Re-run a stored economy report or simulation on the current .py version.

A re-run calculates a stored record again on the v2 version manifest's
default worker, as a new run, and keeps every earlier run. The service:

1. reads what the record calculates: its country, year, region code,
   dataset name and each simulation's policy and dynamic. A record this
   service does not re-run, or one missing a row it calculates on, is
   refused (409 ``rerun_not_supported``), as is one that does not exist
   (404);
2. finds the default worker's economy functions for the country, and reads,
   in one transaction, the country's catalog version for that worker's .py
   version, its region and dataset with the record's code and name, and each
   simulation's policy and dynamic values on that catalog version. For a
   report, unless the re-run is forced, each side's reusable run is a run of
   that side's own simulation that succeeded on the current versions, as a
   new report's would be (``find_reusable_run_of``);
3. creates, in one transaction, the new run (a report's with trigger
   ``manual``) and each report side's new simulation run of its own
   simulation, all pending with no Modal call ID, unless the record has a
   run pending or running (409 ``run_in_progress``);
4. starts and records the new run's call as a new record's: the report run's
   coordinator, or the simulation function, by run ID (C1 · 8's
   coordination), failing the new runs when Modal refuses the call.

Whatever is missing or cannot be calculated is refused in steps 1 and 2,
before anything is written. A budget-window report and a household report
or simulation are refused for now; each gets its re-run with its own
submission.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final, Protocol, get_args

from policyengine_simulation_contract.stage12_bundle import CountryId
from policyengine_simulation_contract.stage12_execution import ReportRunIds
from policyengine_simulation_persistence import (
    EconomyRequest,
    NewReportRerun,
    NewSimulationRerun,
    NewWindowRerun,
    RerunRecordDeletedError,
    RerunSide,
    RunInProgressError,
    RunReuse,
    WindowRerunYear,
)

from policyengine_simulation_entry.budget_window_api_models import (
    BudgetWindowReportCreated,
    BudgetWindowYearCreated,
)
from policyengine_simulation_entry.budget_window_submissions import (
    BudgetWindowRuns,
    ReportSubmissionService,
)
from policyengine_simulation_entry.economy_api_models import (
    EconomyReportCreated,
    EconomySimulationCreated,
)
from policyengine_simulation_entry.economy_calculation_inputs import (
    CalculationScope,
    DatasetNotInBundleError,
    bundle_dataset,
    report_run_calculation,
    simulation_options,
    simulation_run_calculation,
)
from policyengine_simulation_entry.economy_submission_errors import (
    STORED_REPORT,
    STORED_SIMULATION,
    EconomyRecordNotFoundError,
    EconomyRerunConflictError,
    EconomySubmissionRefusedError,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from uuid import UUID

    from policyengine_simulation_persistence import (
        BudgetWindowInputs,
        EconomyCatalogRows,
        EconomyReportInputs,
        RecordKind,
        ReportDefinition,
        ReusableRun,
        SideDefinition,
        SimulationChoice,
        SimulationDefinition,
        WindowYearDefinition,
    )

    from policyengine_simulation_entry.economy_api_models import (
        ReportRerunRequest,
    )
    from policyengine_simulation_entry.economy_submission_errors import (
        RecordName,
        RefusalSource,
    )
    from policyengine_simulation_entry.economy_workers import (
        EconomyWorker,
    )

# How a refusal names each kind of stored record this service does not re-run
# yet.
_NOT_RERUN: Final[dict[RecordKind, str]] = {"household": "household"}
# The kinds of stored report this service re-runs.
_RERUN_REPORT_KINDS: Final[frozenset[RecordKind]] = frozenset(
    {"economy", "budget_window"}
)
# The countries this service calculates, as the contract names them.
_COUNTRIES: Final[tuple[CountryId, ...]] = get_args(CountryId)
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

    def create_report_rerun(self, rerun: NewReportRerun) -> None:
        """Create the report's new run and its sides' new simulation runs."""
        ...

    def create_simulation_rerun(self, rerun: NewSimulationRerun) -> None:
        """Create the simulation's new run."""
        ...


class EconomyRerunService(ReportSubmissionService[EconomyRerunRuns]):
    """Create and re-run economy and budget-window reports and simulations."""

    async def rerun_report(
        self,
        report_id: UUID,
        request: ReportRerunRequest,
        *,
        request_id: str,
        observability_context: dict[str, Any] | None,
    ) -> EconomyReportCreated | BudgetWindowReportCreated:
        """Create the stored report's new run on the current versions, and start it.

        A budget window's new run is a budget-window run with a report run
        per year, whose first years are started (``_rerun_window``). Raise
        EconomyRecordNotFoundError (404) or EconomyRerunConflictError (409)
        as the module describes, and otherwise the errors ``submit_report``
        raises, a 503 for Modal's refusal naming the new run as well as the
        report.
        """
        definition = await self._read_state(
            lambda: self._store.read_report_definition(report_id)
        )
        if definition is None:
            raise EconomyRecordNotFoundError(_REPORT)
        stored = _rerunnable_report(definition)
        window_years = (
            _complete_window_years(definition)
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
        reuse = (
            None
            if request.force or self._settings.reuse_prefix is None
            else RunReuse(
                versions=worker.versions,
                inputs=definition.inputs,
                output_uri_prefix=self._settings.reuse_prefix,
            )
        )
        if window_years:
            return await self._rerun_window(
                _WindowRerunRequest(
                    stored=stored,
                    options=definition.inputs,
                    years=window_years,
                    economy_request=economy_request,
                    reuse=reuse,
                    recalculate=request.force,
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
                reuse=reuse,
            ),
            request=economy_request,
            source=STORED_REPORT,
        )
        rerun = NewReportRerun(
            report_id=report_id,
            run_id=self._new_id(),
            population=inputs.catalog.population(stored.year),
            versions=worker.versions,
            baseline=self._rerun_side(
                stored.baseline.simulation_id, inputs.baseline.reusable_run
            ),
            reform=self._rerun_side(
                stored.reform.simulation_id, inputs.reform.reusable_run
            ),
        )
        calculation = report_run_calculation(
            worker,
            _rerun_scope(
                stored.country,
                economy_request,
                worker,
                inputs.catalog,
                options=definition.inputs,
            ),
            self._origin(request_id),
            run_ids=ReportRunIds(
                report_run_id=rerun.run_id,
                baseline_simulation_run_id=rerun.baseline.simulation_run_id,
                reform_simulation_run_id=rerun.reform.simulation_run_id,
            ),
            baseline_policy=inputs.baseline.reform,
            reform_policy=inputs.reform.reform,
        )
        await self._create_rerun(
            _REPORT,
            lambda: self._store.create_report_rerun(rerun),
            source=STORED_REPORT,
        )
        status = await self._start_report_run(
            worker,
            calculation,
            report_id=report_id,
            run_id=rerun.run_id,
            new_simulation_run_ids=rerun.new_simulation_run_ids,
            observability_context=observability_context,
            rerun=True,
        )
        return EconomyReportCreated(
            report_id=report_id,
            report_run_id=rerun.run_id,
            status=status,
            baseline_simulation_id=rerun.baseline.simulation_id,
            baseline_simulation_run_id=rerun.baseline.simulation_run_id,
            baseline_reused=rerun.baseline.reused,
            reform_simulation_id=rerun.reform.simulation_id,
            reform_simulation_run_id=rerun.reform.simulation_run_id,
            reform_reused=rerun.reform.reused,
        )

    async def rerun_simulation(
        self,
        simulation_id: UUID,
        *,
        request_id: str,
        observability_context: dict[str, Any] | None,
    ) -> EconomySimulationCreated:
        """Create the stored simulation's new run on the current versions, and start it.

        The run is always calculated: a simulation run is the calculation,
        with nothing of it to reuse. Raise the errors ``rerun_report`` raises,
        naming the simulation.
        """
        definition = await self._read_state(
            lambda: self._store.read_simulation_definition(simulation_id)
        )
        if definition is None:
            raise EconomyRecordNotFoundError(_SIMULATION)
        stored = _rerunnable_simulation(definition)
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
            _rerun_scope(
                stored.country, economy_request, worker, inputs.catalog, options={}
            ),
            self._origin(request_id),
            simulation_run_id=rerun.run_id,
            policy=inputs.simulation.reform,
        )
        await self._create_rerun(
            _SIMULATION,
            lambda: self._store.create_simulation_rerun(rerun),
            source=STORED_SIMULATION,
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

    async def _rerun_window(
        self,
        window: _WindowRerunRequest,
        worker: EconomyWorker,
        *,
        request_id: str,
        observability_context: dict[str, Any] | None,
    ) -> BudgetWindowReportCreated:
        """Create the window's new run and each year's runs; start its first years.

        Each year's sides are that year's own simulations, as its report run
        in the report's newest window run names them, and each year reuses a
        run of its own simulations unless the re-run is forced. The years
        start as a new window's do (``_start_first_years``).
        """
        stored = window.stored
        inputs = await self._read_inputs(
            lambda: self._store.read_window_rerun_inputs(
                window.economy_request,
                baseline=stored.baseline.choice,
                reform=stored.reform.choice,
                years=[year.definition for year in window.years],
                reuse=window.reuse,
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
            years=tuple(self._window_rerun_year(year, inputs) for year in window.years),
        )
        first_year = rerun.years[0]
        first_calculation = report_run_calculation(
            worker,
            _rerun_scope(
                stored.country,
                window.economy_request,
                worker,
                inputs.catalog,
                options=window.options,
            ),
            self._origin(request_id),
            run_ids=ReportRunIds(
                report_run_id=first_year.report_run_id,
                baseline_simulation_run_id=first_year.baseline.simulation_run_id,
                reform_simulation_run_id=first_year.reform.simulation_run_id,
            ),
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
            years=[_rerun_year_created(window_year) for window_year in rerun.years],
        )

    def _window_rerun_year(
        self, year: _CompleteWindowYear, inputs: BudgetWindowInputs
    ) -> WindowRerunYear:
        """Return a year naming its own simulations' reusable runs, or new runs."""
        baseline_run, reform_run = inputs.reusable_runs[year.year]
        return WindowRerunYear(
            year=year.year,
            report_run_id=self._new_id(),
            baseline=self._rerun_side(year.baseline_simulation_id, baseline_run),
            reform=self._rerun_side(year.reform_simulation_id, reform_run),
        )

    def _rerun_side(
        self, simulation_id: UUID, reusable_run: ReusableRun | None
    ) -> RerunSide:
        """Return a side naming its own simulation's reusable run, or a new run."""
        if reusable_run is not None:
            return RerunSide(
                simulation_id=simulation_id,
                simulation_run_id=reusable_run.simulation_run_id,
                reused=True,
            )
        return RerunSide(
            simulation_id=simulation_id,
            simulation_run_id=self._new_id(),
            reused=False,
        )

    async def _create_rerun(
        self,
        record: RecordName,
        create: Callable[[], None],
        *,
        source: RefusalSource,
    ) -> None:
        """Create the re-run's rows; refuse it while the record has a run under way.

        A record deleted after its reads is answered as not found. Any other
        row the database refuses is named as ``source``'s; for a report,
        that includes a side's simulation deleted with it.
        """
        try:
            await self._create(record, create, rerun=True, source=source)
        except RerunRecordDeletedError:
            raise EconomyRecordNotFoundError(record) from None
        except RunInProgressError as error:
            raise EconomyRerunConflictError(
                code="run_in_progress",
                detail=(
                    f"The {record} has a run pending or running; re-run it once "
                    "that run has ended."
                ),
                run_id=error.run_id,
            ) from None


@dataclass(frozen=True)
class _StoredReportRows:
    """The country and rows a stored report calculates on, each present."""

    report_id: UUID
    country: CountryId
    year: int
    region_code: str
    dataset_name: str
    baseline: SideDefinition
    reform: SideDefinition


@dataclass(frozen=True)
class _StoredSimulationRows:
    """The country and rows a stored simulation calculates on, each present."""

    country: CountryId
    year: int
    region_code: str
    dataset_name: str


def _calculated_country(country: str | None) -> CountryId | None:
    """Return the country if this service calculates it, or None."""
    return next((known for known in _COUNTRIES if known == country), None)


def _refusal(
    record: RecordName, kind: RecordKind, missing: tuple[tuple[str, object], ...]
) -> EconomyRerunConflictError:
    """Return why a stored record is not re-run: its kind, or the first row it lacks.

    ``missing`` pairs each row's description with the value the record holds.
    """
    if kind == "unsupported":
        detail = f"The service does not re-run this type of {record}."
    elif kind in _NOT_RERUN:
        detail = f"The service does not re-run {_NOT_RERUN[kind]} {record}s yet."
    else:
        lacking = next(name for name, value in missing if value is None)
        detail = f"The {record} names no {lacking}, so it cannot be re-run."
    return EconomyRerunConflictError(code="rerun_not_supported", detail=detail)


def _rerunnable_report(definition: ReportDefinition) -> _StoredReportRows:
    """Return the stored report's rows, or refuse one this service can't re-run."""
    baseline, reform = definition.baseline, definition.reform
    region_code, dataset_name = definition.region_code, definition.dataset_name
    country, year = _calculated_country(definition.country), definition.year
    if (
        definition.kind not in _RERUN_REPORT_KINDS
        or country is None
        or baseline is None
        or reform is None
        or region_code is None
        or dataset_name is None
        or year is None
    ):
        raise _refusal(
            _REPORT,
            definition.kind,
            (
                ("country this service calculates", country),
                ("baseline simulation", baseline),
                ("reform simulation", reform),
                ("region", region_code),
                ("dataset", dataset_name),
                ("year", year),
            ),
        )
    return _StoredReportRows(
        report_id=definition.id,
        country=country,
        year=year,
        region_code=region_code,
        dataset_name=dataset_name,
        baseline=baseline,
        reform=reform,
    )


@dataclass(frozen=True)
class _CompleteWindowYear:
    """A year of a stored window's newest calculation, with both sides' simulations."""

    definition: WindowYearDefinition
    baseline_simulation_id: UUID
    reform_simulation_id: UUID

    @property
    def year(self) -> int:
        """The year."""
        return self.definition.year


@dataclass(frozen=True)
class _WindowRerunRequest:
    """What a budget window's re-run calculates, as the service has read it."""

    stored: _StoredReportRows
    options: dict[str, Any]
    years: tuple[_CompleteWindowYear, ...]
    economy_request: EconomyRequest
    reuse: RunReuse | None
    recalculate: bool


def _complete_window_years(
    definition: ReportDefinition,
) -> tuple[_CompleteWindowYear, ...]:
    """Return the stored window's years, or refuse a window that lacks one.

    Its newest calculation must have a report run naming both sides in each
    year of its window, from the report's first year.
    """
    expected = (
        ()
        if definition.year is None or definition.budget_window_size is None
        else tuple(
            range(definition.year, definition.year + definition.budget_window_size)
        )
    )
    complete = tuple(
        _CompleteWindowYear(
            definition=year,
            baseline_simulation_id=year.baseline_simulation_id,
            reform_simulation_id=year.reform_simulation_id,
        )
        for year in definition.window_years
        if year.reform_simulation_id is not None
    )
    if not expected or tuple(year.year for year in complete) != expected:
        raise EconomyRerunConflictError(
            code="rerun_not_supported",
            detail=(
                "The report names no complete budget-window calculation, a "
                "report run naming both sides in each year of its window, so "
                "it cannot be re-run."
            ),
        )
    return complete


def _rerun_year_created(window_year: WindowRerunYear) -> BudgetWindowYearCreated:
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


def _rerunnable_simulation(definition: SimulationDefinition) -> _StoredSimulationRows:
    """Return the stored simulation's country and rows, or refuse it.

    Its country is None when its catalog version's model is of no country
    this service calculates.
    """
    country, year = _calculated_country(definition.country), definition.year
    region_code, dataset_name = definition.region_code, definition.dataset_name
    if (
        definition.kind != "economy"
        or country is None
        or region_code is None
        or dataset_name is None
        or year is None
    ):
        raise _refusal(
            _SIMULATION,
            definition.kind,
            (
                ("country this service calculates", country),
                ("region", region_code),
                ("dataset", dataset_name),
                ("year", year),
            ),
        )
    return _StoredSimulationRows(
        country=country,
        year=year,
        region_code=region_code,
        dataset_name=dataset_name,
    )


def _rerun_scope(
    country: CountryId,
    request: EconomyRequest,
    worker: EconomyWorker,
    catalog: EconomyCatalogRows,
    *,
    options: dict[str, Any],
) -> CalculationScope:
    """Return what a re-run calculates on, with the bundle's dataset.

    ``options`` are the stored report's (``reports.inputs``). Raise
    EconomySubmissionRefusedError (``dataset_not_in_bundle``) when the
    default worker's bundle lacks the stored dataset.
    """
    try:
        dataset = bundle_dataset(worker, catalog.dataset.name)
    except DatasetNotInBundleError:
        raise EconomySubmissionRefusedError(
            code="dataset_not_in_bundle",
            detail=(
                f"The default v2 worker, .py {worker.version}, has no dataset "
                f"{catalog.dataset.name}, which the stored record names."
            ),
        ) from None
    return CalculationScope(
        country=country,
        region_code=catalog.region.code,
        dataset=dataset,
        year=request.year,
        options=simulation_options(
            include_cliffs=bool(options.get("include_cliffs", False)),
            spm=options.get("spm"),
        ),
    )
