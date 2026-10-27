"""In-memory stores, catalog and transitions for the v2 economy path's tests."""

from __future__ import annotations

from dataclasses import dataclass, field, fields, replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from policyengine_simulation_persistence import (
    SUCCEEDED_RUN_REUSE_WINDOW,
    CatalogError,
    CatalogVersion,
    OutputFileRecord,
    RegionFilter,
    RegionPopulation,
    ReportKind,
    ReportRecord,
    ReportRunRecord,
    RunStatus,
    SimulationKind,
    SimulationRecord,
    SimulationRunRecord,
    StartedSimulationRun,
)

if TYPE_CHECKING:
    from collections.abc import Collection

    from policyengine_simulation_persistence import (
        NewReport,
        NewReportRun,
        NewSimulation,
        NewSimulationRun,
        RunError,
    )

NOW = datetime(2026, 10, 27, 12, tzinfo=UTC)
US_VERSION = CatalogVersion(id=uuid4(), country="us", policyengine_version="6.7.5")
UK_VERSION = CatalogVersion(id=uuid4(), country="uk", policyengine_version="6.7.5")
STATE_DATASET_ID = uuid4()
NATIONAL_DATASET_ID = uuid4()
CALIFORNIA = RegionPopulation(
    region_code="state/ca",
    region_type="state",
    dataset_id=STATE_DATASET_ID,
    dataset_name="populace_us_2024_acs_local",
    row_filter=RegionFilter(field="state_fips", value="6"),
    state_code="CA",
    state_filters=None,
)
UNITED_STATES = RegionPopulation(
    region_code="us",
    region_type="national",
    dataset_id=NATIONAL_DATASET_ID,
    dataset_name="populace_us_2024",
    row_filter=None,
    state_code=None,
    state_filters={"state/ca": RegionFilter(field="state_fips", value="6")},
)
CTC_REFORM = {"gov.irs.credits.ctc.amount.base[0].amount": {"2026-01-01": 3000}}
LABOR_SUPPLY_REFORM = {
    "gov.simulation.labor_supply_responses.elasticities.income": {"2026-01-01": 0.1}
}


class FakeCatalog:
    """Two catalog versions, California and the US, and scripted policies."""

    def __init__(self) -> None:
        self.versions = {("us", "6.7.5"): US_VERSION, ("uk", "6.7.5"): UK_VERSION}
        self.populations = {"state/ca": CALIFORNIA, "us": UNITED_STATES}
        self.changes: dict[UUID | None, dict[str, Any] | Exception] = {None: {}}

    def catalog_version(self, country: str, policyengine_version: str):
        return self.versions.get((country, policyengine_version))

    def catalog_version_by_id(self, version_id: UUID):
        return next(
            (version for version in self.versions.values() if version.id == version_id),
            None,
        )

    def population(self, version, region_code: str, dataset_name: str | None):
        population = self.populations.get(region_code)
        if population is None:
            msg = f"version {version.policyengine_version} has no region {region_code}"
            raise CatalogError(msg)
        if dataset_name not in {None, population.dataset_name}:
            msg = f"no dataset {dataset_name}"
            raise CatalogError(msg)
        return population

    def parameter_changes(self, _version_id, *, policy_id, dynamic_id):
        del dynamic_id
        changes = self.changes[policy_id]
        if isinstance(changes, Exception):
            raise changes
        return changes

    def area_labels(self, _version_id):
        from policyengine_simulation_persistence import EconomyResultLabels

        return EconomyResultLabels()


class FakeSimulations:
    def __init__(self) -> None:
        self.rows: dict[UUID, SimulationRecord] = {}
        self.policies_of_country: dict[str, set[UUID | None]] = {
            "us": {None},
            "uk": {None},
        }

    def find_or_create(self, new_simulation: NewSimulation) -> SimulationRecord:
        if (
            new_simulation.policy_id
            not in self.policies_of_country[new_simulation.country]
        ):
            from sqlalchemy.exc import IntegrityError

            statement = "INSERT"
            raise IntegrityError(
                statement, {}, Exception("fk_simulations_policy_country")
            )
        for row in self.rows.values():
            if all(
                getattr(row, name) == getattr(new_simulation, name)
                for name in (
                    "kind",
                    "country",
                    "policy_id",
                    "dynamic_id",
                    "household_id",
                    "region_code",
                    "dataset_name",
                )
            ):
                return row
        row = SimulationRecord(
            id=uuid4(),
            created_at=NOW,
            **{
                item.name: getattr(new_simulation, item.name)
                for item in fields(new_simulation)
            },
        )
        self.rows[row.id] = row
        return row

    def get(self, simulation_id: UUID) -> SimulationRecord | None:
        return self.rows.get(simulation_id)


