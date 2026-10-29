"""Builders for the budget-window run tests."""

from __future__ import annotations

from typing import TYPE_CHECKING

from policyengine_simulation_persistence import (
    BudgetWindowRunStore,
    NewBudgetWindowRun,
    NewReportRun,
    RunTrigger,
    create_simulation_engine,
)
from policyengine_simulation_persistence.result_tables import budget_impacts
from report_support import economy_pair, economy_report, report_store
from run_support import economy_run, run_store

if TYPE_CHECKING:
    from uuid import UUID

    from policyengine_simulation_persistence import (
        ReportRecord,
        ReportRunRecord,
        SimulationRecord,
    )
    from public_api_support import PublicApiRows
    from transition_support import ManualClock

WINDOW_START = 2026
WINDOW_SIZE = 3


def budget_window_store(
    database, clock: ManualClock | None = None
) -> BudgetWindowRunStore:
    """Return a store that connects as the runtime role."""
    engine = create_simulation_engine(database.runtime_url)
    if clock is None:
        return BudgetWindowRunStore(engine)
    return BudgetWindowRunStore(engine, clock=clock)


def budget_window_report(
    database, rows: PublicApiRows, *, window_size: int = WINDOW_SIZE
) -> tuple[ReportRecord, tuple[SimulationRecord, SimulationRecord]]:
    """Create a California budget-window report from 2026, and its simulations."""
    baseline, reform = economy_pair(database, rows)
    report = report_store(database).create(
        economy_report(
            baseline, reform, year=WINDOW_START, budget_window_size=window_size
        )
    )
    return report, (baseline, reform)


def new_budget_window_run(
    report: ReportRecord, rows: PublicApiRows, **overrides: object
) -> NewBudgetWindowRun:
    """Return a first calculation of the report on the first catalog version."""
    values: dict[str, object] = {
        "report_id": report.id,
        "tax_benefit_model_version_id": rows.catalog_version_id,
        "trigger": RunTrigger.INITIAL,
        "max_concurrent_years": 2,
    }
    values.update(overrides)
    return NewBudgetWindowRun(**values)  # type: ignore[arg-type]


def year_report_run(
    database,
    rows: PublicApiRows,
    window: tuple[ReportRecord, tuple[SimulationRecord, SimulationRecord]],
    *,
    year: int,
    budget_window_run_id: UUID | None,
) -> ReportRunRecord:
    """Start both simulations' runs for ``year`` and join them in a report run.

    ``window`` is a report and its two simulations, as ``budget_window_report``
    returns them.
    """
    report, simulations = window
    store = run_store(database)
    baseline_run, reform_run = (
        store.start_simulation_run(economy_run(simulation, rows, year=year)).run
        for simulation in simulations
    )
    return store.create_report_run(
        NewReportRun(
            report_id=report.id,
            baseline_simulation_run_id=baseline_run.id,
            reform_simulation_run_id=reform_run.id,
            trigger=RunTrigger.INITIAL,
            budget_window_run_id=budget_window_run_id,
        )
    )


def write_year_budget(database, report_run: ReportRunRecord, scale: float) -> None:
    """Store a year's budget figures, as a recorded comparison would."""
    engine = create_simulation_engine(database.runtime_url)
    try:
        with engine.begin() as connection:
            connection.execute(
                budget_impacts.insert().values(
                    report_run_id=report_run.id,
                    budgetary_impact=-1.5 * scale,
                    tax_revenue_impact=2.25 * scale,
                    state_tax_revenue_impact=0.25 * scale,
                    benefit_spending_impact=3.75 * scale,
                    households=100.0,
                    baseline_net_income=1000.0,
                )
            )
    finally:
        engine.dispose()
