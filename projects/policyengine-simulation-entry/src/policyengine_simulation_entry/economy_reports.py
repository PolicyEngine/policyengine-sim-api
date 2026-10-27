"""Submit and re-run v2 economy reports: simulation runs first, then comparisons.

A report compares two simulations of one region (``simulation.reports``).
Running it on a catalog version and year:

1. finds or creates a run of each simulation for that version and year,
   reusing one that is unfinished or succeeded within 28 days (the
   current-law baseline, most often), and records a report run joining them;
2. starts one Modal call per run that has not started, with the region and
   dataset rows of the run's catalog version and its policy's parameter
   values by name (``EconomySimulationCall``); a policy changing a parameter
   the version lacks fails its run as ``invalid_input``;
3. starts the report run's comparison step (``EconomyComparisonCall``) once
   both runs have succeeded: at once when both were reused, or when a call's
   report says that its run succeeded (``start_comparisons_after``).

A report whose policies change a labour supply elasticity needs both
simulations' labour supply response columns, which a run calculates only
when asked, so its runs are always calculated again (forced) and asked for
them. Report options that would change a simulation's output (an SPM
selection, cliff impacts) are not accepted yet, because a reused run could
not tell which ones it was calculated with.

Every status change is one of ``RunTransitions``' conditional writes, so a
call starts once whoever else tries.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import timedelta
from typing import TYPE_CHECKING, Any, Protocol, cast

from policyengine_simulation_contract.economy_runs import (
    ECONOMY_COMPARISON_FUNCTION,
    ECONOMY_SIMULATION_FUNCTIONS,
    CatalogPopulation,
    CatalogRowFilter,
    EconomyComparisonCall,
    EconomyComparisonSide,
    EconomySimulationCall,
    policy_affects_labor_supply,
)
from policyengine_simulation_contract.stage12_manifest import v2_application_name
from policyengine_simulation_persistence import (
    CatalogError,
    NewReport,
    NewReportRun,
    NewSimulation,
    NewSimulationRun,
    ReportKind,
    RunError,
    RunErrorCode,
    RunStatus,
    RunTrigger,
    SimulationKind,
)
from sqlalchemy.exc import IntegrityError

from policyengine_simulation_entry.economy_recorders import SIMULATION_OUTPUT_ROLE
from policyengine_simulation_entry.modal_calls import ModalRequest, ModalTarget

if TYPE_CHECKING:
    from collections.abc import Collection, Mapping
    from datetime import datetime
    from uuid import UUID

    from policyengine_simulation_contract.stage12_bundle import CountryId
    from policyengine_simulation_persistence import (
        CatalogVersion,
        OutputFileRecord,
        RegionFilter,
        RegionPopulation,
        ReportRecord,
        ReportRunRecord,
        SimulationRecord,
        SimulationRunRecord,
        StartedSimulationRun,
    )

    from policyengine_simulation_entry.run_dispatch import RunDispatchService

logger = logging.getLogger(__name__)

RERUN_TRIGGERS = frozenset({RunTrigger.MANUAL, RunTrigger.SYSTEM})
# A run still pending this long after it was created was left by a submitter
# that stopped before starting its call; the safety check starts it.
PENDING_RUN_START_AFTER = timedelta(minutes=10)
WAITING_WORK_BATCH_SIZE = 50


class EconomyRequestError(ValueError):
    """The request cannot run: unknown catalog version, region or dataset, or input."""


class ReportNotFoundError(LookupError):
    """No report has this ID."""


@dataclass(frozen=True)
class ResumedWork:
    """What one safety-check pass started of the work left waiting."""

    comparisons_started: int = 0
    runs_started: int = 0


@dataclass(frozen=True)
class SimulationChoice:
    """A simulation's policy and dynamic; neither means current law."""

    policy_id: UUID | None = None
    dynamic_id: UUID | None = None


