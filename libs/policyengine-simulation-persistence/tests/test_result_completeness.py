"""Every field of every result model maps to a column or a catalog label."""

from __future__ import annotations

import types
import typing

from policyengine_simulation_contract.macro_output import SingleYearMacroOutput
from policyengine_simulation_contract.spm import SPMComparisonProvenance
from policyengine_simulation_persistence import (
    RESULT_FIELD_STORAGE,
    simulation_metadata,
)
from policyengine_simulation_persistence.economy_results import REFUSED_OTHER_KIND
from pydantic import BaseModel, RootModel

# Stored whole, as one JSON column.
WHOLE_VALUES = (SPMComparisonProvenance,)
CATALOG_SOURCES = {
    "catalog version: model version",
    "catalog version: data version",
    "region catalog: name",
    "region catalog: map x",
    "region catalog: map y",
}


def _container_paths(annotation: object, path: str) -> set[str] | None:
    """Return the leaf paths of a union, list or map, or None for anything else."""
    origin = typing.get_origin(annotation)
    if origin in (typing.Union, types.UnionType):
        return set().union(
            *(
                _leaf_paths(argument, path)
                for argument in typing.get_args(annotation)
                if argument is not type(None)
            )
        )
    if origin is list:
        (item,) = typing.get_args(annotation)
        return _leaf_paths(item, f"{path}[]")
    if origin is dict:
        _, value = typing.get_args(annotation)
        return _leaf_paths(value, f"{path}.{{key}}")
    return None


def _leaf_paths(annotation: object, path: str) -> set[str]:
    container = _container_paths(annotation, path)
    if container is not None:
        return container
    if not isinstance(annotation, type) or issubclass(annotation, WHOLE_VALUES):
        return {path}
    if issubclass(annotation, RootModel):
        return _leaf_paths(annotation.model_fields["root"].annotation, path)
    if issubclass(annotation, BaseModel):
        return set().union(
            *(
                _leaf_paths(field.annotation, f"{path}.{name}" if path else name)
                for name, field in annotation.model_fields.items()
            )
        )
    return {path}


def test_every_result_field_has_a_storage_location() -> None:
    assert set(RESULT_FIELD_STORAGE) == _leaf_paths(SingleYearMacroOutput, "")


def test_every_storage_location_exists() -> None:
    for path, location in RESULT_FIELD_STORAGE.items():
        if location in CATALOG_SOURCES or location == REFUSED_OTHER_KIND:
            continue
        table_name, column_name = location.split(".")
        table = simulation_metadata.tables[f"simulation.{table_name}"]
        assert column_name in table.c, path
