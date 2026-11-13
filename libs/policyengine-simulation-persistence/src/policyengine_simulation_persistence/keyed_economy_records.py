"""Find an economy report or simulation by key, with its run, or create them.

Identical requests, together or apart, end on the same simulation, report
and run rows (C1 · 24), and only the request that inserted a run starts its
Modal call. A report's create runs, in one transaction:

1. each side's simulation by its key (``definition_keys``), in key order,
   so two transactions never wait for each other in opposite orders; two
   sides with one key name one simulation;
2. the report by its key;
3. the report's run on the current versions that hasn't failed and wasn't
   forced, pending, running or succeeded: when there is one, the request
   joins it, with the simulation runs it names, and inserts nothing more;
4. otherwise, inside a savepoint, each distinct simulation's run on those
   versions, catalog version and year that hasn't failed and wasn't forced,
   found or inserted in simulation ID order, then the report run naming
   them. A report run another request inserted after step 3 is joined, and
   the savepoint takes back this request's simulation runs.

Every simulation run counts whatever report first calculated it, pending,
running or succeeded: its stored plan no longer depends on the report
(answer 4), and a coordinator waits for a run another report run is
calculating (answer 5). Each lookup is ``shared_rows``'. A standalone
simulation is found or created by its key, with its run, the same way.
Rows keep random IDs (answer 1c): the IDs a request proposes are used only
for the rows it inserts.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final, Literal

from policyengine_simulation_persistence.definition_keys import KEY_VERSION
from policyengine_simulation_persistence.economy_run_records import (
    ECONOMY_REPORT_TYPE,
    references_checked,
    simulation_run_row,
)
from policyengine_simulation_persistence.shared_rows import (
    FoundReportRun,
    FoundSimulationRun,
    find_joinable_report_run,
    find_or_insert_report,
    find_or_insert_report_run,
    find_or_insert_simulation,
    find_or_insert_simulation_run,
    find_or_insert_simulation_runs,
)
from policyengine_simulation_persistence.tables import (
    ReportRunStatus,
    ReportRunTrigger,
    SimulationType,
)

if TYPE_CHECKING:
    from uuid import UUID

    from sqlalchemy import Connection

    from policyengine_simulation_persistence.definition_keys import (
        EconomyReportDefinition,
        EconomySimulationDefinition,
    )
    from policyengine_simulation_persistence.economy_run_records import (
        EconomyPopulation,
        RunVersions,
    )

# A report's two sides, the baseline first.
type Side = Literal["baseline", "reform"]
_SIDES: Final[tuple[Side, Side]] = ("baseline", "reform")


@dataclass(frozen=True)
class ProposedSide:
    """The IDs a side's simulation and run get if this request inserts them."""

    simulation_id: UUID
    simulation_run_id: UUID


@dataclass(frozen=True)
class KeyedEconomyReport:
    """An economy report a request names, found by key or created with its run.

    ``population`` is what its simulations' runs calculate on now: the
    current catalog version's rows with the definition's dataset name and
    region code. ``inputs`` is what ``reports.inputs`` stores: the SPM
    selection when the request gave one, and no other option (answer 4).
    ``label`` and ``description`` are stored only by the request that
    creates the report.
    """

    definition: EconomyReportDefinition
    report_id: UUID
    run_id: UUID
    baseline: ProposedSide
    reform: ProposedSide
    label: str
    description: str | None
    tax_benefit_model_id: UUID
    population: EconomyPopulation
    versions: RunVersions
    inputs: dict[str, Any]


@dataclass(frozen=True)
class FoundSide:
    """A report run's side: its simulation and run, and who inserted the run."""

    simulation_id: UUID
    simulation_run_id: UUID
    run_inserted: bool


@dataclass(frozen=True)
class FoundEconomyReport:
    """The report and run a request ended on, and what it inserted itself."""

    report_id: UUID
    report_inserted: bool
    run: FoundReportRun
    baseline: FoundSide
    reform: FoundSide

    @property
    def inserted_simulation_run_ids(self) -> tuple[UUID, ...]:
        """Return the simulation runs this request inserted, each once."""
        inserted = [
            side.simulation_run_id
            for side in (self.baseline, self.reform)
            if side.run_inserted
        ]
        return tuple(dict.fromkeys(inserted))


