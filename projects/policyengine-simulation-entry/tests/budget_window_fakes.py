"""In-memory stores and a path builder for the budget-window tests."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, replace
from datetime import timedelta
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from economy_fakes import (
    CTC_REFORM,
    LABOR_SUPPLY_REFORM,
    FakeCatalog,
    FakeReports,
    FakeRuns,
    FakeSimulations,
)
from policyengine_simulation_contract.economy_runs import (
    EconomyComparisonCall,
    EconomySimulationCall,
)
from policyengine_simulation_entry.budget_window_reports import (
    BudgetWindowReports,
    BudgetWindowReportSubmission,
)
from policyengine_simulation_entry.economy_report_types import (
    EconomyReportSubmission,
    SimulationChoice,
)
from policyengine_simulation_entry.economy_reports import EconomyReports
from policyengine_simulation_entry.run_dispatch import RunDispatchService
from policyengine_simulation_persistence import (
    BudgetWindowRunRecord,
    BudgetWindowStatus,
    BudgetWindowYear,
    RunStatus,
    StartedBudgetWindowRun,
    YearBudget,
)
from run_fakes import FakeDispatcher

if TYPE_CHECKING:
    from datetime import datetime

    from policyengine_simulation_persistence import (
        NewBudgetWindowRun,
        NewReportRun,
        ReportRunRecord,
        RunError,
    )

REFORM_POLICY = uuid4()
LABOR_SUPPLY_POLICY = uuid4()


class WindowRuns(FakeRuns):
    """``FakeRuns`` whose report runs keep the budget-window run they belong to."""

    def create_report_run(self, new_report_run: NewReportRun) -> ReportRunRecord:
        row = replace(
            super().create_report_run(new_report_run),
            budget_window_run_id=new_report_run.budget_window_run_id,
        )
        self.report_runs_by_id[row.id] = row
        return row


@dataclass
class FakeBudgetWindows:
    """``BudgetWindowRunStore`` in memory, reading years from ``WindowRuns``."""

    economy_runs: WindowRuns
    rows: dict[UUID, BudgetWindowRunRecord] = field(default_factory=dict)
    budgets: dict[UUID, YearBudget] = field(default_factory=dict)
    claims_refused: bool = False

    def _insert(self, new_run: NewBudgetWindowRun, first_check_at: datetime):
        now = self.economy_runs.clock()
        row = BudgetWindowRunRecord(
            id=uuid4(),
            report_id=new_run.report_id,
            tax_benefit_model_version_id=new_run.tax_benefit_model_version_id,
            trigger=new_run.trigger,
            force=new_run.force,
            max_concurrent_years=new_run.max_concurrent_years,
            status=BudgetWindowStatus.RUNNING,
            error_code=None,
            error_message=None,
            next_check_at=first_check_at,
            scheduling_claimed_until=None,
            completed_at=None,
            created_at=now + timedelta(microseconds=len(self.rows)),
            updated_at=now,
        )
        self.rows[row.id] = row
        return row

    def create_run(self, new_run: NewBudgetWindowRun, *, first_check_at: datetime):
        return self._insert(new_run, first_check_at)

    def create_first_run(
        self, new_run: NewBudgetWindowRun, *, first_check_at: datetime
    ) -> StartedBudgetWindowRun:
        newest = self.latest_run(new_run.report_id)
        if newest is not None:
            return StartedBudgetWindowRun(run=newest, created=False)
        return StartedBudgetWindowRun(
            run=self._insert(new_run, first_check_at), created=True
        )

    def get_run(self, run_id: UUID) -> BudgetWindowRunRecord | None:
        return self.rows.get(run_id)

    def runs(self, report_id: UUID) -> list[BudgetWindowRunRecord]:
        return sorted(
            (row for row in self.rows.values() if row.report_id == report_id),
            key=lambda row: row.created_at,
            reverse=True,
        )

    def latest_run(self, report_id: UUID) -> BudgetWindowRunRecord | None:
        newest = self.runs(report_id)
        return newest[0] if newest else None

    def due_runs(self, *, now: datetime, limit: int) -> list[BudgetWindowRunRecord]:
        due = sorted(
            (
                row
                for row in self.rows.values()
                if row.status is BudgetWindowStatus.RUNNING
                and row.next_check_at is not None
                and row.next_check_at <= now
            ),
            key=lambda row: (row.next_check_at, row.created_at),
        )
        return due[:limit]

    def years(self, run_id: UUID) -> list[BudgetWindowYear]:
        year_runs = sorted(
            (
                report_run
                for report_run in self.economy_runs.report_runs_by_id.values()
                if report_run.budget_window_run_id == run_id
            ),
            key=lambda report_run: report_run.year,
        )
        simulation_runs = self.economy_runs.simulation_runs
        return [
            BudgetWindowYear(
                report_run=report_run,
                baseline_run=simulation_runs[report_run.baseline_simulation_run_id],
                reform_run=simulation_runs[
                    report_run.reform_simulation_run_id
                    or report_run.baseline_simulation_run_id
                ],
            )
            for report_run in year_runs
        ]

    def year_budgets(self, run_id: UUID) -> list[YearBudget]:
        return [
            self.budgets[year.report_run.id]
            for year in self.years(run_id)
            if year.report_run.id in self.budgets
        ]

    def claim_scheduling(self, run_id: UUID, *, lease: timedelta) -> bool:
        row = self.rows[run_id]
        now = self.economy_runs.clock()
        if (
            self.claims_refused
            or row.status is not BudgetWindowStatus.RUNNING
            or (
                row.scheduling_claimed_until is not None
                and row.scheduling_claimed_until > now
            )
        ):
            return False
        self.rows[run_id] = replace(row, scheduling_claimed_until=now + lease)
        return True

    def release_scheduling(self, run_id: UUID, *, next_check_at: datetime) -> None:
        row = self.rows[run_id]
        if row.status is BudgetWindowStatus.RUNNING:
            self.rows[run_id] = replace(
                row, scheduling_claimed_until=None, next_check_at=next_check_at
            )

    def finish_run(
        self,
        run_id: UUID,
        status: BudgetWindowStatus,
        error: RunError | None = None,
    ) -> bool:
        row = self.rows[run_id]
        if row.status is not BudgetWindowStatus.RUNNING:
            return False
        self.rows[run_id] = replace(
            row,
            status=status,
            error_code=None if error is None else error.code.value,
            error_message=None if error is None else error.message,
            completed_at=self.economy_runs.clock(),
            next_check_at=None,
            scheduling_claimed_until=None,
        )
        return True


def finish_year(runs: WindowRuns, report_run: ReportRunRecord) -> None:
    """Succeed a year's two simulation runs and its comparison."""
    for run_id in (
        report_run.baseline_simulation_run_id,
        report_run.reform_simulation_run_id,
    ):
        if run_id is not None and (
            runs.simulation_runs[run_id].status is not RunStatus.SUCCEEDED
        ):
            runs.succeed(run_id)
    runs.report_runs_by_id[report_run.id] = replace(
        runs.report_runs_by_id[report_run.id],
        comparison_status=RunStatus.SUCCEEDED,
        comparison_modal_call_id=f"fc-comparison-{report_run.id.hex[:6]}",
        comparison_completed_at=runs.clock(),
    )


