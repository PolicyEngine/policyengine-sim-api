"""Resolve a v2 economy run's region and dataset from its catalog population.

A v2 economy simulation run carries the region and dataset its catalog
version has (``CatalogPopulation``) in its options, under
``CATALOG_POPULATION_OPTION``. Its calculation then scopes the dataset with
the catalog's household filter and loads the dataset the catalog names,
instead of looking the region up in policyengine.py's region registry. A US
national run calculated in segments scopes each segment to its states'
catalog filters. Stage 12 comparison runs carry no catalog population and
resolve their regions as before.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from policyengine_simulation_contract.economy_runs import CatalogPopulation

from .partition import stage12_region_groups

if TYPE_CHECKING:
    from policyengine.core.scoping_strategy import RowFilterStrategy
    from policyengine_simulation_contract.economy_runs import CatalogRowFilter
    from policyengine_simulation_contract.stage12_bundle import (
        Stage12CountryBundle,
        Stage12Dataset,
    )
    from policyengine_simulation_contract.stage12_execution import (
        SimulationExecutionInput,
    )

    from policyengine_simulation_executor.simulation_runtime import (
        DatasetSelection,
        RegionResolution,
    )

CATALOG_POPULATION_OPTION = "catalog_population"
REGION_GROUP_OPTION = "region_group"


def catalog_population(
    simulation: SimulationExecutionInput,
) -> CatalogPopulation | None:
    """Return the simulation's catalog population, or None for a Stage 12 run."""
    payload = simulation.options.get(CATALOG_POPULATION_OPTION)
    if payload is None:
        return None
    return CatalogPopulation.model_validate(payload)


def _row_filter(row_filter: CatalogRowFilter) -> RowFilterStrategy:
    from policyengine.core.scoping_strategy import (  # noqa: PLC0415  # policyengine loads in the worker, not where the contract is imported
        RowFilterStrategy,
    )

    value = row_filter.household_value()
    if isinstance(value, list):
        values: list[str | int | float] = list(value)
        return RowFilterStrategy(variable_name=row_filter.field, variable_value=values)
    return RowFilterStrategy(variable_name=row_filter.field, variable_value=value)


def resolve_catalog_region(
    simulation: SimulationExecutionInput, population: CatalogPopulation
) -> RegionResolution:
    """Return the region the catalog population describes, as the worker scopes it.

    A segment of a national run (``region_group`` in its options) is scoped to
    the union of its states' catalog filters; raise ValueError when the
    population has no filter for one of them.
    """
    from policyengine.core.scoping_strategy import (  # noqa: PLC0415  # policyengine loads in the worker, not where the contract is imported
        RegionGroupStrategy,
    )

    from policyengine_simulation_executor.simulation_runtime import (  # noqa: PLC0415  # the region type the Stage 12 calculation consumes
        RegionResolution,
    )

    group = simulation.options.get(REGION_GROUP_OPTION)
    if isinstance(group, list) and group:
        codes = [str(code) for code in group]
        filters = population.state_filters or {}
        missing = [code for code in codes if code not in filters]
        if missing:
            msg = f"the catalog population has no filter for {', '.join(missing)}"
            raise ValueError(msg)
        return RegionResolution(
            code="region_group/" + "+".join(sorted(codes)),
            scoping_strategy=RegionGroupStrategy(
                members=[_row_filter(filters[code]) for code in codes]
            ),
        )
    return RegionResolution(
        code=population.region_code,
        scoping_strategy=(
            None
            if population.row_filter is None
            else _row_filter(population.row_filter)
        ),
        state_code=population.state_code,
    )


def catalog_dataset(
    country_bundle: Stage12CountryBundle, population: CatalogPopulation
) -> Stage12Dataset:
    """Return the installed release's dataset the catalog population names.

    Raise ValueError when the release has no dataset of that name.
    """
    dataset = next(
        (
            item
            for item in country_bundle.datasets
            if item.identity == population.dataset_name
        ),
        None,
    )
    if dataset is None:
        msg = (
            f"the installed release has no dataset {population.dataset_name!r}, "
            "which the catalog names"
        )
        raise ValueError(msg)
    return dataset


def resolve_catalog_dataset(
    country_bundle: Stage12CountryBundle, population: CatalogPopulation
) -> DatasetSelection:
    """Return the dataset selection the calculation loads for the population."""
    from policyengine_simulation_executor.simulation_runtime import (  # noqa: PLC0415  # the selection type the Stage 12 calculation consumes
        DatasetSelection,
    )

    dataset = catalog_dataset(country_bundle, population)
    return DatasetSelection(
        name=dataset.identity,
        uri=dataset.uri,
        is_default=dataset.identity == country_bundle.default_dataset,
    )


def catalog_region_groups(population: CatalogPopulation) -> list[list[str]]:
    """Return the US partition's region groups for the population's states.

    Every state the catalog filters is calculated: a state the partition
    does not list joins its last group. Raise ValueError when the partition
    lists a state the catalog has no filter for, since its households would
    be left out of the national total.
    """
    groups = stage12_region_groups("us")
    filters = population.state_filters
    if groups is None or not filters:
        msg = "a segmented run needs the catalog's state filters"
        raise ValueError(msg)
    partitioned = {code for group in groups for code in group}
    missing = sorted(partitioned - set(filters))
    if missing:
        msg = f"the catalog population has no filter for {', '.join(missing)}"
        raise ValueError(msg)
    groups[-1].extend(sorted(set(filters) - partitioned))
    return groups