@dataclass(frozen=True)
class KeyedEconomySimulation:
    """A standalone economy simulation, found by key or created with its run."""

    definition: EconomySimulationDefinition
    simulation_id: UUID
    run_id: UUID
    population: EconomyPopulation
    versions: RunVersions


@dataclass(frozen=True)
class FoundEconomySimulation:
    """The simulation and run a request ended on, and what it inserted."""

    simulation_id: UUID
    simulation_inserted: bool
    run: FoundSimulationRun


def find_or_create_economy_report(
    connection: Connection, request: KeyedEconomyReport
) -> FoundEconomyReport:
    """Find the report the request names, and its run, or create them.

    Raise EconomyRecordReferenceError when the database refuses a row an
    insert names, and SharedRowNotSettledError when a run keeps failing
    between a lookup's insert and its read; either ends the caller's
    transaction, or its savepoint.
    """
    with references_checked():
        simulation_ids = _find_or_insert_simulations(connection, request)
        report = find_or_insert_report(connection, _report_row(request, simulation_ids))
        versions = request.versions
        joined = find_joinable_report_run(
            connection,
            report_id=report.id,
            country_package_version=versions.country_package_version,
            policyengine_version=versions.policyengine_version,
        )
        if joined is not None:
            return _found(report.id, report.inserted, joined, simulation_ids, set())
        # A report run another request inserts after the lookup is joined,
        # and the savepoint takes back this request's simulation runs.
        savepoint = connection.begin_nested()
        try:
            runs = _find_or_insert_simulation_runs(connection, request, simulation_ids)
            side_runs: dict[Side, UUID] = {
                side: runs[simulation_ids[side]].id for side in _SIDES
            }
            run = find_or_insert_report_run(
                connection, _report_run_row(request, report.id, side_runs)
            )
        except BaseException:
            savepoint.rollback()
            raise
        if not run.inserted:
            savepoint.rollback()
            return _found(report.id, report.inserted, run, simulation_ids, set())
        savepoint.commit()
        inserted = {found.id for found in runs.values() if found.inserted}
        return _found(report.id, report.inserted, run, simulation_ids, inserted)


def find_or_create_economy_simulation(
    connection: Connection, request: KeyedEconomySimulation
) -> FoundEconomySimulation:
    """Find the simulation the request names, and its run, or create them.

    Raise as ``find_or_create_economy_report`` does.
    """
    with references_checked():
        simulation = find_or_insert_simulation(
            connection,
            _simulation_row(
                request.definition, request.simulation_id, request.population
            ),
        )
        run = find_or_insert_simulation_run(
            connection,
            simulation_run_row(
                run_id=request.run_id,
                simulation_id=simulation.id,
                population=request.population,
                versions=request.versions,
            ),
        )
    return FoundEconomySimulation(
        simulation_id=simulation.id,
        simulation_inserted=simulation.inserted,
        run=run,
    )


def _find_or_insert_simulations(
    connection: Connection, request: KeyedEconomyReport
) -> dict[Side, UUID]:
    """Return each side's simulation ID, found by key or inserted, in key order."""
    proposed = {"baseline": request.baseline, "reform": request.reform}
    by_key: dict[str, list[Side]] = {}
    for side in _SIDES:
        key = request.definition.simulation(side).definition_key
        by_key.setdefault(key, []).append(side)
    simulation_ids: dict[Side, UUID] = {}
    for key in sorted(by_key):
        sides = by_key[key]
        found = find_or_insert_simulation(
            connection,
            _simulation_row(
                request.definition.simulation(sides[0]),
                proposed[sides[0]].simulation_id,
                request.population,
            ),
        )
        for side in sides:
            simulation_ids[side] = found.id
    return simulation_ids


