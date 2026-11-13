"""Find a household report by key, with its run, or create them (answer 8).

Identical household report requests, together or apart, end on the same
simulation, report and run rows, as economy ones do
(``keyed_economy_records``), and only the request that inserted a report
run starts its household function call. A report's create runs, in one
transaction:

1. each side's household simulation by its key (``definition_keys``), in
   key order; two sides with one key name one simulation;
2. the report by its key;
3. the report's run on the current versions that hasn't failed and wasn't
   forced, pending, running or succeeded: when there is one, the request
   joins it, with the simulation runs it names, and inserts nothing more;
4. otherwise, inside a savepoint, each distinct simulation's run on those
   versions, catalog version and year that hasn't failed and wasn't forced,
   found or inserted, then the report run naming them
   (``find_or_insert_report_run_on_sides``). A report run another request
   inserted after step 3 is joined, and the savepoint takes back this
   request's simulation runs.

A household simulation run may be another report run's, pending, running
or succeeded: the household function waits for a run another call holds
(C1 · 26). A report of the baseline alone has no reform side. Rows keep
random IDs: the IDs a request proposes are used only for the rows it
inserts.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from policyengine_simulation_persistence.definition_keys import KEY_VERSION
from policyengine_simulation_persistence.economy_run_records import (
    references_checked,
)
from policyengine_simulation_persistence.household_report_records import (
    HOUSEHOLD_REPORT_TYPE,
    household_simulation_run_row,
)
from policyengine_simulation_persistence.keyed_economy_records import (
    FoundSide,
    ProposedSide,
    Side,
    find_or_insert_report_run_on_sides,
)
from policyengine_simulation_persistence.shared_rows import (
    FoundReportRun,
    find_joinable_report_run,
    find_or_insert_report,
    find_or_insert_simulation,
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
        HouseholdReportDefinition,
    )
    from policyengine_simulation_persistence.economy_run_records import RunVersions


@dataclass(frozen=True)
class KeyedHouseholdReport:
    """A household report a request names, found by key or created with its run.

    ``reform`` is None exactly when the definition has no reform (a report
    of the baseline alone). The runs calculate on the catalog version
    ``tax_benefit_model_version_id``. ``label`` and ``description`` are
    stored only by the request that creates the report.
    """

    definition: HouseholdReportDefinition
    report_id: UUID
    run_id: UUID
    baseline: ProposedSide
    reform: ProposedSide | None
    label: str
    description: str | None
    tax_benefit_model_id: UUID
    tax_benefit_model_version_id: UUID
    versions: RunVersions

    def __post_init__(self) -> None:
        """Refuse a reform side for a report without a reform, or none for one."""
        if (self.reform is None) != (self.definition.reform is None):
            msg = "a household report proposes a reform side exactly when it has one"
            raise ValueError(msg)

    @property
    def proposed_sides(self) -> dict[Side, ProposedSide]:
        """Return each side the report calculates and the IDs it proposes."""
        sides: dict[Side, ProposedSide] = {"baseline": self.baseline}
        if self.reform is not None:
            sides["reform"] = self.reform
        return sides


@dataclass(frozen=True)
class FoundHouseholdReport:
    """The household report and run a request ended on, and what it inserted.

    ``reform`` is None for a report of the baseline alone.
    """

    report_id: UUID
    report_inserted: bool
    run: FoundReportRun
    baseline: FoundSide
    reform: FoundSide | None


def find_or_create_household_report(
    connection: Connection, request: KeyedHouseholdReport
) -> FoundHouseholdReport:
    """Find the household report the request names, and its run, or create them.

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
        run, runs = find_or_insert_report_run_on_sides(
            connection,
            _report_run_row(request, report.id),
            {
                side: household_simulation_run_row(
                    run_id=proposed.simulation_run_id,
                    simulation_id=simulation_ids[side],
                    year=request.definition.year,
                    tax_benefit_model_version_id=request.tax_benefit_model_version_id,
                    versions=versions,
                )
                for side, proposed in request.proposed_sides.items()
            },
        )
        inserted = {found.id for found in runs.values() if found.inserted}
        return _found(report.id, report.inserted, run, simulation_ids, inserted)


