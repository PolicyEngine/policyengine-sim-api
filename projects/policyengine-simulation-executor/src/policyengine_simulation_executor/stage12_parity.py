"""Canonical Stage 12 comparisons that never include calculation values in errors."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import math

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class NumericalTolerance:
    absolute: float = 0.0
    relative: float = 0.0

    def __post_init__(self) -> None:
        if self.absolute < 0 or self.relative < 0:
            raise ValueError("numerical tolerances must be non-negative")


class ParityMismatch(ValueError):
    """A bounded diagnostic containing a code and schema path, never values."""

    def __init__(self, code: str, path: str) -> None:
        self.code = code
        self.path = path[:255]
        super().__init__(f"{code} at {self.path}")


def canonicalize_simulation_frames(
    frames: Mapping[str, pd.DataFrame],
) -> dict[str, pd.DataFrame]:
    canonical = {}
    for entity in sorted(frames):
        identifier = f"{entity}_id"
        frame = pd.DataFrame(frames[entity]).copy()
        if identifier not in frame.columns:
            raise ParityMismatch("missing_row_identity", entity)
        if frame[identifier].duplicated().any():
            raise ParityMismatch("duplicate_row_identity", entity)
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
    expected = canonicalize_simulation_frames(incumbent)
    actual = canonicalize_simulation_frames(candidate)
    if expected.keys() != actual.keys():
        raise ParityMismatch("entity_membership_mismatch", "simulation")
    allowed = tolerances or {}
    for entity, expected_frame in expected.items():
        actual_frame = actual[entity]
        if tuple(expected_frame.columns) != tuple(actual_frame.columns):
            raise ParityMismatch("column_membership_mismatch", entity)
        if len(expected_frame) != len(actual_frame):
            raise ParityMismatch("row_membership_mismatch", entity)
        identifier = f"{entity}_id"
        if not expected_frame[identifier].equals(actual_frame[identifier]):
            raise ParityMismatch("row_identity_mismatch", entity)
        for column in expected_frame.columns:
            path = f"{entity}.{column}"
            expected_values = expected_frame[column]
            actual_values = actual_frame[column]
            if str(expected_values.dtype) != str(actual_values.dtype):
                raise ParityMismatch("dtype_mismatch", path)
            if pd.api.types.is_numeric_dtype(expected_values.dtype):
                tolerance = allowed.get(path, NumericalTolerance())
                if not np.allclose(
                    expected_values.to_numpy(),
                    actual_values.to_numpy(),
                    rtol=tolerance.relative,
                    atol=tolerance.absolute,
                    equal_nan=True,
                ):
                    raise ParityMismatch("numeric_value_mismatch", path)
            elif not expected_values.equals(actual_values):
                raise ParityMismatch("value_mismatch", path)


def compare_aggregate_reports(
    incumbent: object,
    candidate: object,
    *,
    tolerances: Mapping[str, NumericalTolerance] | None = None,
) -> None:
    allowed = tolerances or {}

    def compare(expected: object, actual: object, path: str) -> None:
        if isinstance(expected, Mapping):
            if not isinstance(actual, Mapping) or expected.keys() != actual.keys():
                raise ParityMismatch("field_membership_mismatch", path or "report")
            for key in sorted(expected):
                compare(expected[key], actual[key], f"{path}.{key}".lstrip("."))
            return
        if isinstance(expected, Sequence) and not isinstance(
            expected, (str, bytes, bytearray)
        ):
            if not isinstance(actual, Sequence) or isinstance(
                actual, (str, bytes, bytearray)
            ):
                raise ParityMismatch("value_type_mismatch", path)
            if len(expected) != len(actual):
                raise ParityMismatch("list_length_mismatch", path)
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
                raise ParityMismatch("numeric_value_mismatch", path)
            return
        if type(expected) is not type(actual):
            raise ParityMismatch("value_type_mismatch", path)
        if expected != actual:
            raise ParityMismatch("value_mismatch", path)

    compare(incumbent, candidate, "")