class FakeReports:
    def __init__(self) -> None:
        self.rows: dict[UUID, ReportRecord] = {}

    def create(self, new_report: NewReport) -> ReportRecord:
        row = ReportRecord(
            id=uuid4(),
            created_at=NOW,
            **{
                item.name: getattr(new_report, item.name) for item in fields(new_report)
            },
        )
        self.rows[row.id] = row
        return row

    def get(self, report_id: UUID) -> ReportRecord | None:
        return self.rows.get(report_id)

    def get_by_legacy_report_id(self, legacy_report_id: int) -> ReportRecord | None:
        return next(
            (
                row
                for row in self.rows.values()
                if row.legacy_report_id == legacy_report_id
            ),
            None,
        )


def simulation_run(**values: Any) -> SimulationRunRecord:
    defaults: dict[str, Any] = {
        "id": uuid4(),
        "simulation_kind": SimulationKind.ECONOMY,
        "year": 2026,
        "attempt": 1,
        "region_code": "state/ca",
        "dataset_id": STATE_DATASET_ID,
        "status": RunStatus.PENDING,
        "modal_call_id": None,
        "error_code": None,
        "error_message": None,
        "check_count": 0,
        "dispatched_at": None,
        "next_check_at": None,
        "completed_at": None,
        "created_at": NOW,
        "updated_at": NOW,
    }
    defaults.update(values)
    return SimulationRunRecord(**defaults)


