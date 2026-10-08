"""Unit coverage for reporting real ACS preparation and calculation results.

The real data-loading check runs separately in the PR Modal image check.
These small frames test its acceptance conditions, not dataset loading.
"""

from dataclasses import replace

import numpy as np
import pandas as pd
import pytest

from policyengine_simulation_executor.simulation_runtime import DatasetSelection
from src.modal.us_regional_dataset_check import summarize_us_regional_calculation


@pytest.fixture
def selection() -> DatasetSelection:
    return DatasetSelection(
        name="populace_us_2024_acs_local",
        uri="hf://policyengine/populace-us/acs.h5@fixture-revision",
        is_default=False,
        artifact_revision="fixture-revision",
        sha256="a" * 64,
    )


@pytest.fixture
def frames() -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    return (
        pd.DataFrame({"takes_up_wic_if_eligible": [True, False, True]}),
        pd.DataFrame({"state_fips": [49], "household_net_income": [100.0]}),
        pd.DataFrame({"takes_up_wic_if_eligible": [True, False], "wic": [10.0, 0.0]}),
    )


def test_reports_selected_source_and_nonempty_utah_calculation(selection, frames):
    report = summarize_us_regional_calculation(
        selection, *frames, year=2026, policyengine_version="fixture-version"
    )

    assert report.dataset == selection.name
    assert report.source_sha256 == selection.sha256
    assert report.artifact_revision == "fixture-revision"
    assert report.prepared_person_count == 3
    assert report.household_count == 1
    assert report.person_count == 2


@pytest.mark.parametrize(
    "change", [{"is_default": True}, {"sha256": None}, {"artifact_revision": None}]
)
def test_refuses_national_or_unpinned_source(selection, frames, change):
    with pytest.raises(ValueError, match="non-default certified"):
        summarize_us_regional_calculation(
            replace(selection, **change),
            *frames,
            year=2026,
            policyengine_version="fixture-version",
        )


@pytest.mark.parametrize("scope", [0, 2])
def test_rejects_missing_prepared_or_calculated_wic_decisions(selection, frames, scope):
    frames[scope]["takes_up_wic_if_eligible"] = frames[scope][
        "takes_up_wic_if_eligible"
    ].astype("boolean")
    frames[scope].loc[0, "takes_up_wic_if_eligible"] = None
    with pytest.raises(ValueError, match="WIC participation"):
        summarize_us_regional_calculation(
            selection, *frames, year=2026, policyengine_version="fixture-version"
        )


@pytest.mark.parametrize(
    "frame_index,column", [(1, "household_net_income"), (2, "wic")]
)
@pytest.mark.parametrize("invalid", [np.nan, np.inf, -np.inf])
def test_rejects_nonfinite_calculated_values(
    selection, frames, frame_index, column, invalid
):
    frames[frame_index].loc[0, column] = invalid
    with pytest.raises(ValueError, match="finite"):
        summarize_us_regional_calculation(
            selection, *frames, year=2026, policyengine_version="fixture-version"
        )


def test_refuses_a_calculation_that_was_not_scoped_to_utah(selection, frames):
    frames[1].loc[0, "state_fips"] = 6
    with pytest.raises(ValueError, match="Utah"):
        summarize_us_regional_calculation(
            selection, *frames, year=2026, policyengine_version="fixture-version"
        )


@pytest.mark.parametrize("scope", [0, 2])
def test_rejects_a_missing_participation_column(selection, frames, scope):
    frames[scope].drop(columns="takes_up_wic_if_eligible", inplace=True)
    with pytest.raises(ValueError, match="WIC participation"):
        summarize_us_regional_calculation(
            selection, *frames, year=2026, policyengine_version="fixture-version"
        )


def test_rejects_missing_household_geography(selection, frames):
    frames[1]["state_fips"] = pd.Series([pd.NA], dtype="Int64")
    with pytest.raises(ValueError, match="Utah"):
        summarize_us_regional_calculation(
            selection, *frames, year=2026, policyengine_version="fixture-version"
        )
