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

A household report compares a baseline household simulation with an
optional reform. Each report run joins one run of each, and one Modal call
computes both households; ``read_report`` returns the report with a report
run (its newest, unless another is named) and both runs as calculations.
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
from policyengine_simulation_persistence.reports import (
    ReportKind,
    ReportRecord,
    ReportStore,
    reports,
)
from policyengine_simulation_persistence.run_store import RunStore, utc_now
from policyengine_simulation_persistence.runs import (
    ReportRunRecord,
    RunStatus,
    SimulationRunRecord,
    report_runs,
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

    from sqlalchemy import Connection, Engine

    from policyengine_simulation_persistence.reports import NewReport
    from policyengine_simulation_persistence.runs import (
        NewReportRun,
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


@dataclass(frozen=True)
class HouseholdReport:
    """A household report, one of its runs, and that run's two calculations.

    ``report_run``, ``baseline`` and ``reform`` are None before the report's
    first run; ``reform`` is None for a report with only a baseline.
    """

    report: ReportRecord
    report_run: ReportRunRecord | None
    baseline: HouseholdCalculation | None
    reform: HouseholdCalculation | None


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
        self._reports = ReportStore(engine)
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

    def get_simulation(self, simulation_id: UUID) -> SimulationRecord | None:
        """Return the simulation with this ID, or None."""
        return self._simulations.get(simulation_id)

    def read_calculation(self, run_id: UUID) -> HouseholdCalculation | None:
        """Return the household calculation run ``run_id``, or None.

        An economy run is not a household calculation, so it reads as None.
        """
        with self.engine.connect() as connection:
            return _read_calculation(connection, run_id)

    # --- Household reports --------------------------------------------------

    def create_report(self, new_report: NewReport) -> ReportRecord:
        """Insert one report and return it."""
        return self._reports.create(new_report)

    def get_report(self, report_id: UUID) -> ReportRecord | None:
        """Return the household report with this ID, or None."""
        report = self._reports.get(report_id)
        if report is None or report.kind is not ReportKind.HOUSEHOLD:
            return None
        return report

    def create_report_run(self, new_report_run: NewReportRun) -> ReportRunRecord:
        """Add a report run joining runs of the report's simulations."""
        return self._runs.create_report_run(new_report_run)

    def read_report(
        self, report_id: UUID, *, report_run_id: UUID | None = None
    ) -> HouseholdReport | None:
        """Return a household report with a run and its two calculations.

        The run is ``report_run_id`` when given, and the report's newest run
        otherwise. Return None for an economy report, an unknown report, or a
        run of another report.
        """
        with self.engine.connect() as connection:
            report_row = (
                connection.execute(
                    select(reports).where(
                        reports.c.id == report_id,
                        reports.c.kind == ReportKind.HOUSEHOLD.value,
                    )
                )
                .mappings()
                .first()
            )
            if report_row is None:
                return None
            run_query = select(report_runs).where(report_runs.c.report_id == report_id)
            if report_run_id is not None:
                run_query = run_query.where(report_runs.c.id == report_run_id)
            run_row = (
                connection.execute(
                    run_query.order_by(
                        report_runs.c.created_at.desc(), report_runs.c.id.desc()
                    ).limit(1)
                )
                .mappings()
                .first()
            )
            if run_row is None and report_run_id is not None:
                return None
            report_run = None if run_row is None else ReportRunRecord.from_row(run_row)
            baseline = (
                None
                if report_run is None
                else _read_calculation(
                    connection, report_run.baseline_simulation_run_id
                )
            )
            reform = (
                None
                if report_run is None or report_run.reform_simulation_run_id is None
                else _read_calculation(connection, report_run.reform_simulation_run_id)
            )
        return HouseholdReport(
            report=ReportRecord.from_row(report_row),
            report_run=report_run,
            baseline=baseline,
            reform=reform,
        )


def _read_calculation(
    connection: Connection, run_id: UUID
) -> HouseholdCalculation | None:
    row = (
        connection.execute(
            select(simulation_runs).where(
                simulation_runs.c.id == run_id,
                simulation_runs.c.simulation_kind == SimulationKind.HOUSEHOLD.value,
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