def _find_or_insert_simulation_runs(
    connection: Connection,
    request: KeyedEconomyReport,
    simulation_ids: dict[Side, UUID],
) -> dict[UUID, FoundSimulationRun]:
    """Return each distinct simulation's run, found or inserted, by simulation ID.

    Inserted in simulation ID order; one simulation on both sides gets one
    run, with the baseline's proposed ID.
    """
    proposed_sides = {"baseline": request.baseline, "reform": request.reform}
    rows = {
        simulation_ids[side]: simulation_run_row(
            run_id=proposed_sides[side].simulation_run_id,
            simulation_id=simulation_ids[side],
            population=request.population,
            versions=request.versions,
        )
        # The reform first, so one simulation on both sides keeps the
        # baseline's proposed ID.
        for side in reversed(_SIDES)
    }
    return find_or_insert_simulation_runs(connection, rows)


def _simulation_row(
    definition: EconomySimulationDefinition,
    simulation_id: UUID,
    population: EconomyPopulation,
) -> dict[str, Any]:
    """Return a keyed economy simulation as inserted, on ``population``'s rows."""
    return {
        "id": simulation_id,
        "simulation_type": SimulationType.ECONOMY,
        "dataset_id": population.dataset_id,
        "household_id": None,
        "policy_id": definition.choice.policy_id,
        "dynamic_id": definition.choice.dynamic_id,
        "tax_benefit_model_version_id": population.tax_benefit_model_version_id,
        "region_id": population.region_id,
        "year": definition.year,
        "key_version": KEY_VERSION,
        "definition_key": definition.definition_key,
    }


def _report_row(
    request: KeyedEconomyReport, simulation_ids: dict[Side, UUID]
) -> dict[str, Any]:
    """Return a keyed economy report as inserted, naming its sides' simulations."""
    definition = request.definition
    population = request.population
    return {
        "id": request.report_id,
        "label": request.label,
        "description": request.description,
        "country": definition.country,
        "type": ECONOMY_REPORT_TYPE,
        "user_id": None,
        "tax_benefit_model_id": request.tax_benefit_model_id,
        "policy_id": definition.reform.policy_id,
        "baseline_simulation_id": simulation_ids["baseline"],
        "reform_simulation_id": simulation_ids["reform"],
        "household_id": None,
        "dataset_id": population.dataset_id,
        "region_id": population.region_id,
        "year": definition.year,
        "budget_window_size": definition.budget_window_size,
        "inputs": request.inputs,
        "key_version": KEY_VERSION,
        "definition_key": definition.definition_key,
        "dataset_independent_key": definition.dataset_independent_key,
    }


def _report_run_row(
    request: KeyedEconomyReport,
    report_id: UUID,
    side_runs: dict[Side, UUID],
) -> dict[str, Any]:
    """Return the report's first run on these versions, naming its sides' runs."""
    return {
        "id": request.run_id,
        "report_id": report_id,
        "baseline_simulation_run_id": side_runs["baseline"],
        "reform_simulation_run_id": side_runs["reform"],
        "budget_window_run_id": None,
        "year": None,
        "country_package_version": request.versions.country_package_version,
        "policyengine_version": request.versions.policyengine_version,
        "status": ReportRunStatus.PENDING,
        "trigger": ReportRunTrigger.INITIAL,
        "modal_call_id": None,
    }


def _found(
    report_id: UUID,
    report_inserted: bool,  # noqa: FBT001  # the lookup's own flag, passed on
    run: FoundReportRun,
    simulation_ids: dict[Side, UUID],
    inserted_run_ids: set[UUID],
) -> FoundEconomyReport:
    """Return what the request ended on, with each side's simulation and run."""
    reform_run_id = run.reform_simulation_run_id
    if reform_run_id is None:
        msg = f"report run {run.id} of an economy report names no reform run"
        raise ValueError(msg)
    run_ids = {"baseline": run.baseline_simulation_run_id, "reform": reform_run_id}
    sides = {
        side: FoundSide(
            simulation_id=simulation_ids[side],
            simulation_run_id=run_ids[side],
            run_inserted=run_ids[side] in inserted_run_ids,
        )
        for side in _SIDES
    }
    return FoundEconomyReport(
        report_id=report_id,
        report_inserted=report_inserted,
        run=run,
        baseline=sides["baseline"],
        reform=sides["reform"],
    )
