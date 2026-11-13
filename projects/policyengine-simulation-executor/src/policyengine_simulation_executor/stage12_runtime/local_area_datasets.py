"""The local-area dataset a UK-wide or nation simulation may be calculated on.

A UK local-area report run calculates its UK-wide or nation region on the
dataset the release routes constituency and local-authority regions to
(``routed_uk_area_dataset_name``), not on the dataset the region loads
otherwise, so its area results come only from that dataset's own weights. It
does so only on a release that routes them (``uk_area_regions_routed``),
whose UK-wide and nation runs leave the area results out: the rule Stage 12's
national aggregation uses (``uk_area_breakdowns_omitted``), and the one the v2
manifest's ``LocalAreaReports`` declares to the simulation service. The
service names that dataset in each simulation's provenance, as it names any
dataset. The UK worker accepts it, selects it in place of the region's own
dataset, keeps the region's scoping (a nation's filter), and checks the
columns the area outputs read as it loads.
"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

from policyengine_simulation_contract.stage12_bundle import region_type_of

from policyengine_simulation_executor.stage12_bundle import load_stage12_bundle
from policyengine_simulation_executor.uk_local_authority_metadata import (
    routed_uk_area_dataset_name,
    uk_area_regions_routed,
)

if TYPE_CHECKING:
    from policyengine_simulation_contract.stage12_bundle import (
        Stage12CountryBundle,
        Stage12Dataset,
    )
    from policyengine_simulation_contract.stage12_execution import (
        ReportExecutionInput,
        SimulationExecutionInput,
    )

    from policyengine_simulation_executor.region_resolution import RegionResolution

# The region types a local-area report run covers: the UK and its nations.
LOCAL_AREA_RUN_REGION_TYPES = frozenset({"national", "country"})


def calculates_on_local_area_dataset(simulation: SimulationExecutionInput) -> bool:
    """Whether ``simulation`` is of the UK or a nation, on the local-area dataset.

    That is the dataset the release routes UK area regions to, on a release
    that routes them to one.
    """
    if simulation.geography.country != "uk":
        return False
    region_type = region_type_of("uk", simulation.geography.region)
    if region_type not in LOCAL_AREA_RUN_REGION_TYPES or not uk_area_regions_routed():
        return False
    return simulation.bundle.dataset.identity == routed_uk_area_dataset_name()


def is_local_area_report(report: ReportExecutionInput) -> bool:
    """Whether a run of ``report`` also calculates the UK area results.

    It does for a report of the UK or a nation whose simulations are on the
    local-area dataset, an ordinary report whose simulations the simulation
    service names on that dataset: it returns its full results and its
    constituency and local-authority results, with their withheld codes.
    Both of a report's simulations share its dataset and region.
    """
    return calculates_on_local_area_dataset(report.baseline)


def named_local_area_dataset(
    country: Stage12CountryBundle, simulation: SimulationExecutionInput
) -> Stage12Dataset | None:
    """Return the local-area dataset ``simulation`` names, or None.

    It is the UK's local-area dataset when the release routes UK area regions
    to it, the simulation is of the UK or one of its nations, and its
    provenance names that dataset. The caller still checks the provenance's
    URI and revision against it.
    """
    if country.country != "uk" or not calculates_on_local_area_dataset(simulation):
        return None
    identity = simulation.bundle.dataset.identity
    return next((item for item in country.datasets if item.identity == identity), None)


def local_area_dataset_name(simulation: SimulationExecutionInput) -> str | None:
    """Return the local-area dataset ``simulation`` names in the installed bundle."""
    country = next(
        (
            item
            for item in load_stage12_bundle().bundle.countries
            if item.country == simulation.geography.country
        ),
        None,
    )
    if country is None:
        return None
    dataset = named_local_area_dataset(country, simulation)
    return None if dataset is None else dataset.identity


def dataset_region(
    region: RegionResolution, local_area_dataset: str | None
) -> RegionResolution:
    """Return the resolution the simulation's dataset is selected by.

    A local-area run selects the local-area dataset; every other run selects
    the dataset its region names. Only the dataset differs: the simulation
    is still scoped by ``region`` itself.
    """
    if local_area_dataset is None:
        return region
    return replace(region, dataset_reference=local_area_dataset)
