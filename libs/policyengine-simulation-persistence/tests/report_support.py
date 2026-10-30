"""Builders for the report tests."""

from __future__ import annotations

from typing import TYPE_CHECKING

from policyengine_simulation_persistence import (
    NewReport,
    ReportKind,
    ReportStore,
    create_simulation_engine,
)
from simulation_support import (
    economy_simulation,
    household_simulation,
    simulation_store,
)

if TYPE_CHECKING:
    from policyengine_simulation_persistence import SimulationRecord
    from public_api_support import PublicApiRows


def report_store(database) -> ReportStore:
    """Return a store that connects as the runtime role."""
    return ReportStore(create_simulation_engine(database.runtime_url))


def economy_pair(
    database, rows: PublicApiRows
) -> tuple[SimulationRecord, SimulationRecord]:
    """Create a current-law baseline and a reform of California's economy."""
    store = simulation_store(database)
    baseline = store.create(economy_simulation(rows, policy_id=None))
    reform = store.create(economy_simulation(rows))
    return baseline, reform


def economy_report(
    baseline: SimulationRecord, reform: SimulationRecord, **overrides: object
) -> NewReport:
    """Return a 2026 US economy report comparing the two simulations."""
    values: dict[str, object] = {
        "kind": ReportKind.ECONOMY,
        "country": "us",
        "baseline_simulation_id": baseline.id,
        "reform_simulation_id": reform.id,
        "year": 2026,
    }
    values.update(overrides)
    return NewReport(**values)  # type: ignore[arg-type]


def household_baseline(database, rows: PublicApiRows) -> SimulationRecord:
    """Create a current-law simulation of the US household."""
    return simulation_store(database).create(household_simulation(rows))