@dataclass(frozen=True)
class EconomyReportSubmission:
    """A new economy report: two simulations of one region, a year, a version.

    ``policyengine_version`` selects the country's catalog version, which
    fixes the model, the data release and the region rows. ``dataset_name``
    names a dataset of that version; None means the region's default.
    """

    country: str
    policyengine_version: str
    year: int
    region_code: str
    baseline: SimulationChoice
    reform: SimulationChoice
    dataset_name: str | None = None
    options: Mapping[str, Any] = field(default_factory=dict)


class SimulationDefinitions(Protocol):
    """``SimulationStore``'s finds and reads."""

    def find_or_create(self, new_simulation: NewSimulation) -> SimulationRecord:
        """Return the simulation with this definition, creating it if needed."""
        ...

    def get(self, simulation_id: UUID) -> SimulationRecord | None:
        """Return the simulation, or None."""
        ...


class ReportDefinitions(Protocol):
    """``ReportStore``'s inserts and reads."""

    def create(self, new_report: NewReport) -> ReportRecord:
        """Insert a report."""
        ...

    def get(self, report_id: UUID) -> ReportRecord | None:
        """Return the report, or None."""
        ...


class EconomyRuns(Protocol):
    """``RunStore``'s finds, inserts and reads."""

    def start_simulation_run(
        self, new_run: NewSimulationRun, *, force: bool = False
    ) -> StartedSimulationRun:
        """Return the reusable run, or the next attempt."""
        ...

    def create_report_run(self, new_report_run: NewReportRun) -> ReportRunRecord:
        """Insert a report run."""
        ...

    def get_simulation_run(self, run_id: UUID) -> SimulationRunRecord | None:
        """Return the simulation run, or None."""
        ...

    def comparisons_ready_to_start(
        self, *, simulation_run_ids: Collection[UUID] | None = None, limit: int = 100
    ) -> list[ReportRunRecord]:
        """Return pending comparisons whose runs succeeded."""
        ...

    def simulation_output_file(
        self, simulation_run_id: UUID, role: str
    ) -> OutputFileRecord | None:
        """Return a run's stored file of ``role`` while it is offered."""
        ...

    def pending_simulation_runs(
        self, *, created_before: datetime, limit: int = 100
    ) -> list[SimulationRunRecord]:
        """Return runs still pending that were created before a time."""
        ...

    def report_runs_using(self, simulation_run_id: UUID) -> list[ReportRunRecord]:
        """Return the report runs comparing a simulation run."""
        ...


class EconomyCatalog(Protocol):
    """``CatalogReader``'s reads."""

    def catalog_version(
        self, country: str, policyengine_version: str
    ) -> CatalogVersion | None:
        """Return the country's catalog version for a policyengine.py version."""
        ...

    def catalog_version_by_id(self, version_id: UUID) -> CatalogVersion | None:
        """Return a catalog version by ID."""
        ...

    def population(
        self, version: CatalogVersion, region_code: str, dataset_name: str | None
    ) -> RegionPopulation:
        """Return the region and dataset rows; raise CatalogError."""
        ...

    def parameter_changes(
        self,
        catalog_version_id: UUID,
        *,
        policy_id: UUID | None,
        dynamic_id: UUID | None,
    ) -> dict[str, dict[str, Any]]:
        """Return the values a policy and dynamic set, checked against a version."""
        ...


class FailureTransitions(Protocol):
    """The ``RunTransitions`` writes that fail work that cannot start."""

    def claim_simulation_dispatch(self, run_ids: Collection[UUID]) -> bool:
        """Move pending runs to dispatching."""
        ...

    def record_simulation_dispatch_failure(
        self, run_ids: Collection[UUID], error: RunError
    ) -> bool:
        """Fail dispatching runs."""
        ...

    def claim_comparison_dispatch(self, report_run_id: UUID) -> bool:
        """Move a pending comparison step to dispatching."""
        ...

    def record_comparison_dispatch_failure(
        self, report_run_id: UUID, error: RunError
    ) -> bool:
        """Fail a dispatching comparison step."""
        ...


def _row_filter(region_filter: RegionFilter | None) -> CatalogRowFilter | None:
    if region_filter is None:
        return None
    return CatalogRowFilter(field=region_filter.field, value=region_filter.value)


