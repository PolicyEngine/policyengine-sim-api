"""Controlled output-parity tests for the existing and Stage 12 paths."""

import numpy as np
import pandas as pd
import pytest
from policyengine_simulation_executor.stage12_parity import (
    NumericalTolerance,
    ParityMismatchError,
    compare_aggregate_reports,
    compare_simulation_frames,
)


def _frames(value: float = 100.0):
    return {
        "household": pd.DataFrame(
            {
                "household_id": np.array([2, 1], dtype=np.int64),
                "household_net_income": np.array([value + 1, value], dtype=np.float32),
            }
        ),
        "person": pd.DataFrame(
            {
                "person_id": np.array([2, 1], dtype=np.int64),
                "person_household_id": np.array([2, 1], dtype=np.int64),
            }
        ),
    }


def test_simulation_parity_canonicalizes_rows_entities_and_columns() -> None:
    incumbent = _frames()
    candidate = {
        entity: frame.iloc[::-1].reindex(columns=list(reversed(frame.columns)))
        for entity, frame in reversed(tuple(incumbent.items()))
    }

    compare_simulation_frames(incumbent, candidate)


def test_simulation_parity_requires_reviewed_field_specific_tolerance() -> None:
    incumbent = _frames()
    candidate = _frames(100.005)

    with pytest.raises(ParityMismatchError) as error:
        compare_simulation_frames(incumbent, candidate)
    assert error.value.code == "numeric_value_mismatch"
    assert "100" not in str(error.value)

    compare_simulation_frames(
        incumbent,
        candidate,
        tolerances={
            "household.household_net_income": NumericalTolerance(absolute=0.01)
        },
    )


def test_simulation_parity_compares_schema_dtypes_and_row_membership() -> None:
    wrong_dtype = _frames()
    wrong_dtype["person"]["person_household_id"] = wrong_dtype["person"][
        "person_household_id"
    ].astype("int32")
    with pytest.raises(ParityMismatchError, match="dtype_mismatch"):
        compare_simulation_frames(_frames(), wrong_dtype)

    missing_row = _frames()
    missing_row["person"] = missing_row["person"].iloc[:1]
    with pytest.raises(ParityMismatchError, match="row_membership_mismatch"):
        compare_simulation_frames(_frames(), missing_row)


def test_aggregate_parity_compares_every_field_and_bounds_diagnostics() -> None:
    incumbent = {
        "budget": {"total": 100.0},
        "poverty": [{"name": "all", "rate": 0.12}],
    }
    candidate = {
        "budget": {"total": 100.001},
        "poverty": [{"name": "all", "rate": 0.12}],
    }

    with pytest.raises(ParityMismatchError) as error:
        compare_aggregate_reports(incumbent, candidate)
    assert error.value.path == "budget.total"
    assert "100" not in str(error.value)

    compare_aggregate_reports(
        incumbent,
        candidate,
        tolerances={"budget.total": NumericalTolerance(absolute=0.01)},
    )


def _without_column(frames, entity, column):
    return {**frames, entity: frames[entity].drop(columns=[column])}


def _with_column(frames, entity, column, values):
    return {**frames, entity: frames[entity].assign(**{column: values})}


def _person_frames(**columns):
    return {
        "person": pd.DataFrame(
            {"person_id": np.array([1, 2], dtype=np.int64), **columns}
        )
    }


@pytest.mark.parametrize(
    ("incumbent", "candidate", "code", "path"),
    [
        pytest.param(
            _without_column(_frames(), "person", "person_id"),
            _frames(),
            "missing_row_identity",
            "person",
            id="incumbent-missing-id",
        ),
        pytest.param(
            _frames(),
            {
                **_frames(),
                "person": pd.DataFrame(
                    {
                        "person_id": np.array([1, 1], dtype=np.int64),
                        "person_household_id": np.array([1, 1], dtype=np.int64),
                    }
                ),
            },
            "duplicate_row_identity",
            "person",
            id="candidate-duplicate-id",
        ),
        pytest.param(
            _frames(),
            {"household": _frames()["household"]},
            "entity_membership_mismatch",
            "simulation",
            id="missing-entity",
        ),
        pytest.param(
            _frames(),
            _with_column(_frames(), "household", "extra", [1, 2]),
            "column_membership_mismatch",
            "household",
            id="extra-column",
        ),
        pytest.param(
            _frames(),
            _with_column(
                _frames(), "person", "person_id", np.array([3, 1], dtype=np.int64)
            ),
            "row_identity_mismatch",
            "person",
            id="different-ids",
        ),
        pytest.param(
            _person_frames(name=["a", "b"]),
            _person_frames(name=["a", "c"]),
            "value_mismatch",
            "person.name",
            id="text-value",
        ),
        pytest.param(
            _person_frames(score=np.array([1.0, np.nan])),
            _person_frames(score=np.array([1.0, 2.0])),
            "numeric_value_mismatch",
            "person.score",
            id="nan-against-number",
        ),
        pytest.param(
            {
                "a": pd.DataFrame({"a_id": [1], "value": [1.0]}),
                "b": pd.DataFrame({"b_id": [1], "value": [1.0]}),
            },
            {
                "a": pd.DataFrame({"a_id": [1], "value": [2.0]}),
                "b": pd.DataFrame({"b_id": [1], "value": [3.0]}),
            },
            "numeric_value_mismatch",
            "a.value",
            id="first-entity-reported",
        ),
        pytest.param(
            _person_frames(name=["a", "b"], score=[1.0, 2.0]),
            _person_frames(name=["a", "c"], score=[1.0, 3.0]),
            "value_mismatch",
            "person.name",
            id="columns-checked-in-name-order",
        ),
        pytest.param(
            _person_frames(score=[1.0, 2.0]),
            _person_frames(score=[1, 2]),
            "dtype_mismatch",
            "person.score",
            id="float-against-int",
        ),
    ],
)
def test_simulation_parity_reports_the_first_mismatch(incumbent, candidate, code, path):
    with pytest.raises(ParityMismatchError) as error:
        compare_simulation_frames(incumbent, candidate)

    assert (error.value.code, error.value.path) == (code, path)
    assert str(error.value) == f"{code} at {path}"