class EconomyPath:
    """The economy path over in-memory stores, with every spawned request kept."""

    def __init__(self) -> None:
        self.catalog = FakeCatalog()
        self.catalog.changes.update(
            {REFORM_POLICY: CTC_REFORM, LABOR_SUPPLY_POLICY: LABOR_SUPPLY_REFORM}
        )
        self.simulations = FakeSimulations()
        self.simulations.policies_of_country["us"].update(
            {REFORM_POLICY, LABOR_SUPPLY_POLICY}
        )
        self.reports = FakeReports()
        self.runs = WindowRuns()
        self.dispatcher = FakeDispatcher()
        self.economy = EconomyReports(
            simulations=self.simulations,
            reports=self.reports,
            runs=self.runs,
            catalog=self.catalog,
            transitions=self.runs,
            dispatch=RunDispatchService(
                transitions=self.runs, dispatcher=self.dispatcher, clock=self.runs.clock
            ),
            artifact_environment="staging",
        )

    @staticmethod
    def submission(**changes: object) -> EconomyReportSubmission:
        """Return a California 2026 submission of the CTC reform."""
        values: dict[str, Any] = {
            "country": "us",
            "policyengine_version": "6.7.5",
            "year": 2026,
            "region_code": "state/ca",
            "baseline": SimulationChoice(),
            "reform": SimulationChoice(policy_id=REFORM_POLICY),
        }
        values.update(changes)
        return EconomyReportSubmission(**values)

    def prepare(self, *, window_size: int | None = 3, **changes: object):
        """Create a report, by default a three-year budget window from 2026."""
        return asyncio.run(
            self.economy.prepare_report(
                self.submission(**changes), budget_window_size=window_size
            )
        )

    def simulation_calls(self) -> list[EconomySimulationCall]:
        """Return the simulation calls started so far."""
        return [
            EconomySimulationCall.model_validate(request.arguments[0])
            for request in self.dispatcher.requests
            if request.target.function.startswith("run_economy_simulation")
        ]

    def comparison_calls(self) -> list[EconomyComparisonCall]:
        """Return the comparison calls started so far."""
        return [
            EconomyComparisonCall.model_validate(request.arguments[0])
            for request in self.dispatcher.requests
            if request.target.function == "compare_economy_simulations"
        ]


class WindowPath(EconomyPath):
    """The budget-window path over the economy path's in-memory stores."""

    def __init__(self, *, max_concurrent_years: int = 2) -> None:
        super().__init__()
        self.windows = FakeBudgetWindows(self.runs)
        self.budget_windows = BudgetWindowReports(
            economy=self.economy,
            reports=self.reports,
            windows=self.windows,
            runs=self.runs,
            catalog=self.catalog,
            max_concurrent_years=max_concurrent_years,
            clock=self.runs.clock,
        )

    def submit_window(self, *, window_size: int = 4, **changes: object):
        """Submit a budget window of the CTC reform, from 2026 by default."""
        return asyncio.run(
            self.budget_windows.submit(
                BudgetWindowReportSubmission(
                    report=self.submission(**changes), window_size=window_size
                )
            )
        )

    def year_runs(self, budget_window_run_id: UUID) -> dict[int, ReportRunRecord]:
        """Return the run's report runs by year."""
        return {
            report_run.year: report_run
            for report_run in self.runs.report_runs_by_id.values()
            if report_run.budget_window_run_id == budget_window_run_id
        }
