"""Shared helpers for simulation macro output serialization."""

from __future__ import annotations

import logging
import math
from collections.abc import Callable, Mapping
from importlib import import_module
from typing import TYPE_CHECKING, Any

import pandas as pd
from policyengine_simulation_contract.spm import spm_error_detail

if TYPE_CHECKING:
    from _typeshed import ConvertibleToFloat
    from policyengine.core import Simulation

import numpy as np

logger = logging.getLogger(__name__)

# Weights read back from a stored artifact may have passed through float32, so
# "identical" allows a float32 round trip and nothing more.
WEIGHT_RELATIVE_TOLERANCE = 1e-6


def _number(value: ConvertibleToFloat | None, default: float = 0.0) -> float:
    if value is None:
        return default
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    if math.isnan(result) or math.isinf(result):
        return default
    return result


def _collection_records(collection: object) -> list[dict[str, Any]]:
    if collection is None:
        return []
    dataframe = getattr(collection, "dataframe", None)
    if dataframe is not None:
        return list(dataframe.to_dict("records"))
    if isinstance(collection, list):
        return [dict(item) for item in collection if isinstance(item, Mapping)]
    return []


def _output_model_dump(value: object) -> object:
    if value is None:
        return None
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, Mapping):
        return dict(value)
    return None


def _entity_data(simulation: Simulation, entity: str) -> pd.DataFrame:
    if simulation.output_dataset is None or simulation.output_dataset.data is None:
        simulation.ensure()
    return getattr(simulation.output_dataset.data, entity)


def _count_output_entities(simulation: Simulation, entity: str, weight: str) -> float:
    """Return the weighted number of entities: the plain sum of their weights.

    Output frames are microdf ``MicroDataFrame``s weighted by the entity's
    weight, so a column's ``.sum()`` is already a weighted sum. Summing the
    weight column that way weights it twice and returns the sum of squared
    weights, about 10^4 times the household count on the sparse US data.
    Read the raw values instead. A weighted frame always holds its weight
    column, so there is no aggregate fallback: that would weight it again.
    """
    data = _entity_data(simulation, entity)
    return float(np.asarray(data[weight], dtype=float).sum())


def _unweighted_household_frame(simulation: Simulation) -> pd.DataFrame:
    """Return the household output as a plain DataFrame, without its weights.

    policyengine's ``compute_longwise_uk_geography_impacts`` takes plain frames
    and weights each area by the baseline's ``household_weight`` column itself.
    The UK local-authority breakdown calls it directly, so that it can apply
    this service's boundary metadata. Handing it the weighted frame would weight
    twice if it ever reduced a column directly.

    This is a recorded exception to sim.microdata.weight-stripping-confined,
    listed in rules/baselines/sim-microdata-weight-stripping.txt, which may only
    shrink. It goes once the breakdown hands policyengine the simulations, as
    the constituency and US congressional district breakdowns already do.
    """
    return pd.DataFrame(simulation.output_dataset.data.household)


def _warn_on_mismatched_weights(
    baseline_data: pd.DataFrame, reform_data: pd.DataFrame, entity: str
) -> None:
    """Warn when a reform-minus-baseline total's two sides are weighted differently.

    MicroSeries arithmetic keeps the left operand's weights, so reform minus
    baseline is weighted by the reform's weights alone. That change total
    only means something when both sides weight the same records identically.
    Plain frames, which carry no weights, have nothing to compare. The total
    is still published: this warns rather than refuses until logs show that
    no legitimate run produces a mismatch.
    """
    baseline_weights = getattr(baseline_data, "weights", None)
    reform_weights = getattr(reform_data, "weights", None)
    if baseline_weights is None and reform_weights is None:
        return
    if (
        baseline_weights is None
        or reform_weights is None
        or len(baseline_weights) != len(reform_weights)
        or not np.allclose(
            np.asarray(baseline_weights, dtype=float),
            np.asarray(reform_weights, dtype=float),
            rtol=WEIGHT_RELATIVE_TOLERANCE,
            atol=0.0,
            equal_nan=True,
        )
    ):
        logger.warning(
            "Baseline and reform %s frames carry different weights, so their "
            "change total is weighted by the reform's weights alone",
            entity,
        )


def _sum_output_variable(simulation: Simulation, variable: str, entity: str) -> float:
    data = _entity_data(simulation, entity)
    if variable in data.columns:
        return float(data[variable].sum())

    from policyengine.outputs import (  # noqa: PLC0415  # policyengine stays out of this module's import-time imports
        Aggregate,
        AggregateType,
    )

    output = Aggregate(
        simulation=simulation,
        variable=variable,
        entity=entity,
        aggregate_type=AggregateType.SUM,
    )
    output.run()
    return float(output.result)


def _change_output_variable(
    baseline: Simulation, reform: Simulation, variable: str, entity: str
) -> float:
    baseline_data = _entity_data(baseline, entity)
    reform_data = _entity_data(reform, entity)
    _warn_on_mismatched_weights(baseline_data, reform_data, entity)
    if variable in baseline_data.columns and variable in reform_data.columns:
        return float((reform_data[variable] - baseline_data[variable]).sum())

    from policyengine.outputs import (  # noqa: PLC0415  # policyengine stays out of this module's import-time imports
        ChangeAggregate,
        ChangeAggregateType,
    )

    output = ChangeAggregate(
        baseline_simulation=baseline,
        reform_simulation=reform,
        variable=variable,
        entity=entity,
        aggregate_type=ChangeAggregateType.SUM,
    )
    output.run()
    return float(output.result)


def _output_module_function(module_name: str, name: str) -> Callable[..., object]:
    module = import_module(f"policyengine.outputs.{module_name}")
    return getattr(module, name)


def _poverty_module_function(name: str) -> Callable[..., object]:
    return _output_module_function("poverty", name)


def _try_compute_output[**P, T](
    label: str, fn: Callable[P, T], *args: P.args, **kwargs: P.kwargs
) -> T | None:
    try:
        return fn(*args, **kwargs)
    except Exception as exc:
        if spm_error_detail(exc) is not None:
            raise
        logger.warning("Unable to calculate %s", label, exc_info=True)
        return None