def _find_or_insert_simulations(
    connection: Connection, request: KeyedHouseholdReport
) -> dict[Side, UUID]:
    """Return each side's simulation ID, found by key or inserted, in key order."""
    definition = request.definition
    proposed = request.proposed_sides
    by_key: dict[str, list[Side]] = {}
    for side in proposed:
        by_key.setdefault(definition.simulation(side).definition_key, []).append(side)
    simulation_ids: dict[Side, UUID] = {}
    for key in sorted(by_key):
        sides = by_key[key]
        found = find_or_insert_simulation(
            connection,
            _simulation_row(request, sides[0], proposed[sides[0]].simulation_id),
        )
        for side in sides:
            simulation_ids[side] = found.id
    return simulation_ids


def _simulation_row(
    request: KeyedHouseholdReport, side: Side, simulation_id: UUID
) -> dict[str, Any]:
    """Return a side's keyed household simulation as inserted."""
    simulation = request.definition.simulation(side)
    return {
        "id": simulation_id,
        "simulation_type": SimulationType.HOUSEHOLD,
        "dataset_id": None,
        "household_id": simulation.household_id,
        "policy_id": simulation.choice.policy_id,
        "dynamic_id": simulation.choice.dynamic_id,
        "tax_benefit_model_version_id": request.tax_benefit_model_version_id,
        "region_id": None,
        "year": simulation.year,
        "key_version": KEY_VERSION,
        "definition_key": simulation.definition_key,
    }


def _report_row(
    request: KeyedHouseholdReport, simulation_ids: dict[Side, UUID]
) -> dict[str, Any]:
    """Return a keyed household report as inserted, naming its sides' simulations."""
    definition = request.definition
    reform = definition.reform
    return {
        "id": request.report_id,
        "label": request.label,
        "description": request.description,
        "country": definition.country,
        "type": HOUSEHOLD_REPORT_TYPE,
        "user_id": None,
        "tax_benefit_model_id": request.tax_benefit_model_id,
        "policy_id": None if reform is None else reform.policy_id,
        "baseline_simulation_id": simulation_ids["baseline"],
        "reform_simulation_id": simulation_ids.get("reform"),
        "household_id": definition.household_id,
        "dataset_id": None,
        "region_id": None,
        "year": definition.year,
        "budget_window_size": None,
        # A household report has no options; the column holds an object.
        "inputs": {},
        "key_version": KEY_VERSION,
        "definition_key": definition.definition_key,
        "dataset_independent_key": definition.dataset_independent_key,
    }


def _report_run_row(request: KeyedHouseholdReport, report_id: UUID) -> dict[str, Any]:
    """Return the report's first run on these versions, without its sides' runs."""
    return {
        "id": request.run_id,
        "report_id": report_id,
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
) -> FoundHouseholdReport:
    """Return what the request ended on, with each side's simulation and run."""
    run_ids: dict[Side, UUID | None] = {
        "baseline": run.baseline_simulation_run_id,
        "reform": run.reform_simulation_run_id,
    }
    sides: dict[Side, FoundSide] = {}
    for side, simulation_id in simulation_ids.items():
        run_id = run_ids[side]
        if run_id is None:
            msg = f"report run {run.id} of a household report names no {side} run"
            raise ValueError(msg)
        sides[side] = FoundSide(
            simulation_id=simulation_id,
            simulation_run_id=run_id,
            run_inserted=run_id in inserted_run_ids,
        )
    return FoundHouseholdReport(
        report_id=report_id,
        report_inserted=report_inserted,
        run=run,
        baseline=sides["baseline"],
        reform=sides.get("reform"),
    )
