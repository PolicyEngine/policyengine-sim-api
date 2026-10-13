"""Canonical Stage 12 comparisons that never include calculation values in errors."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class NumericalTolerance:
    """Absolute and relative slack for one numeric field; the default is exact."""

    absolute: float = 0.0
    relative: float = 0.0

    def __post_init__(self) -> None:
        """Reject negative tolerances with ``ValueError``."""
        if self.absolute < 0 or self.relative < 0:
            msg = "numerical tolerances must be non-negative"
            raise ValueError(msg)


class ParityMismatch(ValueError):
    """A bounded diagnostic containing a code and schema path, never values."""

    def __init__(self, code: str, path: str) -> None:
        """Record the mismatch code and its path, truncated to 255 characters."""
        self.code = code
        self.path = path[:255]
        super().__init__(f"{code} at {self.path}")


def canonicalize_simulation_frames(
    frames: Mapping[str, pd.DataFrame],
) -> dict[str, pd.DataFrame]:
    """Sort each entity's rows by ``<entity>_id`` and its columns by name.

    Raises ``ParityMismatch`` if an entity lacks that id column or repeats an id.
    """
    canonical = {}
    for entity in sorted(frames):
        identifier = f"{entity}_id"
        frame = pd.DataFrame(frames[entity]).copy()
        if identifier not in frame.columns:
            error_code = "missing_row_identity"
            raise ParityMismatch(error_code, entity)
        if frame[identifier].duplicated().any():
            error_code = "duplicate_row_identity"
            raise ParityMismatch(error_code, entity)
        canonical[entity] = (
            frame.sort_values(identifier, kind="stable")
            .reset_index(drop=True)
            .reindex(columns=sorted(frame.columns))
        )
    return canonical


def compare_simulation_frames(
    incumbent: Mapping[str, pd.DataFrame],
    candidate: Mapping[str, pd.DataFrame],
    *,
    tolerances: Mapping[str, NumericalTolerance] | None = None,
) -> None:
    """Raise ``ParityMismatch`` at the first difference between two simulation outputs.

    Entities, columns, row ids and dtypes must match exactly. Numeric columns
    compare within the tolerance keyed by ``entity.column`` (exact by default,
    NaN equal to NaN); other columns must be equal.
    """
    expected = canonicalize_simulation_frames(incumbent)
    actual = canonicalize_simulation_frames(candidate)
    if expected.keys() != actual.keys():
        error_code = "entity_membership_mismatch"
        raise ParityMismatch(error_code, "simulation")
    allowed = tolerances or {}
    for entity, expected_frame in expected.items():
        actual_frame = actual[entity]
        if tuple(expected_frame.columns) != tuple(actual_frame.columns):
            error_code = "column_membership_mismatch"
            raise ParityMismatch(error_code, entity)
        if len(expected_frame) != len(actual_frame):
            error_code = "row_membership_mismatch"
            raise ParityMismatch(error_code, entity)
        identifier = f"{entity}_id"
        if not expected_frame[identifier].equals(actual_frame[identifier]):
            error_code = "row_identity_mismatch"
            raise ParityMismatch(error_code, entity)
        for column in expected_frame.columns:
            path = f"{entity}.{column}"
            expected_values = expected_frame[column]
            actual_values = actual_frame[column]
            if str(expected_values.dtype) != str(actual_values.dtype):
                error_code = "dtype_mismatch"
                raise ParityMismatch(error_code, path)
            if pd.api.types.is_numeric_dtype(expected_values.dtype):
                tolerance = allowed.get(path, NumericalTolerance())
                if not np.allclose(
                    expected_values.to_numpy(),
                    actual_values.to_numpy(),
                    rtol=tolerance.relative,
                    atol=tolerance.absolute,
                    equal_nan=True,
                ):
                    error_code = "numeric_value_mismatch"
                    raise ParityMismatch(error_code, path)
            elif not expected_values.equals(actual_values):
                error_code = "value_mismatch"
                raise ParityMismatch(error_code, path)


def compare_aggregate_reports(
    incumbent: object,
    candidate: object,
    *,
    tolerances: Mapping[str, NumericalTolerance] | None = None,
) -> None:
    """Raise ``ParityMismatch`` at the first difference between two aggregate reports.

    Mappings and lists must match in shape. Numbers (not booleans) compare
    within the tolerance keyed by their path, such as ``budget.total`` or
    ``deciles[3]``; any other value must be equal and of the same type.
    """
    allowed = tolerances or {}

    def compare(expected: object, actual: object, path: str) -> None:
        if isinstance(expected, Mapping):
            if not isinstance(actual, Mapping) or expected.keys() != actual.keys():
                error_code = "field_membership_mismatch"
                raise ParityMismatch(error_code, path or "report")
            for key in sorted(expected):
                compare(expected[key], actual[key], f"{path}.{key}".lstrip("."))
            return
        if isinstance(expected, Sequence) and not isinstance(
            expected, (str, bytes, bytearray)
        ):
            if not isinstance(actual, Sequence) or isinstance(
                actual, (str, bytes, bytearray)
            ):
                error_code = "value_type_mismatch"
                raise ParityMismatch(error_code, path)
            if len(expected) != len(actual):
                error_code = "list_length_mismatch"
                raise ParityMismatch(error_code, path)
            for index, value in enumerate(expected):
                compare(value, actual[index], f"{path}[{index}]")
            return
        if (
            isinstance(expected, (int, float))
            and not isinstance(expected, bool)
            and isinstance(actual, (int, float))
            and not isinstance(actual, bool)
        ):
            tolerance = allowed.get(path, NumericalTolerance())
            if not math.isclose(
                float(expected),
                float(actual),
                rel_tol=tolerance.relative,
                abs_tol=tolerance.absolute,
            ):
                error_code = "numeric_value_mismatch"
                raise ParityMismatch(error_code, path)
            return
        if type(expected) is not type(actual):
            error_code = "value_type_mismatch"
            raise ParityMismatch(error_code, path)
        if expected != actual:
            error_code = "value_mismatch"
            raise ParityMismatch(error_code, path)

    compare(incumbent, candidate, "")
