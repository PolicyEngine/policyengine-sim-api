"""Controlled output-parity tests for the existing and Stage 12 paths."""

import numpy as np
import pandas as pd
import pytest

from policyengine_simulation_executor.stage12_parity import (
    NumericalTolerance,
    ParityMismatch,
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

    with pytest.raises(ParityMismatch) as error:
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
    with pytest.raises(ParityMismatch, match="dtype_mismatch"):
        compare_simulation_frames(_frames(), wrong_dtype)

    missing_row = _frames()
    missing_row["person"] = missing_row["person"].iloc[:1]
    with pytest.raises(ParityMismatch, match="row_membership_mismatch"):
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

    with pytest.raises(ParityMismatch) as error:
        compare_aggregate_reports(incumbent, candidate)
    assert error.value.path == "budget.total"
    assert "100" not in str(error.value)

    compare_aggregate_reports(
        incumbent,
        candidate,
        tolerances={"budget.total": NumericalTolerance(absolute=0.01)},
    )
