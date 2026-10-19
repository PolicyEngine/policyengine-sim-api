"""Per-state output blocks for a segmented US national run.

A segmented national run already holds every state's computed baseline and
reform microdata at the reduce. For each state whose own ``region=state/xx``
run would be a row filter on the SAME dataset the national run used, this
module applies that state's own ``RowFilterStrategy`` to the national
microdata and runs the existing ``SimulationOutputBuilder`` over the result.
The model never re-runs: a state's block is the output a separate state run
would build, because the rows, weights and household-independent outputs are
the same.

A state whose run resolves to a different dataset (for example a local-area
dataset) gets no block: its own run uses other households, so a block from the
national rows would not equal it. A block that fails to build is omitted and
logged; it never fails the national run.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import pandas as pd

from policyengine_simulation_executor.simulation_output_builder import (
    SimulationOutputBuilder,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping
    from types import ModuleType

    from policyengine.core.scoping_strategy import RowFilterStrategy
    from policyengine.tax_benefit_models.us.datasets import PolicyEngineUSDataset

logger = logging.getLogger(__name__)

# The request field that turns per-state blocks on or off. Absent means the
# default below.
INCLUDE_REGIONS_FIELD = "include_regions"
# Per-state blocks are on unless a request sets include_regions to false.
INCLUDE_REGIONS_DEFAULT = True
# The household input that a state's row filter reads. Region-group children
# copy it from their input dataset onto the household microdata they emit.
STATE_FILTER_HOUSEHOLD_INPUTS = ("state_fips",)
# Output fields copied from the national output into every block: the
# children's versions are authoritative (see segmented_national_reduce).
VERSION_FIELDS = ("model_version", "data_version")


def include_regions_requested(params: Mapping[str, Any]) -> bool:
    """Return whether a request wants per-state blocks (default on)."""
    value = params.get(INCLUDE_REGIONS_FIELD)
    if value is None:
        return INCLUDE_REGIONS_DEFAULT
    return value is True


def row_filter_regions_on_dataset(
    region_codes: Iterable[str],
    *,
    country: str,
    country_module: ModuleType,
) -> dict[str, RowFilterStrategy]:
    """Return the regions whose own run is a row filter on the national dataset.

    Each code is resolved exactly as a ``region=<code>`` request would be. A
    region qualifies only when that resolution yields a ``RowFilterStrategy``
    and selects the same dataset (name and URI) as the national run. Any
    other region, or one that fails to resolve, is left out and logged once.
    """
    from policyengine.core.scoping_strategy import (  # noqa: PLC0415  # policyengine stays out of this module's import-time imports
        RowFilterStrategy,
    )

    from policyengine_simulation_executor.simulation_runtime import (  # noqa: PLC0415  # import cycle: simulation_runtime imports the output builder this module also imports
        _resolve_dataset_selection,
        _resolve_region,
    )

    def selection_for(region: str | None) -> tuple[str, str, RowFilterStrategy | None]:
        params = {"country": country, "region": region}
        resolution = _resolve_region(
            country_module=country_module, country=country, params=params
        )
        selection = _resolve_dataset_selection(
            {"country": country}, region_resolution=resolution
        )
        strategy = resolution.scoping_strategy
        return (
            selection.name,
            selection.uri,
            strategy if isinstance(strategy, RowFilterStrategy) else None,
        )

    national_name, national_uri, _ = selection_for(None)
    eligible: dict[str, RowFilterStrategy] = {}
    other_dataset: list[str] = []
    not_row_filter: list[str] = []
    unresolved: list[str] = []
    for code in region_codes:
        try:
            name, uri, strategy = selection_for(code)
        except Exception:  # noqa: BLE001  # a region that cannot resolve gets no block; the national run continues
            unresolved.append(code)
            continue
        if strategy is None:
            not_row_filter.append(code)
        elif (name, uri) != (national_name, national_uri):
            other_dataset.append(code)
        else:
            eligible[code] = strategy
    if other_dataset or not_row_filter or unresolved:
        logger.info(
            "Per-state blocks omitted for %d regions: %d run on a dataset other "
            "than %s, %d are not row filters, %d did not resolve",
            len(other_dataset) + len(not_row_filter) + len(unresolved),
            len(other_dataset),
            national_name,
            len(not_row_filter),
            len(unresolved),
        )
    return eligible


def build_region_outputs(
    datasets: Mapping[str, PolicyEngineUSDataset],
    *,
    regions: Mapping[str, RowFilterStrategy],
    country: str,
    simulation_params: dict[str, Any],
    country_module: ModuleType,
    year: int,
    national_output: Mapping[str, Any],
    stand_in: Callable[[PolicyEngineUSDataset], Any],
) -> dict[str, dict[str, Any]]:
    """Build one state-run-shaped output per region from the national microdata.

    ``datasets`` holds the national ``baseline`` and ``reform`` output
    datasets and ``stand_in`` wraps a dataset as a simulation that never
    recomputes. A region whose block fails is omitted and logged.
    """
    outputs: dict[str, dict[str, Any]] = {}
    failed: list[str] = []
    for code, strategy in regions.items():
        try:
            outputs[code] = _build_region_output(
                datasets,
                code=code,
                strategy=strategy,
                country=country,
                simulation_params=simulation_params,
                country_module=country_module,
                year=year,
                national_output=national_output,
                stand_in=stand_in,
            )
        except Exception as exc:  # noqa: BLE001  # one state's block must never fail the national run
            failed.append(code)
            logger.warning(
                "Omitting the per-state block for %s: %s",
                code,
                type(exc).__name__,
            )
    if failed:
        logger.warning(
            "Per-state blocks failed for %d of %d regions: %s",
            len(failed),
            len(regions),
            ", ".join(failed),
        )
    return outputs


def _build_region_output(
    datasets: Mapping[str, PolicyEngineUSDataset],
    *,
    code: str,
    strategy: RowFilterStrategy,
    country: str,
    simulation_params: dict[str, Any],
    country_module: ModuleType,
    year: int,
    national_output: Mapping[str, Any],
    stand_in: Callable[[PolicyEngineUSDataset], Any],
) -> dict[str, Any]:
    scoped = {
        side: scope_dataset(dataset, strategy=strategy, year=year)
        for side, dataset in datasets.items()
    }
    builder = SimulationOutputBuilder(
        country=country,
        simulation_params=simulation_params,
        country_module=country_module,
        dataset=scoped["baseline"],
        baseline=stand_in(scoped["baseline"]),
        reform=stand_in(scoped["reform"]),
        resolved_region_code=code,
    )
    output = builder.serialize()
    for key in VERSION_FIELDS:
        if national_output.get(key):
            output[key] = national_output[key]
    return output


def scope_dataset(
    dataset: PolicyEngineUSDataset, *, strategy: RowFilterStrategy, year: int
) -> PolicyEngineUSDataset:
    """Apply a region's row filter to an output dataset, as a region run would.

    The filter is the region's own ``RowFilterStrategy.apply`` over the same
    entity cascade the US model uses on its input data.
    """
    from policyengine.tax_benefit_models.us.datasets import (  # noqa: PLC0415  # policyengine stays out of this module's import-time imports
        PolicyEngineUSDataset,
        USYearData,
    )
    from policyengine.tax_benefit_models.us.model import (  # noqa: PLC0415  # policyengine stays out of this module's import-time imports
        US_GROUP_ENTITIES,
    )

    if dataset.data is None:
        msg = "The national output dataset holds no data to scope"
        raise ValueError(msg)
    scoped = strategy.apply(
        entity_data=dataset.data.entity_data,
        group_entities=US_GROUP_ENTITIES,
        year=year,
    )
    return PolicyEngineUSDataset(
        data=USYearData(**scoped),
        year=year,
        filepath=None,
        name=dataset.name,
        description=dataset.description,
    )


def household_input_columns(
    simulations: Iterable[Any],
    household_ids: pd.Series,
    columns: Iterable[str] = STATE_FILTER_HOUSEHOLD_INPUTS,
) -> dict[str, pd.Series]:
    """Read household input columns for ``household_ids`` from a loaded input dataset.

    Returns the columns found on the first simulation whose input dataset is
    loaded and covers every household, aligned to ``household_ids``. A column
    that is missing or does not cover every household is left out.
    """
    for simulation in simulations:
        data = getattr(getattr(simulation, "dataset", None), "data", None)
        if data is None:
            continue
        household = pd.DataFrame(data.household)
        if "household_id" not in household.columns:
            continue
        indexed = household.set_index("household_id")
        found: dict[str, pd.Series] = {}
        for column in columns:
            if column not in indexed.columns:
                continue
            values = indexed[column].reindex(household_ids.to_numpy())
            if bool(values.isna().to_numpy().any()):
                continue
            found[column] = pd.Series(
                values.to_numpy(), index=household_ids.index, name=column
            )
        return found
    return {}
