"""Submit and re-run v2 economy reports: simulation runs first, then comparisons.

A report compares two simulations of one region (``simulation.reports``).
Running it on a catalog version and year:

1. finds or creates a run of each simulation for that version and year,
   reusing one that is unfinished or succeeded within 28 days (the
   current-law baseline, most often), and records a report run joining them;
2. starts one Modal call per run it created, with the region and
   dataset rows of the run's catalog version and its policy's parameter
   values by name (``EconomySimulationCall``); a policy changing a parameter
   the version lacks fails its run as ``invalid_input``;
3. starts the report run's comparison step (``EconomyComparisonCall``) once
   both runs have succeeded: at once when both were reused, or when a call's
   report says that its run succeeded (``start_comparisons_after``).

A budget-window report (``budget_window_reports``) runs the same steps once
per year of its window: ``run_year`` records each year's report run as one
year of its budget-window run. The single-year path neither runs it nor
counts its runs as the report's.

A report whose policies change a labour supply elasticity needs both
simulations' labour supply response columns, which a run calculates only
when asked, so its runs are always calculated again (forced) and asked for
them. Report options that would change a simulation's output (an SPM
selection, cliff impacts) are not accepted yet, because a reused run could
not tell which ones it was calculated with.

Every status change is one of ``RunTransitions``' conditional writes, so a
call starts once whoever else tries. The path's requests, errors and store
protocols are in ``economy_report_types``.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING, Any, cast

from policyengine_simulation_contract.economy_runs import (
    ECONOMY_COMPARISON_FUNCTION,
    ECONOMY_SIMULATION_FUNCTIONS,
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
from policyengine_simulation_entry.economy_report_types import (
    EconomyRequestError,
    ReportNotFoundError,
    ResumedWork,
    catalog_population,
)
from policyengine_simulation_entry.modal_calls import ModalRequest, ModalTarget
from policyengine_simulation_entry.run_dispatch import DispatchOutcome

if TYPE_CHECKING:
    from collections.abc import Collection, Coroutine
    from datetime import datetime
    from uuid import UUID

    from policyengine_simulation_contract.stage12_bundle import CountryId
    from policyengine_simulation_persistence import (
        CatalogVersion,
        RegionPopulation,
        ReportRecord,
        ReportRunRecord,
        SimulationRecord,
        SimulationRunRecord,
        StartedSimulationRun,
    )

    from policyengine_simulation_entry.economy_report_types import (
        EconomyCatalog,
        EconomyReportSubmission,
        EconomyRuns,
        FailureTransitions,
        ReportDefinitions,
        SimulationDefinitions,
    )
    from policyengine_simulation_entry.run_dispatch import RunDispatchService

logger = logging.getLogger(__name__)

RERUN_TRIGGERS = frozenset({RunTrigger.MANUAL, RunTrigger.SYSTEM})
# A run still pending this long after it was created was left by a submitter
# that stopped before starting its call; the safety check starts it.
PENDING_RUN_START_AFTER = timedelta(minutes=10)
WAITING_WORK_BATCH_SIZE = 50


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


@dataclass(frozen=True)
class ReportSetup:
    """A report and its two simulations, to run on one catalog version."""

    report: ReportRecord
    version: CatalogVersion
    simulations: tuple[SimulationRecord, SimulationRecord]


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

        Raise EconomyRequestError, before anything is written, as
        ``prepare_report`` does.
        """
        setup = await self.prepare_report(submission)
        report_run = await self._run(
            setup, setup.report.year, RunTrigger.INITIAL, force=False
        )
        return setup.report, report_run

    async def prepare_report(
        self,
        submission: EconomyReportSubmission,
        *,
        budget_window_size: int | None = None,
    ) -> ReportSetup:
        """Create a report and its simulations, without running it.

        ``budget_window_size`` makes it a budget-window report of that many
        years from the submission's year. Raise EconomyRequestError, before
        anything is written, when the country has no catalog for the
        policyengine.py version, the version has no such region or dataset,
        options are given, or a policy or dynamic does not exist for the
        country.
        """
        if submission.options:
            msg = "economy reports take no options yet"
            raise EconomyRequestError(msg)
        version = await self.catalog_version(
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
                budget_window_size=budget_window_size,
            ),
        )
        return ReportSetup(report, version, (baseline, reform))

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
        migrated report. A ``system`` run returns the report's newest run
        when it has one, and simultaneous ``system`` runs of a report without
        one record a single report run. Existing simulation runs are reused
        unless ``force`` is set. Raise ReportNotFoundError for an unknown
        report, and EconomyRequestError for one that cannot run.
        """
        if trigger not in RERUN_TRIGGERS:
            msg = "a report is run again with the manual or system trigger"
            raise EconomyRequestError(msg)
        report = await asyncio.to_thread(self.reports.get, report_id)
        if report is None:
            msg = f"no report {report_id}"
            raise ReportNotFoundError(msg)
        if (
            report.kind is not ReportKind.ECONOMY
            or report.reform_simulation_id is None
            or report.budget_window_size is not None
        ):
            msg = f"no economy report {report_id}"
            raise ReportNotFoundError(msg)
        if report.options:
            msg = "economy reports take no options yet"
            raise EconomyRequestError(msg)
        if trigger is RunTrigger.SYSTEM:
            newest = await asyncio.to_thread(self.runs.latest_report_run, report_id)
            if newest is not None:
                return newest
        version = await self.catalog_version(report.country, policyengine_version)
        setup = await self.report_setup(report, version)
        return await self._run(setup, report.year, trigger, force=force)

    async def report_setup(
        self, report: ReportRecord, version: CatalogVersion
    ) -> ReportSetup:
        """Return an economy report with its two simulations, to run on a version."""
        simulations = []
        for simulation_id in (
            report.baseline_simulation_id,
            report.reform_simulation_id,
        ):
            simulation = (
                None
                if simulation_id is None
                else await asyncio.to_thread(self.simulations.get, simulation_id)
            )
            if simulation is None:  # pragma: no cover - a foreign key keeps it
                msg = f"no simulation {simulation_id}"
                raise ReportNotFoundError(msg)
            simulations.append(simulation)
        return ReportSetup(report, version, (simulations[0], simulations[1]))

    async def check_setup(self, setup: ReportSetup) -> None:
        """Raise EconomyRequestError unless both simulations' rows resolve.

        Each simulation's region and dataset must exist in the setup's
        catalog version, in a form a call can carry, so a caller can refuse
        a report before it writes anything.
        """
        for simulation in setup.simulations:
            await self._population(
                setup.version, simulation.region_code, simulation.dataset_name
            )

    async def run_year(
        self,
        setup: ReportSetup,
        *,
        year: int,
        trigger: RunTrigger,
        force: bool,
        budget_window_run_id: UUID | None = None,
    ) -> ReportRunRecord:
        """Run a report's two simulations and their comparison for one year.

        The report run is one year of ``budget_window_run_id``, a
        budget-window run of the report, and is recorded as a run of its
        own whatever the trigger. Runs are reused, forced and started as
        for a single-year report.
        """
        return await self._run(
            setup,
            year,
            trigger,
            force=force,
            budget_window_run_id=budget_window_run_id,
        )

    async def catalog_version(
        self, country: str, policyengine_version: str
    ) -> CatalogVersion:
        """Return the country's catalog version; raise EconomyRequestError if none."""
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
            population = await asyncio.to_thread(
                self.catalog.population, version, region_code, dataset_name
            )
        except CatalogError as error:
            raise EconomyRequestError(str(error)) from error
        # Refuse catalog rows a call could not carry before anything is
        # written or started.
        try:
            catalog_population(population)
        except ValueError as error:
            msg = (
                f"the catalog's {region_code} cannot be sent to a calculation: "
                f"{type(error).__name__}"
            )
            raise EconomyRequestError(msg) from error
        return population

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
        setup: ReportSetup,
        year: int,
        trigger: RunTrigger,
        *,
        force: bool,
        budget_window_run_id: UUID | None = None,
    ) -> ReportRunRecord:
        """Find or create both runs and the report run, then start what can start."""
        report, version = setup.report, setup.version
        planned = [
            await self._plan(version, simulation) for simulation in setup.simulations
        ]
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
                        year=year,
                        trigger=trigger,
                        region_code=run.population.region_code,
                        dataset_id=run.population.dataset_id,
                    ),
                    force=force_runs,
                )
            )
        baseline, reform = (started[run.simulation.id].run for run in planned)
        report_run = await self._record_report_run(
            NewReportRun(
                report_id=report.id,
                baseline_simulation_run_id=baseline.id,
                reform_simulation_run_id=reform.id,
                trigger=trigger,
                budget_window_run_id=budget_window_run_id,
            )
        )
        compared = {
            run_id
            for run_id in (
                report_run.baseline_simulation_run_id,
                report_run.reform_simulation_run_id,
            )
            if run_id is not None
        }
        # Only the submission that created a run starts it. A reused run
        # still pending belongs to the submission that created it, which may
        # be a labour supply report asking for more columns; if that
        # submitter stopped, the safety check starts it for every report
        # run comparing it.
        dispatched: set[UUID] = set()
        for run in planned:
            started_run = started[run.simulation.id]
            if not started_run.created or started_run.run.id in dispatched:
                continue
            dispatched.add(started_run.run.id)
            if started_run.run.id not in compared:
                # Another first open of the report recorded its run with its
                # own runs. This one stays pending: another report may have
                # reused it meanwhile, and the safety check starts it for
                # them, or fails it when nothing compares it.
                continue
            await self._start_simulation_run(
                started_run.run,
                run,
                version,
                labor_supply_response=labor_supply_response,
            )
        await self.start_comparisons_after(compared)
        return report_run

    async def _record_report_run(self, new_report_run: NewReportRun) -> ReportRunRecord:
        """Record a report run; a ``system`` one only while the report has none.

        Simultaneous first opens of a report without a run then share one
        report run. A caller that loses still starts the runs it created
        that the recorded run compares, and leaves the others pending for the
        safety check. A year of a budget-window run is its own run, whatever
        the trigger; a caller that starts a year another caller has just
        started shares that caller's report run in the same way.
        """
        if new_report_run.budget_window_run_id is not None:
            year = await asyncio.to_thread(
                self.runs.create_window_year_run, new_report_run
            )
            return year.run
        if new_report_run.trigger is not RunTrigger.SYSTEM:
            return await asyncio.to_thread(self.runs.create_report_run, new_report_run)
        first = await asyncio.to_thread(
            self.runs.create_first_report_run, new_report_run
        )
        return first.run

    # --- Simulation runs ---------------------------------------------------

    async def _start_simulation_run(
        self,
        simulation_run: SimulationRunRecord,
        planned: _PlannedRun,
        version: CatalogVersion,
        *,
        labor_supply_response: bool,
    ) -> bool:
        """Start the run's call if it has not started; fail it if it cannot run.

        Return whether this call started it.
        """
        if simulation_run.status is not RunStatus.PENDING:
            return False
        if planned.changes is None:
            await self._fail_simulation_run(
                simulation_run.id,
                RunError(RunErrorCode.INVALID_INPUT, planned.refusal or ""),
            )
            return False
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
        outcome = await self.dispatch.start_simulation_runs(
            [simulation_run.id],
            ModalRequest(
                target=ModalTarget(
                    application=v2_application_name(version.policyengine_version),
                    function=ECONOMY_SIMULATION_FUNCTIONS[call.country],
                ),
                arguments=(call.model_dump(mode="json"),),
            ),
        )
        return outcome is DispatchOutcome.STARTED

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
        comparisons_started = 0
        for report_run in ready:
            comparisons_started += await self._resume(
                self.start_comparison(report_run), report_run_id=str(report_run.id)
            )
        stranded = await asyncio.to_thread(
            lambda: self.runs.pending_simulation_runs(
                created_before=now - PENDING_RUN_START_AFTER,
                simulation_kind=SimulationKind.ECONOMY,
                limit=WAITING_WORK_BATCH_SIZE,
            )
        )
        runs_started = 0
        for simulation_run in stranded:
            runs_started += await self._resume(
                self._start_stranded_run(simulation_run),
                simulation_run_id=str(simulation_run.id),
            )
        return ResumedWork(
            comparisons_started=comparisons_started, runs_started=runs_started
        )

    @staticmethod
    async def _resume(start: Coroutine[Any, Any, bool], **subject: str) -> int:
        """Start one piece of waiting work; return 1 if its call started.

        A failure is logged and leaves the item for the next pass, so one
        item cannot stop the others.
        """
        try:
            return int(await start)
        except Exception as error:  # noqa: BLE001  # logged; the next pass tries again
            logger.warning(
                "economy_work_not_resumed",
                extra={**subject, "error_type": type(error).__name__},
            )
            return 0

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

    async def _start_stranded_run(self, simulation_run: SimulationRunRecord) -> bool:
        """Start a run left pending; return whether its call started.

        A run no report run compares, or that its catalog version can no
        longer resolve, is failed, so the safety check stops picking it.
        """
        version = await asyncio.to_thread(
            self.catalog.catalog_version_by_id,
            simulation_run.tax_benefit_model_version_id,
        )
        simulation = await asyncio.to_thread(
            self.simulations.get, simulation_run.simulation_id
        )
        if version is None or simulation is None:  # pragma: no cover - foreign keys
            return False
        report_runs = await asyncio.to_thread(
            self.runs.report_runs_using, simulation_run.id
        )
        if not report_runs:
            await self._fail_simulation_run(
                simulation_run.id,
                RunError(
                    RunErrorCode.DISPATCH_ABANDONED,
                    "No report run compares the run, so no call was started.",
                ),
            )
            return False
        try:
            planned = await self._plan(version, simulation)
        except EconomyRequestError as error:
            await self._fail_simulation_run(
                simulation_run.id, RunError(RunErrorCode.INVALID_INPUT, str(error))
            )
            return False
        return await self._start_simulation_run(
            simulation_run,
            planned,
            version,
            labor_supply_response=await self._labor_supply_needed(report_runs, version),
        )

    async def _fail_simulation_run(self, run_id: UUID, error: RunError) -> None:
        """Fail a pending run that cannot start, if no one else has claimed it."""
        if await asyncio.to_thread(
            self.transitions.claim_simulation_dispatch, [run_id]
        ):
            await asyncio.to_thread(
                self.transitions.record_simulation_dispatch_failure, [run_id], error
            )

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

    async def start_comparison(self, report_run: ReportRunRecord) -> bool:
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
            return False
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
                return False
            try:
                planned = await self._plan(version, simulation)
            except EconomyRequestError as error:
                await self._fail_comparison(
                    report_run.id, RunError(RunErrorCode.INVALID_INPUT, str(error))
                )
                return False
            if planned.changes is None:
                await self._fail_comparison(
                    report_run.id,
                    RunError(RunErrorCode.INVALID_INPUT, planned.refusal or ""),
                )
                return False
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
        outcome = await self.dispatch.start_comparison(
            report_run.id,
            ModalRequest(
                target=ModalTarget(
                    application=v2_application_name(version.policyengine_version),
                    function=ECONOMY_COMPARISON_FUNCTION,
                ),
                arguments=(call.model_dump(mode="json"),),
            ),
        )
        return outcome is DispatchOutcome.STARTED

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
