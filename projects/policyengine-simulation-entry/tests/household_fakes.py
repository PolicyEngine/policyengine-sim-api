"""In-memory household simulations and calculations, for the household tests.

``FakeHouseholdStore`` keeps household simulations and run setups and leaves
each run's status to ``FakeRunTransitions``, so the dispatcher, the recorder
and the reader all see one copy of every run. It reuses runs by the same
rules as ``RunStore``: an unfinished run, or one that succeeded within the
reuse window, is reused unless forced; a failed one never is.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from policyengine_simulation_contract.stage12_manifest import publish_v2_manifest
from policyengine_simulation_entry.household_workers import ManifestHouseholdWorkers
from policyengine_simulation_persistence import (
    SUCCEEDED_RUN_REUSE_WINDOW,
    HouseholdCalculation,
    HouseholdReport,
    ReportKind,
    ReportRecord,
    ReportRunRecord,
    RunStatus,
    SimulationKind,
    SimulationRecord,
    SimulationRunRecord,
    StartedSimulationRun,
)
from sqlalchemy.exc import IntegrityError
from stage12_fixtures import worker

if TYPE_CHECKING:
    from datetime import datetime

    from policyengine_simulation_persistence import (
        HouseholdOutput,
        NewReport,
        NewReportRun,
        NewSimulation,
        NewSimulationRun,
        RunTrigger,
    )
    from run_fakes import FakeRunTransitions, ManualClock

# The PolicyEngine.py version the fixture manifest's worker runs.
POLICYENGINE_VERSION = "5.2.0"
CATALOG_VERSION_ID = UUID("00000000-0000-4000-8000-00000000ca7a")


@dataclass(frozen=True)
class RunSetup:
    """What a fake run runs: its simulation, catalog version, year and attempt."""

    simulation_id: UUID
    catalog_version_id: UUID
    year: int
    attempt: int
    trigger: RunTrigger


class FakeHouseholdStore:
    """Household simulations and run setups over ``FakeRunTransitions``' runs."""

    def __init__(self, transitions: FakeRunTransitions, clock: ManualClock) -> None:
        """Start with no simulations."""
        self.transitions = transitions
        self.clock = clock
        self.simulations: dict[UUID, SimulationRecord] = {}
        self.setups: dict[UUID, RunSetup] = {}
        self.outputs: dict[UUID, HouseholdOutput] = {}
        # When each succeeded run's household was written, with its success.
        self.succeeded_at: dict[UUID, datetime] = {}
        self.reports: dict[UUID, ReportRecord] = {}
        self.report_runs: list[ReportRunRecord] = []
        # Households the Public API does not have: creating their simulations
        # breaks a foreign key.
        self.unknown_households: set[UUID] = set()
        # Catalog versions the Public API does not have: starting a run on one
        # breaks a foreign key.
        self.unknown_catalog_versions: set[UUID] = set()

    def find_or_create_simulation(
        self, new_simulation: NewSimulation
    ) -> SimulationRecord:
        """Return the household simulation with these inputs, or create it."""
        if new_simulation.household_id in self.unknown_households:
            statement = "INSERT INTO simulation.simulations"
            raise IntegrityError(
                statement, {}, LookupError(new_simulation.household_id)
            )
        for simulation in self.simulations.values():
            if (
                simulation.country,
                simulation.household_id,
                simulation.policy_id,
                simulation.dynamic_id,
                simulation.options,
            ) == (
                new_simulation.country,
                new_simulation.household_id,
                new_simulation.policy_id,
                new_simulation.dynamic_id,
                new_simulation.options,
            ):
                return simulation
        created = SimulationRecord(
            id=uuid4(),
            kind=SimulationKind.HOUSEHOLD,
            country=new_simulation.country,
            policy_id=new_simulation.policy_id,
            dynamic_id=new_simulation.dynamic_id,
            household_id=new_simulation.household_id,
            region_code=None,
            dataset_name=None,
            legacy_simulation_id=None,
            options=dict(new_simulation.options),
            created_at=self.clock(),
        )
        self.simulations[created.id] = created
        return created

    def start_run(
        self, new_run: NewSimulationRun, *, force: bool = False
    ) -> StartedSimulationRun:
        """Reuse the newest attempt for this setup, or add the next one."""
        if new_run.tax_benefit_model_version_id in self.unknown_catalog_versions:
            statement = "INSERT INTO simulation.simulation_runs"
            raise IntegrityError(
                statement, {}, LookupError(new_run.tax_benefit_model_version_id)
            )
        attempts = [
            run_id
            for run_id, setup in self.setups.items()
            if (setup.simulation_id, setup.catalog_version_id, setup.year)
            == (
                new_run.simulation_id,
                new_run.tax_benefit_model_version_id,
                new_run.year,
            )
        ]
        latest = max(
            attempts, key=lambda run_id: self.setups[run_id].attempt, default=None
        )
        if latest is not None and not force and self._reusable(latest):
            return StartedSimulationRun(run=self.run_record(latest), created=False)
        run_id = self.transitions.add_run("household", updated_at=self.clock())
        self.setups[run_id] = RunSetup(
            simulation_id=new_run.simulation_id,
            catalog_version_id=new_run.tax_benefit_model_version_id,
            year=new_run.year,
            attempt=1 if latest is None else self.setups[latest].attempt + 1,
            trigger=new_run.trigger,
        )
        return StartedSimulationRun(run=self.run_record(run_id), created=True)

    def _reusable(self, run_id: UUID) -> bool:
        status = self.transitions.runs[run_id].status
        if status is RunStatus.SUCCEEDED:
            return self.succeeded_at[run_id] >= (
                self.clock() - SUCCEEDED_RUN_REUSE_WINDOW
            )
        return status is not RunStatus.FAILED

    def run_record(self, run_id: UUID) -> SimulationRunRecord:
        """Return the run as the database would, from its fake execution."""
        execution = self.transitions.runs[run_id]
        setup = self.setups[run_id]
        error = execution.error
        finished = execution.status in {RunStatus.SUCCEEDED, RunStatus.FAILED}
        return SimulationRunRecord(
            id=run_id,
            simulation_id=setup.simulation_id,
            simulation_kind=SimulationKind.HOUSEHOLD,
            tax_benefit_model_version_id=setup.catalog_version_id,
            year=setup.year,
            attempt=setup.attempt,
            region_code=None,
            dataset_id=None,
            trigger=setup.trigger,
            status=execution.status,
            modal_call_id=execution.modal_call_id,
            error_code=None if error is None else error.code.value,
            error_message=None if error is None else error.message,
            error_detail=None if error is None else error.stored_detail(),
            check_count=execution.check_count,
            dispatched_at=None,
            next_check_at=execution.next_check_at,
            completed_at=self.clock() if finished else None,
            created_at=self.clock(),
            updated_at=execution.updated_at or self.clock(),
        )

    def read_calculation(self, run_id: UUID) -> HouseholdCalculation | None:
        """Return the household calculation run ``run_id``, or None."""
        if run_id not in self.setups:
            return None
        run = self.run_record(run_id)
        return HouseholdCalculation(
            run=run,
            simulation=self.simulations[run.simulation_id],
            output=(
                self.outputs.get(run_id) if run.status is RunStatus.SUCCEEDED else None
            ),
        )

    def get_simulation(self, simulation_id: UUID) -> SimulationRecord | None:
        """Return the simulation with this ID, or None."""
        return self.simulations.get(simulation_id)

    def create_report(self, new_report: NewReport) -> ReportRecord:
        """Keep a new household report."""
        report = ReportRecord(
            id=uuid4(),
            kind=ReportKind(new_report.kind),
            country=new_report.country,
            baseline_simulation_id=new_report.baseline_simulation_id,
            reform_simulation_id=new_report.reform_simulation_id,
            year=new_report.year,
            options=dict(new_report.options),
            legacy_report_id=None,
            created_at=self.clock(),
        )
        self.reports[report.id] = report
        return report

    def get_report(self, report_id: UUID) -> ReportRecord | None:
        """Return the household report with this ID, or None."""
        return self.reports.get(report_id)

    def create_report_run(self, new_report_run: NewReportRun) -> ReportRunRecord:
        """Keep a report run joining two of the fake's runs."""
        baseline = self.setups[new_report_run.baseline_simulation_run_id]
        report_run = ReportRunRecord(
            id=uuid4(),
            report_id=new_report_run.report_id,
            report_kind=ReportKind.HOUSEHOLD,
            tax_benefit_model_version_id=baseline.catalog_version_id,
            year=baseline.year,
            baseline_simulation_run_id=new_report_run.baseline_simulation_run_id,
            reform_simulation_run_id=new_report_run.reform_simulation_run_id,
            trigger=new_report_run.trigger,
            comparison_status=None,
            comparison_modal_call_id=None,
            comparison_error_code=None,
            comparison_error_message=None,
            comparison_error_detail=None,
            comparison_check_count=0,
            comparison_dispatched_at=None,
            comparison_next_check_at=None,
            comparison_completed_at=None,
            created_at=self.clock(),
            updated_at=self.clock(),
        )
        self.report_runs.append(report_run)
        return report_run

    def read_report(
        self, report_id: UUID, *, report_run_id: UUID | None = None
    ) -> HouseholdReport | None:
        """Return a report with a run (its newest unless named) and both runs."""
        report = self.reports.get(report_id)
        if report is None:
            return None
        runs = [
            report_run
            for report_run in self.report_runs
            if report_run.report_id == report_id
            and report_run_id in {None, report_run.id}
        ]
        if not runs and report_run_id is not None:
            return None
        report_run = runs[-1] if runs else None
        return HouseholdReport(
            report=report,
            report_run=report_run,
            baseline=(
                None
                if report_run is None
                else self.read_calculation(report_run.baseline_simulation_run_id)
            ),
            reform=(
                None
                if report_run is None or report_run.reform_simulation_run_id is None
                else self.read_calculation(report_run.reform_simulation_run_id)
            ),
        )

    def write_output(self, _connection, run, output: HouseholdOutput) -> None:
        """Keep a run's household, as the result writer stores it."""
        self.outputs[run.id] = output
        self.succeeded_at[run.id] = self.clock()


def manifest_workers() -> ManifestHouseholdWorkers:
    """Return household workers resolved from a manifest of the fixture worker."""
    store: dict[str, object] = {}
    publish_v2_manifest(store=store, worker=worker())
    return ManifestHouseholdWorkers.from_store(store)
