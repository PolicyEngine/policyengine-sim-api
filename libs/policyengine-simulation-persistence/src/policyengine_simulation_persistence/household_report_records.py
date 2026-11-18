"""The simulation service's household report type and household run rows.

A household report calculates one stored household, for one year, under a
baseline policy and, optionally, a reform. Its ``reports`` row has type
``household`` (``HOUSEHOLD_REPORT_TYPE``) and names the household; each side
is a household ``simulations`` row naming the household and the side's
policy. The service finds or creates the report, its simulations and its
first run by definition key (``keyed_household_records``), and re-runs a
stored one (``economy_rerun_records``). Either creates each side's run
``pending`` with no Modal call ID, from ``household_simulation_run_row``;
a forced re-run inserts it with ``insert_household_simulation_run``.

The service then starts one household function call for the report run and
records the call's ID on it, or fails it and its simulation runs at once,
with ``economy_run_records``' report run writes. The household function
writes the rest: each run's ``household_result``, and every status after
``pending``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

from policyengine_simulation_persistence.tables import (
    SimulationRun,
    SimulationStatus,
)

if TYPE_CHECKING:
    from uuid import UUID

    from sqlalchemy.orm import Session

    from policyengine_simulation_persistence.economy_run_records import RunVersions

# A report's type, as API v1 reads it.
HOUSEHOLD_REPORT_TYPE: Final = "household"


def insert_household_simulation_run(  # noqa: PLR0913  # the run and what it calculates on
    session: Session,
    *,
    run_id: UUID,
    simulation_id: UUID,
    year: int,
    tax_benefit_model_version_id: UUID,
    versions: RunVersions,
    forced: bool = False,
) -> None:
    """Create a run of a household simulation, pending with no Modal call ID.

    It records the year, catalog version and versions it calculates on; a
    household run names no dataset or region. A ``forced`` run is an extra
    run that nothing joins. The row is flushed here: no relationship orders
    it before the report run that goes on to name it.
    """
    session.add(
        SimulationRun(
            **household_simulation_run_row(
                run_id=run_id,
                simulation_id=simulation_id,
                year=year,
                tax_benefit_model_version_id=tax_benefit_model_version_id,
                versions=versions,
            ),
            forced=forced,
        )
    )
    session.flush()


def household_simulation_run_row(
    *,
    run_id: UUID,
    simulation_id: UUID,
    year: int,
    tax_benefit_model_version_id: UUID,
    versions: RunVersions,
) -> dict[str, Any]:
    """Return a household simulation's new run, pending with no Modal call ID.

    Unforced: ``insert_household_simulation_run`` adds ``forced`` to it.
    """
    return {
        "id": run_id,
        "simulation_id": simulation_id,
        "year": year,
        "tax_benefit_model_version_id": tax_benefit_model_version_id,
        "country_package_version": versions.country_package_version,
        "policyengine_version": versions.policyengine_version,
        "dataset_id": None,
        "region_id": None,
        "status": SimulationStatus.PENDING,
        "modal_call_id": None,
    }
