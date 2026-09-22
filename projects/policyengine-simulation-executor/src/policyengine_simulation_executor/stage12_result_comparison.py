"""Exact, private comparison of production and Stage 12 aggregate results."""

from __future__ import annotations

from datetime import UTC, datetime
from hashlib import sha256
from typing import TypeGuard
from uuid import UUID

from policyengine_simulation_contract.stage12_execution import (
    ResultComparisonArtifactPayload,
    ResultDifference,
)
from pydantic import JsonValue, TypeAdapter

from policyengine_simulation_executor.stage12_artifacts import canonical_json_bytes

type ResultObject = dict[str, JsonValue]
_result_adapter = TypeAdapter(ResultObject)


class _MissingValue:
    """Sentinel type for a JSON Pointer absent from one result."""


_MISSING = _MissingValue()


def _is_present(value: JsonValue | _MissingValue) -> TypeGuard[JsonValue]:
    return not isinstance(value, _MissingValue)


def _pointer_token(value: str) -> str:
    return value.replace("~", "~0").replace("/", "~1")


def _leaf_values(value: JsonValue, path: str = "") -> dict[str, JsonValue]:
    """Flatten JSON containers to scalar leaves using JSON Pointer paths."""

    if isinstance(value, dict):
        if not value:
            return {path: value}
        leaves: dict[str, JsonValue] = {}
        for key in sorted(value):
            child_path = f"{path}/{_pointer_token(key)}"
            leaves.update(_leaf_values(value[key], child_path))
        return leaves
    if isinstance(value, list):
        if not value:
            return {path: value}
        leaves = {}
        for index, item in enumerate(value):
            leaves.update(_leaf_values(item, f"{path}/{index}"))
        return leaves
    return {path: value}


def _equal_json_values(first: JsonValue, second: JsonValue) -> bool:
    return canonical_json_bytes(first) == canonical_json_bytes(second)


def _numeric_deltas(
    production_value: JsonValue,
    stage12_value: JsonValue,
) -> tuple[float | None, float | None]:
    if (
        isinstance(production_value, bool)
        or isinstance(stage12_value, bool)
        or not isinstance(production_value, (int, float))
        or not isinstance(stage12_value, (int, float))
    ):
        return None, None
    absolute = abs(float(stage12_value) - float(production_value))
    if float(production_value) == 0:
        return absolute, None
    return absolute, absolute / abs(float(production_value))


def compare_results(
    *,
    evaluation_id: UUID,
    production_job_id: str,
    production_result: object,
    stage12_result: object,
    compared_at: datetime | None = None,
) -> ResultComparisonArtifactPayload:
    """Return an exact digest and every differing scalar result leaf."""

    production = _result_adapter.validate_python(production_result)
    stage12 = _result_adapter.validate_python(stage12_result)
    production_bytes = canonical_json_bytes(production)
    stage12_bytes = canonical_json_bytes(stage12)
    production_leaves = _leaf_values(production)
    stage12_leaves = _leaf_values(stage12)
    differences: list[ResultDifference] = []
    for path in sorted(production_leaves.keys() | stage12_leaves.keys()):
        production_value = production_leaves.get(path, _MISSING)
        stage12_value = stage12_leaves.get(path, _MISSING)
        production_present = _is_present(production_value)
        stage12_present = _is_present(stage12_value)
        if (
            production_present
            and stage12_present
            and _equal_json_values(production_value, stage12_value)
        ):
            continue
        absolute_delta: float | None = None
        relative_delta: float | None = None
        if production_present and stage12_present:
            absolute_delta, relative_delta = _numeric_deltas(
                production_value,
                stage12_value,
            )
        differences.append(
            ResultDifference(
                path=path,
                production_present=production_present,
                stage12_present=stage12_present,
                production_value=production_value if production_present else None,
                stage12_value=stage12_value if stage12_present else None,
                absolute_delta=absolute_delta,
                relative_delta=relative_delta,
            )
        )
    return ResultComparisonArtifactPayload(
        evaluation_id=evaluation_id,
        production_job_id=production_job_id,
        compared_at=compared_at or datetime.now(UTC),
        status="matched" if not differences else "different",
        production_result_sha256=sha256(production_bytes).hexdigest(),
        stage12_result_sha256=sha256(stage12_bytes).hexdigest(),
        difference_count=len(differences),
        differences=tuple(differences),
    )
