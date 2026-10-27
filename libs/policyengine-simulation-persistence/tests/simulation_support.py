"""Builders shared by the simulation and report tests."""

from __future__ import annotations

from typing import TYPE_CHECKING

from policyengine_simulation_persistence import (
    NewSimulation,
    SimulationKind,
    SimulationStore,
    create_simulation_engine,
)

if TYPE_CHECKING:
    from public_api_support import PublicApiRows


def economy_simulation(rows: PublicApiRows, **overrides: object) -> NewSimulation:
    """Return a California economy simulation of the US reform."""
    values: dict[str, object] = {
        "kind": SimulationKind.ECONOMY,
        "country": "us",
        "policy_id": rows.us_policy_id,
        "region_code": "state/ca",
    }
    values.update(overrides)
    return NewSimulation(**values)  # type: ignore[arg-type]


def household_simulation(rows: PublicApiRows, **overrides: object) -> NewSimulation:
    """Return a current-law simulation of the US household."""
    values: dict[str, object] = {
        "kind": SimulationKind.HOUSEHOLD,
        "country": "us",
        "household_id": rows.us_household_id,
    }
    values.update(overrides)
    return NewSimulation(**values)  # type: ignore[arg-type]


def simulation_store(database) -> SimulationStore:
    """Return a store that connects as the runtime role."""
    return SimulationStore(create_simulation_engine(database.runtime_url))