def catalog_population(population: RegionPopulation) -> CatalogPopulation:
    """Return the population an economy simulation call carries."""
    return CatalogPopulation(
        region_code=population.region_code,
        dataset_name=population.dataset_name,
        row_filter=_row_filter(population.row_filter),
        state_code=population.state_code,
        state_filters=(
            None
            if population.state_filters is None
            else {
                code: CatalogRowFilter(field=item.field, value=item.value)
                for code, item in population.state_filters.items()
            }
        ),
    )


@dataclass(frozen=True)
class _PlannedRun:
    """One simulation of a report run, resolved in the run's catalog version.

    ``changes`` is None when the policy cannot run on the version, and
    ``refusal`` then says why.
    """

    simulation: SimulationRecord
    population: RegionPopulation
    changes: dict[str, dict[str, Any]] | None
    refusal: str | None = None


@dataclass
class EconomyReports:
    """The v2 economy path: report submission, runs, calls and comparisons."""

    simulations: SimulationDefinitions
    reports: ReportDefinitions
    runs: EconomyRuns
    catalog: EconomyCatalog
    transitions: FailureTransitions
    dispatch: RunDispatchService
    artifact_environment: str

    # --- Reports -----------------------------------------------------------

    async def submit(
        self, submission: EconomyReportSubmission
    ) -> tuple[ReportRecord, ReportRunRecord]:
        """Create a report's simulations, the report and its first run, and start it.

        Raise EconomyRequestError, before anything is written, when the
        country has no catalog for the policyengine.py version, the version
        has no such region or dataset, options are given, or a policy or
        dynamic does not exist for the country.
        """
        if submission.options:
            msg = "economy reports take no options yet"
            raise EconomyRequestError(msg)
        version = await self._version(
            submission.country, submission.policyengine_version
        )
        await self._population(version, submission.region_code, submission.dataset_name)
        try:
            baseline, reform = [
                await asyncio.to_thread(
                    self.simulations.find_or_create,
                    NewSimulation(
                        kind=SimulationKind.ECONOMY,
                        country=submission.country,
                        policy_id=choice.policy_id,
                        dynamic_id=choice.dynamic_id,
                        region_code=submission.region_code,
                        dataset_name=submission.dataset_name,
                    ),
                )
                for choice in (submission.baseline, submission.reform)
            ]
        except IntegrityError as error:
            msg = (
                f"a policy or dynamic of the submission does not exist for "
                f"{submission.country}"
            )
            raise EconomyRequestError(msg) from error
        report = await asyncio.to_thread(
            self.reports.create,
            NewReport(
                kind=ReportKind.ECONOMY,
                country=submission.country,
                baseline_simulation_id=baseline.id,
                reform_simulation_id=reform.id,
                year=submission.year,
            ),
        )
        report_run = await self._run(
            report, version, (baseline, reform), RunTrigger.INITIAL, force=False
        )
        return report, report_run

    async def run_report(
        self,
        report_id: UUID,
        *,
        policyengine_version: str,
        trigger: RunTrigger,
        force: bool,
    ) -> ReportRunRecord:
        """Run an existing report again on a catalog version.

        ``trigger`` is ``manual`` for a user's re-run and ``system`` for a
        run sim's caller starts itself, such as the first opening of a
        migrated report. Existing runs are reused unless ``force`` is set.
        Raise ReportNotFoundError for an unknown report, and
        EconomyRequestError for one that cannot run.
        """
        if trigger not in RERUN_TRIGGERS:
            msg = "a report is run again with the manual or system trigger"
            raise EconomyRequestError(msg)
        report = await asyncio.to_thread(self.reports.get, report_id)
        if report is None:
            msg = f"no report {report_id}"
            raise ReportNotFoundError(msg)
        if report.kind is not ReportKind.ECONOMY or report.reform_simulation_id is None:
            msg = "only an economy report runs on the economy path"
            raise EconomyRequestError(msg)
        if report.options:
            msg = "economy reports take no options yet"
            raise EconomyRequestError(msg)
        version = await self._version(report.country, policyengine_version)
        simulations = []
        for simulation_id in (
            report.baseline_simulation_id,
            report.reform_simulation_id,
        ):
            simulation = await asyncio.to_thread(self.simulations.get, simulation_id)
            if simulation is None:  # pragma: no cover - a foreign key keeps it
                msg = f"no simulation {simulation_id}"
                raise ReportNotFoundError(msg)
            simulations.append(simulation)
        return await self._run(
            report, version, (simulations[0], simulations[1]), trigger, force=force
        )

    async def _version(self, country: str, policyengine_version: str) -> CatalogVersion:
        version = await asyncio.to_thread(
            self.catalog.catalog_version, country, policyengine_version
        )
        if version is None:
            msg = f"{country} has no catalog for PolicyEngine.py {policyengine_version}"
            raise EconomyRequestError(msg)
        return version

    async def _population(
        self, version: CatalogVersion, region_code: str | None, dataset_name: str | None
    ) -> RegionPopulation:
        if region_code is None:  # pragma: no cover - an economy simulation has one
            msg = "an economy simulation names its region"
            raise EconomyRequestError(msg)
        try:
            return await asyncio.to_thread(
                self.catalog.population, version, region_code, dataset_name
            )
        except CatalogError as error:
            raise EconomyRequestError(str(error)) from error

    async def _plan(
        self, version: CatalogVersion, simulation: SimulationRecord
    ) -> _PlannedRun:
        population = await self._population(
            version, simulation.region_code, simulation.dataset_name
        )
        try:
            changes = await asyncio.to_thread(
                self.catalog.parameter_changes,
                version.id,
                policy_id=simulation.policy_id,
                dynamic_id=simulation.dynamic_id,
            )
        # A policy the version cannot run fails its run, not the request.
        except ValueError as error:
            return _PlannedRun(simulation, population, None, refusal=str(error))
        return _PlannedRun(simulation, population, changes)

    async def _run(
        self,
        report: ReportRecord,
        version: CatalogVersion,
        simulations: tuple[SimulationRecord, SimulationRecord],
        trigger: RunTrigger,
        *,
        force: bool,
    ) -> ReportRunRecord:
        """Find or create both runs and the report run, then start what can start."""
        planned = [await self._plan(version, simulation) for simulation in simulations]
        labor_supply_response = any(
            run.changes is not None
            and policy_affects_labor_supply(report.country, run.changes)
            for run in planned
        )
        # A run reused from a report without labour supply responses lacks
        # their columns, so such a report calculates its runs again.
        force_runs = force or labor_supply_response
        started: dict[UUID, StartedSimulationRun] = {}
        for run in planned:
            if run.simulation.id in started:
                continue
            started[run.simulation.id] = await asyncio.to_thread(
                lambda run=run: self.runs.start_simulation_run(
                    NewSimulationRun(
                        simulation_id=run.simulation.id,
                        tax_benefit_model_version_id=version.id,
                        year=report.year,
                        trigger=trigger,
                        region_code=run.population.region_code,
                        dataset_id=run.population.dataset_id,
                    ),
                    force=force_runs,
                )
            )
        baseline, reform = (started[run.simulation.id].run for run in planned)
        report_run = await asyncio.to_thread(
            self.runs.create_report_run,
            NewReportRun(
                report_id=report.id,
                baseline_simulation_run_id=baseline.id,
                reform_simulation_run_id=reform.id,
                trigger=trigger,
            ),
        )
        dispatched: set[UUID] = set()
        for run in planned:
            started_run = started[run.simulation.id]
            simulation_run = started_run.run
            if simulation_run.id in dispatched:
                continue
            dispatched.add(simulation_run.id)
            # A reused run still pending may be one a labour supply report
            # forced and is waiting for, so it keeps their columns.
            await self._start_simulation_run(
                simulation_run,
                run,
                version,
                labor_supply_response=labor_supply_response
                or (
                    not started_run.created
                    and simulation_run.status is RunStatus.PENDING
                    and await self._labor_supply_needed(
                        await asyncio.to_thread(
                            self.runs.report_runs_using, simulation_run.id
                        ),
                        version,
                    )
                ),
            )
        await self.start_comparisons_after([baseline.id, reform.id])
        return report_run

    # --- Simulation runs ---------------------------------------------------

    async def _start_simulation_run(
        self,
        simulation_run: SimulationRunRecord,
        planned: _PlannedRun,
        version: CatalogVersion,
        *,
        labor_supply_response: bool,
    ) -> None:
        """Start the run's call if it has not started; fail it if it cannot run."""
        if simulation_run.status is not RunStatus.PENDING:
            return
        if planned.changes is None:
            if await asyncio.to_thread(
                self.transitions.claim_simulation_dispatch, [simulation_run.id]
            ):
                await asyncio.to_thread(
                    self.transitions.record_simulation_dispatch_failure,
                    [simulation_run.id],
                    RunError(RunErrorCode.INVALID_INPUT, planned.refusal or ""),
                )
            return
        call = EconomySimulationCall(
            simulation_run_id=simulation_run.id,
            country=cast("CountryId", planned.simulation.country),
            year=simulation_run.year,
            policyengine_version=version.policyengine_version,
            policy=planned.changes,
            population=catalog_population(planned.population),
            labor_supply_response=labor_supply_response,
            artifact_environment=self.artifact_environment,
        )
        await self.dispatch.start_simulation_runs(
            [simulation_run.id],
            ModalRequest(
                target=ModalTarget(
                    application=v2_application_name(version.policyengine_version),
                    function=ECONOMY_SIMULATION_FUNCTIONS[call.country],
                ),
                arguments=(call.model_dump(mode="json"),),
            ),
        )

    # --- Work left waiting -------------------------------------------------

    async def resume_waiting_work(self, *, now: datetime) -> ResumedWork:
        """Start what a stopped instance or a lost report left waiting.

        The safety check runs this after recording due calls: comparison
        steps whose runs both succeeded, and runs still pending
        ``PENDING_RUN_START_AFTER`` after they were created. A run's call
        asks for labour supply columns when any report run comparing it
        needs them.
        """
        ready = await asyncio.to_thread(
            lambda: self.runs.comparisons_ready_to_start(limit=WAITING_WORK_BATCH_SIZE)
        )
        for report_run in ready:
            await self.start_comparison(report_run)
        stranded = await asyncio.to_thread(
            lambda: self.runs.pending_simulation_runs(
                created_before=now - PENDING_RUN_START_AFTER,
                limit=WAITING_WORK_BATCH_SIZE,
            )
        )
        started = 0
        for simulation_run in stranded:
            started += await self._start_stranded_run(simulation_run)
        return ResumedWork(comparisons_started=len(ready), runs_started=started)

    async def _labor_supply_needed(
        self, report_runs: list[ReportRunRecord], version: CatalogVersion
    ) -> bool:
        """Return whether any of these report runs compares an elasticity change.

        A run every one of them compares must then calculate the labour
        supply response columns.
        """
        for report_run in report_runs:
            report = await asyncio.to_thread(self.reports.get, report_run.report_id)
            if report is None:  # pragma: no cover - a foreign key keeps it
                continue
            for simulation_id in (
                report.baseline_simulation_id,
                report.reform_simulation_id,
            ):
                compared = (
                    None
                    if simulation_id is None
                    else await asyncio.to_thread(self.simulations.get, simulation_id)
                )
                if compared is None:  # pragma: no cover - a foreign key keeps it
                    continue
                planned = await self._plan(version, compared)
                if planned.changes is not None and policy_affects_labor_supply(
                    report.country, planned.changes
                ):
                    return True
        return False

    async def _start_stranded_run(self, simulation_run: SimulationRunRecord) -> int:
        """Start a run left pending; return 1 if a report run compares it."""
        version = await asyncio.to_thread(
            self.catalog.catalog_version_by_id,
            simulation_run.tax_benefit_model_version_id,
        )
        simulation = await asyncio.to_thread(
            self.simulations.get, simulation_run.simulation_id
        )
        report_runs = await asyncio.to_thread(
            self.runs.report_runs_using, simulation_run.id
        )
        if version is None or simulation is None or not report_runs:
            return 0
        labor_supply_response = await self._labor_supply_needed(report_runs, version)
        await self._start_simulation_run(
            simulation_run,
            await self._plan(version, simulation),
            version,
            labor_supply_response=labor_supply_response,
        )
        return 1

    # --- Comparisons -------------------------------------------------------

    async def start_comparisons_after(
        self, simulation_run_ids: Collection[UUID]
    ) -> None:
        """Start the comparison steps whose two runs have now both succeeded."""
        ready = await asyncio.to_thread(
            lambda: self.runs.comparisons_ready_to_start(
                simulation_run_ids=set(simulation_run_ids)
            )
        )
        for report_run in ready:
            await self.start_comparison(report_run)

    async def start_comparison(self, report_run: ReportRunRecord) -> None:
        """Start a report run's comparison step with both runs' stored outputs.

        A step whose output files are no longer offered, or whose report can
        no longer be resolved in its catalog version, is failed.
        """
        report = await asyncio.to_thread(self.reports.get, report_run.report_id)
        version = await asyncio.to_thread(
            self.catalog.catalog_version_by_id, report_run.tax_benefit_model_version_id
        )
        if (
            report is None
            or version is None
            or report_run.reform_simulation_run_id is None
        ):
            return
        sides = []
        for simulation_id, run_id in (
            (report.baseline_simulation_id, report_run.baseline_simulation_run_id),
            (report.reform_simulation_id, report_run.reform_simulation_run_id),
        ):
            simulation = (
                None
                if simulation_id is None
                else await asyncio.to_thread(self.simulations.get, simulation_id)
            )
            output = await asyncio.to_thread(
                self.runs.simulation_output_file, run_id, SIMULATION_OUTPUT_ROLE
            )
            if simulation is None or output is None:
                await self._fail_comparison(
                    report_run.id,
                    RunError(
                        RunErrorCode.RESULT_EXPIRED,
                        "A simulation run's stored output is no longer offered.",
                    ),
                )
                return
            try:
                planned = await self._plan(version, simulation)
            except EconomyRequestError as error:
                await self._fail_comparison(
                    report_run.id, RunError(RunErrorCode.INVALID_INPUT, str(error))
                )
                return
            if planned.changes is None:
                await self._fail_comparison(
                    report_run.id,
                    RunError(RunErrorCode.INVALID_INPUT, planned.refusal or ""),
                )
                return
            sides.append((planned, run_id, output))
        (
            (baseline, baseline_run_id, baseline_output),
            (
                reform,
                reform_run_id,
                reform_output,
            ),
        ) = sides
        call = EconomyComparisonCall(
            report_run_id=report_run.id,
            country=cast("CountryId", report.country),
            year=report_run.year,
            policyengine_version=version.policyengine_version,
            region_code=baseline.population.region_code,
            dataset_name=baseline.population.dataset_name,
            baseline=EconomyComparisonSide(
                simulation_run_id=baseline_run_id,
                policy=baseline.changes or {},
                output_uri=baseline_output.uri,
                output_sha256=baseline_output.sha256,
            ),
            reform=EconomyComparisonSide(
                simulation_run_id=reform_run_id,
                policy=reform.changes or {},
                output_uri=reform_output.uri,
                output_sha256=reform_output.sha256,
            ),
        )
        await self.dispatch.start_comparison(
            report_run.id,
            ModalRequest(
                target=ModalTarget(
                    application=v2_application_name(version.policyengine_version),
                    function=ECONOMY_COMPARISON_FUNCTION,
                ),
                arguments=(call.model_dump(mode="json"),),
            ),
        )

    async def _fail_comparison(self, report_run_id: UUID, error: RunError) -> None:
        if await asyncio.to_thread(
            self.transitions.claim_comparison_dispatch, report_run_id
        ):
            await asyncio.to_thread(
                self.transitions.record_comparison_dispatch_failure,
                report_run_id,
                error,
            )
            logger.warning(
                "report_run_comparison_not_started",
                extra={
                    "report_run_id": str(report_run_id),
                    "error_code": error.code.value,
                },
            )