@dataclass
class FakeRuns:
    """Runs, report runs and their files, with RunStore's reuse rules."""

    clock: Any = lambda: NOW
    simulation_runs: dict[UUID, SimulationRunRecord] = field(default_factory=dict)
    report_runs_by_id: dict[UUID, ReportRunRecord] = field(default_factory=dict)
    files: dict[tuple[UUID, str], OutputFileRecord] = field(default_factory=dict)

    # --- RunStore ----------------------------------------------------------

    def start_simulation_run(self, new_run: NewSimulationRun, *, force: bool = False):
        attempts = sorted(
            (
                run
                for run in self.simulation_runs.values()
                if run.simulation_id == new_run.simulation_id
                and run.tax_benefit_model_version_id
                == new_run.tax_benefit_model_version_id
                and run.year == new_run.year
            ),
            key=lambda run: run.attempt,
        )
        latest = attempts[-1] if attempts else None
        if latest is not None and not force and self._reusable(latest):
            return StartedSimulationRun(run=latest, created=False)
        run = simulation_run(
            simulation_id=new_run.simulation_id,
            tax_benefit_model_version_id=new_run.tax_benefit_model_version_id,
            year=new_run.year,
            attempt=1 if latest is None else latest.attempt + 1,
            region_code=new_run.region_code,
            dataset_id=new_run.dataset_id,
            trigger=new_run.trigger,
        )
        self.simulation_runs[run.id] = run
        return StartedSimulationRun(run=run, created=True)

    def _reusable(self, run: SimulationRunRecord) -> bool:
        if run.status in {RunStatus.PENDING, RunStatus.DISPATCHING, RunStatus.RUNNING}:
            return True
        return (
            run.status is RunStatus.SUCCEEDED
            and run.completed_at is not None
            and run.completed_at >= self.clock() - SUCCEEDED_RUN_REUSE_WINDOW
        )

    def create_report_run(self, new_report_run: NewReportRun) -> ReportRunRecord:
        baseline = self.simulation_runs[new_report_run.baseline_simulation_run_id]
        row = ReportRunRecord(
            id=uuid4(),
            report_id=new_report_run.report_id,
            report_kind=ReportKind.ECONOMY,
            tax_benefit_model_version_id=baseline.tax_benefit_model_version_id,
            year=baseline.year,
            baseline_simulation_run_id=new_report_run.baseline_simulation_run_id,
            reform_simulation_run_id=new_report_run.reform_simulation_run_id,
            trigger=new_report_run.trigger,
            comparison_status=RunStatus.PENDING,
            comparison_modal_call_id=None,
            comparison_error_code=None,
            comparison_error_message=None,
            comparison_check_count=0,
            comparison_dispatched_at=None,
            comparison_next_check_at=None,
            comparison_completed_at=None,
            created_at=self.clock()
            + timedelta(microseconds=len(self.report_runs_by_id)),
            updated_at=self.clock(),
        )
        self.report_runs_by_id[row.id] = row
        return row

    def get_simulation_run(self, run_id: UUID):
        return self.simulation_runs.get(run_id)

    def get_report_run(self, report_run_id: UUID):
        return self.report_runs_by_id.get(report_run_id)

    def report_runs(self, report_id: UUID) -> list[ReportRunRecord]:
        return sorted(
            (
                run
                for run in self.report_runs_by_id.values()
                if run.report_id == report_id
            ),
            key=lambda run: run.created_at,
            reverse=True,
        )

    def latest_report_run(self, report_id: UUID):
        runs = self.report_runs(report_id)
        return runs[0] if runs else None

    def comparisons_ready_to_start(self, *, simulation_run_ids=None, limit=100):
        ready = [
            run
            for run in self.report_runs_by_id.values()
            if run.comparison_status is RunStatus.PENDING
            and self.simulation_runs[run.baseline_simulation_run_id].status
            is RunStatus.SUCCEEDED
            and run.reform_simulation_run_id is not None
            and self.simulation_runs[run.reform_simulation_run_id].status
            is RunStatus.SUCCEEDED
            and (
                simulation_run_ids is None
                or {run.baseline_simulation_run_id, run.reform_simulation_run_id}
                & set(simulation_run_ids)
            )
        ]
        return ready[:limit]

    def pending_simulation_runs(
        self, *, created_before, simulation_kind=None, limit=100
    ):
        return [
            run
            for run in self.simulation_runs.values()
            if run.status is RunStatus.PENDING
            and run.created_at < created_before
            and simulation_kind in {None, run.simulation_kind}
        ][:limit]

    def report_runs_using(self, simulation_run_id: UUID) -> list[ReportRunRecord]:
        return [
            run
            for run in self.report_runs_by_id.values()
            if simulation_run_id
            in {run.baseline_simulation_run_id, run.reform_simulation_run_id}
        ]

    def simulation_output_file(self, simulation_run_id, role, *, now=None):
        del now
        return self.files.get((simulation_run_id, role))

    # --- Test set-up -------------------------------------------------------

    def succeed(self, run_id: UUID, *, days_ago: int = 0) -> None:
        completed = self.clock() - timedelta(days=days_ago)
        self.simulation_runs[run_id] = replace(
            self.simulation_runs[run_id],
            status=RunStatus.SUCCEEDED,
            modal_call_id=f"fc-done-{run_id.hex[:6]}",
            completed_at=completed,
        )
        self.files[(run_id, "simulation_output")] = OutputFileRecord(
            id=uuid4(),
            simulation_run_id=run_id,
            report_run_id=None,
            role="simulation_output",
            uri=f"gs://private/stage-12-runs/staging/2026/10/{run_id}/output.parquet",
            sha256="a" * 64,
            written_at=completed,
        )

    # --- RunTransitions ----------------------------------------------------

    def _change_runs(self, run_ids, expected: RunStatus, **values: Any) -> bool:
        wanted = set(run_ids)
        if any(
            self.simulation_runs[run_id].status is not expected for run_id in wanted
        ):
            return False
        for run_id in wanted:
            self.simulation_runs[run_id] = replace(
                self.simulation_runs[run_id], **values
            )
        return True

    def claim_simulation_dispatch(self, run_ids: Collection[UUID]) -> bool:
        return self._change_runs(
            run_ids, RunStatus.PENDING, status=RunStatus.DISPATCHING
        )

    def record_simulation_dispatched(self, run_ids, *, modal_call_id, first_check_at):
        return self._change_runs(
            run_ids,
            RunStatus.DISPATCHING,
            status=RunStatus.RUNNING,
            modal_call_id=modal_call_id,
            next_check_at=first_check_at,
        )

    def record_simulation_dispatch_failure(self, run_ids, error: RunError) -> bool:
        return self._change_runs(
            run_ids,
            RunStatus.DISPATCHING,
            status=RunStatus.FAILED,
            error_code=error.code.value,
            error_message=error.message,
            completed_at=self.clock(),
        )

    def simulation_runs_hold_call(self, run_ids, modal_call_id) -> bool:
        return all(
            self.simulation_runs[run_id].modal_call_id == modal_call_id
            for run_id in run_ids
        )

    def _change_comparison(self, report_run_id, expected: RunStatus, **values) -> bool:
        run = self.report_runs_by_id[report_run_id]
        if run.comparison_status is not expected:
            return False
        self.report_runs_by_id[report_run_id] = replace(run, **values)
        return True

    def claim_comparison_dispatch(self, report_run_id: UUID) -> bool:
        run = self.report_runs_by_id[report_run_id]
        if not all(
            self.simulation_runs[run_id].status is RunStatus.SUCCEEDED
            for run_id in (run.baseline_simulation_run_id, run.reform_simulation_run_id)
            if run_id is not None
        ):
            return False
        return self._change_comparison(
            report_run_id, RunStatus.PENDING, comparison_status=RunStatus.DISPATCHING
        )

    def record_comparison_dispatched(
        self, report_run_id, *, modal_call_id, first_check_at
    ):
        return self._change_comparison(
            report_run_id,
            RunStatus.DISPATCHING,
            comparison_status=RunStatus.RUNNING,
            comparison_modal_call_id=modal_call_id,
            comparison_next_check_at=first_check_at,
        )

    def record_comparison_dispatch_failure(
        self, report_run_id, error: RunError
    ) -> bool:
        return self._change_comparison(
            report_run_id,
            RunStatus.DISPATCHING,
            comparison_status=RunStatus.FAILED,
            comparison_error_code=error.code.value,
            comparison_error_message=error.message,
            comparison_completed_at=self.clock(),
        )

    def comparison_holds_call(self, report_run_id, modal_call_id) -> bool:
        return (
            self.report_runs_by_id[report_run_id].comparison_modal_call_id
            == modal_call_id
        )
