"""Shared helpers for simulation macro output serialization."""

from __future__ import annotations

import logging
import math
from collections.abc import Mapping
from importlib import import_module
from typing import Any

logger = logging.getLogger(__name__)


def _number(value: Any, default: float = 0.0) -> float:
    if value is None:
        return default
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    if math.isnan(result) or math.isinf(result):
        return default
    return result


def _collection_records(collection: Any) -> list[dict[str, Any]]:
    if collection is None:
        return []
    dataframe = getattr(collection, "dataframe", None)
    if dataframe is not None:
        return list(dataframe.to_dict("records"))
    if isinstance(collection, list):
        return [dict(item) for item in collection if isinstance(item, Mapping)]
    return []


def _output_model_dump(value: Any) -> Any:
    if value is None:
        return None
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, Mapping):
        return dict(value)
    return None


def _entity_data(simulation, entity: str):
    if simulation.output_dataset is None or simulation.output_dataset.data is None:
        simulation.ensure()
    return getattr(simulation.output_dataset.data, entity)


def _sum_output_variable(simulation, variable: str, entity: str) -> float:
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


def _change_output_variable(baseline, reform, variable: str, entity: str) -> float:
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


def _output_module_function(module_name: str, name: str):
    module = import_module(f"policyengine.outputs.{module_name}")
    return getattr(module, name)


def _poverty_module_function(name: str):
    return _output_module_function("poverty", name)


def _try_compute_output(label: str, fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except Exception:
        logger.warning("Unable to calculate %s", label, exc_info=True)
        return None
