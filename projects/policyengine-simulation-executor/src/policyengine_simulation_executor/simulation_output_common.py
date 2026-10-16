"""Shared helpers for simulation macro output serialization."""

from __future__ import annotations

import logging
import math
from collections.abc import Callable, Mapping
from importlib import import_module
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import pandas as pd
    from _typeshed import ConvertibleToFloat
    from policyengine.core import Simulation

import numpy as np

logger = logging.getLogger(__name__)


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
    """The weighted number of entities: the plain sum of their weights.

    Output frames are microdf ``MicroDataFrame``s weighted by the entity's
    weight, so a column's ``.sum()`` is already a weighted sum. Summing the
    weight column that way weights it twice and returns the sum of squared
    weights, about 10^4 times the household count on the sparse US data.
    Read the raw values instead. A weighted frame always holds its weight
    column, so there is no aggregate fallback: that would weight it again.
    """
    data = _entity_data(simulation, entity)
    return float(np.asarray(data[weight], dtype=float).sum())


def _sum_output_variable(simulation: Simulation, variable: str, entity: str) -> float:
    data = _entity_data(simulation, entity)
    if variable in data.columns:
        return float(data[variable].sum())

    from policyengine.outputs import Aggregate, AggregateType

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
    if variable in baseline_data.columns and variable in reform_data.columns:
        return float((reform_data[variable] - baseline_data[variable]).sum())

    from policyengine.outputs import ChangeAggregate, ChangeAggregateType

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
        from policyengine_simulation_contract.spm import spm_error_detail

        if spm_error_detail(exc) is not None:
            raise
        logger.warning("Unable to calculate %s", label, exc_info=True)
        return None
