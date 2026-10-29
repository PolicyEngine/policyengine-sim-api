"""In-memory stores and a path builder for the budget-window tests."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import TYPE_CHECKING, Any
from uuid import uuid4

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
from policyengine_simulation_entry.economy_report_types import (
    EconomyReportSubmission,
    SimulationChoice,
)
from policyengine_simulation_entry.economy_reports import EconomyReports
from policyengine_simulation_entry.run_dispatch import RunDispatchService
from run_fakes import FakeDispatcher

if TYPE_CHECKING:
    from policyengine_simulation_persistence import NewReportRun, ReportRunRecord

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
