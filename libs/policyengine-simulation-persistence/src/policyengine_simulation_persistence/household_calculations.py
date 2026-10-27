"""Household calculations: household simulations found by what they calculate.

A household calculation is one simulation run of a household simulation: one
household under one policy (null for current law), with an optional dynamic
and the options that change what it calculates, such as its axes. The Public
API's households and policies never change once stored, so those keys identify
the calculation's inputs, and a calculation submitted again finds the same
simulation and, through ``RunStore``, reuses its run.

Two submissions racing may each create the simulation; both rows describe the
same calculation, and later submissions find the older one. That costs one
calculation and no wrong result, so no unique constraint is needed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import cast, select
from sqlalchemy.dialects.postgresql import JSONB

from policyengine_simulation_persistence.simulations import (
    SimulationKind,
    SimulationRecord,
    SimulationStore,
    simulations,
)

if TYPE_CHECKING:
    from sqlalchemy import Engine

    from policyengine_simulation_persistence.simulations import NewSimulation


class HouseholdCalculationStore:
    """Typed SQLAlchemy Core reads and inserts for household calculations."""

    def __init__(self, engine: Engine) -> None:
        """Use ``engine``, which connects as the runtime role."""
        self.engine = engine
        self._simulations = SimulationStore(engine)

    def find_or_create_simulation(
        self, new_simulation: NewSimulation
    ) -> SimulationRecord:
        """Return the oldest household simulation with these inputs, or create it.

        It matches the country, household, policy, dynamic and options; options
        compare by value, whatever their key order. Raise ValueError for an
        economy simulation.
        """
        if new_simulation.kind is not SimulationKind.HOUSEHOLD:
            msg = "only a household simulation is found by its household"
            raise ValueError(msg)
        with self.engine.connect() as connection:
            row = (
                connection.execute(
                    select(simulations)
                    .where(
                        simulations.c.kind == SimulationKind.HOUSEHOLD.value,
                        simulations.c.country == new_simulation.country,
                        simulations.c.household_id == new_simulation.household_id,
                        simulations.c.policy_id.is_not_distinct_from(
                            new_simulation.policy_id
                        ),
                        simulations.c.dynamic_id.is_not_distinct_from(
                            new_simulation.dynamic_id
                        ),
                        simulations.c.options == cast(new_simulation.options, JSONB),
                    )
                    .order_by(simulations.c.created_at, simulations.c.id)
                    .limit(1)
                )
                .mappings()
                .first()
            )
        if row is not None:
            return SimulationRecord.from_row(row)
        return self._simulations.create(new_simulation)