def test_simulation_parity_treats_matching_nan_and_text_as_equal() -> None:
    frames = _person_frames(score=np.array([np.nan, 1.0]), name=["a", None])

    compare_simulation_frames(
        frames,
        _person_frames(score=np.array([np.nan, 1.0]), name=["a", None]),
    )


def test_simulation_parity_applies_relative_tolerances() -> None:
    compare_simulation_frames(
        _person_frames(score=[100.0, 200.0]),
        _person_frames(score=[101.0, 202.0]),
        tolerances={"person.score": NumericalTolerance(relative=0.01)},
    )
    with pytest.raises(ParityMismatchError, match="numeric_value_mismatch"):
        compare_simulation_frames(
            _person_frames(score=[100.0, 200.0]),
            _person_frames(score=[101.0, 203.0]),
            tolerances={"person.score": NumericalTolerance(relative=0.01)},
        )


@pytest.mark.parametrize(
    ("incumbent", "candidate", "code", "path"),
    [
        pytest.param(
            {"a": 1}, {"b": 1}, "field_membership_mismatch", "report", id="root-keys"
        ),
        pytest.param(
            {"a": 1}, [1], "field_membership_mismatch", "report", id="root-type"
        ),
        pytest.param(
            {"a": {"b": 1}},
            {"a": {"b": 1, "c": 2}},
            "field_membership_mismatch",
            "a",
            id="nested-keys",
        ),
        pytest.param(
            {"a": [1]}, {"a": "1"}, "value_type_mismatch", "a", id="list-as-text"
        ),
        pytest.param(
            {"a": [1]}, {"a": 1}, "value_type_mismatch", "a", id="list-as-number"
        ),
        pytest.param(
            {"a": [1, 2]}, {"a": [1]}, "list_length_mismatch", "a", id="list-length"
        ),
        pytest.param(
            {"a": [{"b": 1}, {"b": 2}]},
            {"a": [{"b": 1}, {"b": 3}]},
            "numeric_value_mismatch",
            "a[1].b",
            id="nested-index",
        ),
        pytest.param([1, 2], [1, 3], "numeric_value_mismatch", "[1]", id="root-list"),
        pytest.param(
            {"a": True}, {"a": 1}, "value_type_mismatch", "a", id="bool-as-number"
        ),
        pytest.param(
            {"a": 1}, {"a": False}, "value_type_mismatch", "a", id="number-as-bool"
        ),
        pytest.param({"a": True}, {"a": False}, "value_mismatch", "a", id="bool-value"),
        pytest.param({"a": "x"}, {"a": "y"}, "value_mismatch", "a", id="text-value"),
        pytest.param(
            {"a": None}, {"a": 0}, "value_type_mismatch", "a", id="none-as-number"
        ),
        pytest.param(
            {"a": "x"}, {"a": b"x"}, "value_type_mismatch", "a", id="text-as-bytes"
        ),
        pytest.param(
            {"b": 1, "a": 2},
            {"b": 2, "a": 3},
            "numeric_value_mismatch",
            "a",
            id="keys-checked-in-sorted-order",
        ),
        pytest.param(
            {"a": float("nan")},
            {"a": float("nan")},
            "numeric_value_mismatch",
            "a",
            id="nan-never-matches",
        ),
    ],
)
def test_aggregate_parity_reports_the_first_mismatch(incumbent, candidate, code, path):
    with pytest.raises(ParityMismatchError) as error:
        compare_aggregate_reports(incumbent, candidate)

    assert (error.value.code, error.value.path) == (code, path)


@pytest.mark.parametrize(
    ("incumbent", "candidate"),
    [
        ({"a": 1}, {"a": 1.0}),
        ({"a": [1, "x", None, True]}, {"a": (1, "x", None, True)}),
        ({"a": {"b": [1.0]}}, {"a": {"b": [1]}}),
        ("text", "text"),
        (None, None),
    ],
)
def test_aggregate_parity_accepts_equal_reports(incumbent, candidate):
    compare_aggregate_reports(incumbent, candidate)


def test_aggregate_parity_applies_relative_tolerances_by_path() -> None:
    compare_aggregate_reports(
        {"deciles": [100.0]},
        {"deciles": [101.0]},
        tolerances={"deciles[0]": NumericalTolerance(relative=0.01)},
    )


def test_parity_paths_are_truncated_to_255_characters() -> None:
    long_key = "k" * 300

    with pytest.raises(ParityMismatchError) as error:
        compare_aggregate_reports({long_key: 1}, {long_key: 2})

    assert error.value.path == long_key[:255]
    assert str(error.value) == f"numeric_value_mismatch at {long_key[:255]}"


@pytest.mark.parametrize("tolerance", [{"absolute": -0.1}, {"relative": -0.1}])
def test_negative_tolerances_are_rejected(tolerance) -> None:
    with pytest.raises(
        ValueError, match=r"^numerical tolerances must be non-negative$"
    ):
        NumericalTolerance(**tolerance)
