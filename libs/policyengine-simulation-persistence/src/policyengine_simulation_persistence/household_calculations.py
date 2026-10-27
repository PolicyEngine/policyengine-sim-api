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

``read_calculation`` returns a calculation as its reader sees it: the run's
status, timings and error (with its detail), its simulation, and, once it
succeeded, the computed household.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy import cast, select
from sqlalchemy.dialects.postgresql import JSONB

from policyengine_simulation_persistence.household_results import (
    HouseholdOutput,
    read_household_output,
)
from policyengine_simulation_persistence.run_store import RunStore, utc_now
from policyengine_simulation_persistence.runs import (
    RunStatus,
    SimulationRunRecord,
    simulation_runs,
)
from policyengine_simulation_persistence.simulations import (
    SimulationKind,
    SimulationRecord,
    SimulationStore,
    simulations,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import datetime
    from uuid import UUID

    from sqlalchemy import Engine

    from policyengine_simulation_persistence.runs import (
        NewSimulationRun,
        StartedSimulationRun,
    )
    from policyengine_simulation_persistence.simulations import NewSimulation


@dataclass(frozen=True)
class HouseholdCalculation:
    """A household calculation: its run, its simulation, and its household.

    ``output`` is set once the run succeeded.
    """

    run: SimulationRunRecord
    simulation: SimulationRecord
    output: HouseholdOutput | None


class HouseholdCalculationStore:
    """Typed SQLAlchemy Core reads and inserts for household calculations."""

    def __init__(
        self,
        engine: Engine,
        *,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        """Use ``engine`` (the runtime role) and ``clock`` for run timestamps."""
        self.engine = engine
        self._simulations = SimulationStore(engine)
        self._runs = RunStore(engine, clock=clock)

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

    def start_run(
        self, new_run: NewSimulationRun, *, force: bool = False
    ) -> StartedSimulationRun:
        """Return the reusable run for this setup, or create the next attempt.

        As ``RunStore.start_simulation_run``: an unfinished run, or one that
        succeeded within the reuse window, is reused; a failed one never is.
        """
        return self._runs.start_simulation_run(new_run, force=force)

    def read_calculation(self, run_id: UUID) -> HouseholdCalculation | None:
        """Return the household calculation run ``run_id``, or None.

        An economy run is not a household calculation, so it reads as None.
        """
        with self.engine.connect() as connection:
            row = (
                connection.execute(
                    select(simulation_runs).where(
                        simulation_runs.c.id == run_id,
                        simulation_runs.c.simulation_kind
                        == SimulationKind.HOUSEHOLD.value,
                    )
                )
                .mappings()
                .first()
            )
            if row is None:
                return None
            run = SimulationRunRecord.from_row(row)
            simulation_row = (
                connection.execute(
                    select(simulations).where(simulations.c.id == run.simulation_id)
                )
                .mappings()
                .one()
            )
            output = (
                read_household_output(connection, run.id)
                if run.status is RunStatus.SUCCEEDED
                else None
            )
        return HouseholdCalculation(
            run=run,
            simulation=SimulationRecord.from_row(simulation_row),
            output=output,
        )
