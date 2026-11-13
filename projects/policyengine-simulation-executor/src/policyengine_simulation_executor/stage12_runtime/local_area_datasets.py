"""The local-area dataset a UK-wide or nation simulation may be calculated on.

A UK local-area report run calculates its UK-wide or nation region on the
dataset the bundle gives constituency and local-authority regions
(``Stage12CountryBundle.local_area_dataset``), not on the dataset the region
loads otherwise, so its area results come only from that dataset's own
weights. The simulation service names that dataset in each simulation's
provenance, as it names any dataset. The UK worker accepts it, selects it in
place of the region's own dataset, keeps the region's scoping (a nation's
filter), and checks the columns the area outputs read as it loads.
"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

from policyengine_simulation_contract.stage12_bundle import region_type_of

from policyengine_simulation_executor.stage12_bundle import load_stage12_bundle

if TYPE_CHECKING:
    from policyengine_simulation_contract.stage12_bundle import (
        Stage12CountryBundle,
        Stage12Dataset,
    )
    from policyengine_simulation_contract.stage12_execution import (
        SimulationExecutionInput,
    )

    from policyengine_simulation_executor.region_resolution import RegionResolution

# The region types a local-area report run covers: the UK and its nations.
LOCAL_AREA_RUN_REGION_TYPES = frozenset({"national", "country"})


def named_local_area_dataset(
    country: Stage12CountryBundle, simulation: SimulationExecutionInput
) -> Stage12Dataset | None:
    """Return the local-area dataset ``simulation`` names, or None.

    It is the country's local-area dataset when the simulation is of the
    country or one of its nations and its provenance names that dataset.
    The caller still checks the provenance's URI and revision against it.
    """
    identity = country.local_area_dataset()
    if identity is None or simulation.bundle.dataset.identity != identity:
        return None
    region_type = region_type_of(country.country, simulation.geography.region)
    if region_type not in LOCAL_AREA_RUN_REGION_TYPES:
        return None
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
