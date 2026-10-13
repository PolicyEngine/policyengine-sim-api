"""Unit coverage for reporting real ACS preparation and calculation results.

The real data-loading check runs separately in the PR Modal image check.
These small frames test its acceptance conditions, not dataset loading.
"""

from dataclasses import replace

import numpy as np
import pandas as pd
import pytest

from policyengine_simulation_executor.simulation_runtime import DatasetSelection
from src.modal.us_regional_dataset_check import (
    SPM_MEASUREMENT_SCOPE_RULE,
    USSPMMeasurementCheck,
    summarize_us_regional_calculation,
    summarize_us_spm_measurement,
)

SOURCE_SHA256 = "a" * 64


@pytest.fixture
def selection() -> DatasetSelection:
    return DatasetSelection(
        name="populace_us_2024_acs_local",
        uri="hf://policyengine/populace-us/acs.h5@fixture-revision",
        is_default=False,
        artifact_revision="fixture-revision",
        sha256=SOURCE_SHA256,
    )


@pytest.fixture
def spm_measurement() -> USSPMMeasurementCheck:
    return USSPMMeasurementCheck(
        scope_rule=SPM_MEASUREMENT_SCOPE_RULE,
        model_mode="override",
        outside_spm_unit_count=1,
        measured_spm_unit_count=2,
        spm_poverty_rate=0.1,
        deep_spm_poverty_rate=0.03,
    )


@pytest.fixture
def frames() -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    return (
        pd.DataFrame({"takes_up_wic_if_eligible": [True, False, True]}),
        pd.DataFrame({"state_fips": [49], "household_net_income": [100.0]}),
        pd.DataFrame({"takes_up_wic_if_eligible": [True, False], "wic": [10.0, 0.0]}),
    )


def test_reports_selected_source_and_nonempty_utah_calculation(
    selection, frames, spm_measurement
):
    report = summarize_us_regional_calculation(
        selection,
        *frames,
        year=2026,
        policyengine_version="fixture-version",
        spm_measurement=spm_measurement,
    )

    assert report.dataset == selection.name
    assert report.source_sha256 == selection.sha256
    assert report.artifact_revision == "fixture-revision"
    assert report.prepared_person_count == 3
    assert report.household_count == 1
    assert report.person_count == 2
    assert report.spm_measurement == spm_measurement


@pytest.mark.parametrize(
    "change", [{"is_default": True}, {"sha256": None}, {"artifact_revision": None}]
)
def test_refuses_national_or_unpinned_source(
    selection, frames, spm_measurement, change
):
    with pytest.raises(ValueError, match="non-default certified"):
        summarize_us_regional_calculation(
            replace(selection, **change),
            *frames,
            year=2026,
            policyengine_version="fixture-version",
            spm_measurement=spm_measurement,
        )


@pytest.mark.parametrize("scope", [0, 2])
def test_rejects_missing_prepared_or_calculated_wic_decisions(
    selection, frames, spm_measurement, scope
):
    frames[scope]["takes_up_wic_if_eligible"] = frames[scope][
        "takes_up_wic_if_eligible"
    ].astype("boolean")
    frames[scope].loc[0, "takes_up_wic_if_eligible"] = None
    with pytest.raises(ValueError, match="WIC participation"):
        summarize_us_regional_calculation(
            selection,
            *frames,
            year=2026,
            policyengine_version="fixture-version",
            spm_measurement=spm_measurement,
        )


@pytest.mark.parametrize(
    "frame_index,column", [(1, "household_net_income"), (2, "wic")]
)
@pytest.mark.parametrize("invalid", [np.nan, np.inf, -np.inf])
def test_rejects_nonfinite_calculated_values(
    selection, frames, spm_measurement, frame_index, column, invalid
):
    frames[frame_index].loc[0, column] = invalid
    with pytest.raises(ValueError, match="finite"):
        summarize_us_regional_calculation(
            selection,
            *frames,
            year=2026,
            policyengine_version="fixture-version",
            spm_measurement=spm_measurement,
        )


def test_refuses_a_calculation_that_was_not_scoped_to_utah(
    selection, frames, spm_measurement
):
    frames[1].loc[0, "state_fips"] = 6
    with pytest.raises(ValueError, match="Utah"):
        summarize_us_regional_calculation(
            selection,
            *frames,
            year=2026,
            policyengine_version="fixture-version",
            spm_measurement=spm_measurement,
        )


@pytest.mark.parametrize("scope", [0, 2])
def test_rejects_a_missing_participation_column(
    selection, frames, spm_measurement, scope
):
    frames[scope].drop(columns="takes_up_wic_if_eligible", inplace=True)
    with pytest.raises(ValueError, match="WIC participation"):
        summarize_us_regional_calculation(
            selection,
            *frames,
            year=2026,
            policyengine_version="fixture-version",
            spm_measurement=spm_measurement,
        )


def test_rejects_missing_household_geography(selection, frames, spm_measurement):
    frames[1]["state_fips"] = pd.Series([pd.NA], dtype="Int64")
    with pytest.raises(ValueError, match="Utah"):
        summarize_us_regional_calculation(
            selection,
            *frames,
            year=2026,
            policyengine_version="fixture-version",
            spm_measurement=spm_measurement,
        )


# --- SPM measurement on the ACS-local source (PolicyEngine/policyengine.py#597)


@pytest.fixture
def spm_unit() -> pd.DataFrame:
    """Two measured units and one group-quarters unit outside the measurement."""
    return pd.DataFrame(
        {
            "spm_unit_is_in_spm_poverty": [1.0, 0.0, np.nan],
            "spm_unit_is_in_deep_spm_poverty": [0.0, 0.0, np.nan],
            "spm_unit_net_income": [9_000.0, 60_000.0, np.nan],
        }
    )


def _record(rule=SPM_MEASUREMENT_SCOPE_RULE, sha256=SOURCE_SHA256, mode="override"):
    """The parts of .py's legacy SPM declarations record the check reads."""
    return {
        "profile": {"source": {"spm_universe_rule": rule, "source_sha256": sha256}},
        "model_mode": mode,
    }


@pytest.fixture
def scope_record() -> dict:
    return _record()


def _measure(spm_unit, scope_record, rates=None):
    return summarize_us_spm_measurement(
        spm_unit,
        rates or {"spm": 0.1, "spm_deep": 0.03},
        scope_record,
        source_sha256=SOURCE_SHA256,
    )


def test_reports_measured_and_outside_units(spm_unit, scope_record):
    report = _measure(spm_unit, scope_record)
    assert (report.outside_spm_unit_count, report.measured_spm_unit_count) == (1, 2)
    assert (report.spm_poverty_rate, report.deep_spm_poverty_rate) == (0.1, 0.03)
    assert report.model_mode == "override"


@pytest.mark.parametrize(
    "record",
    [
        None,
        {},
        _record(rule="other"),
        _record(sha256="b" * 64),
        _record(mode=None),
    ],
)
def test_requires_the_scope_record_for_the_selected_source(spm_unit, record):
    with pytest.raises(ValueError, match="measurement scope"):
        _measure(spm_unit, record)


def test_requires_group_quarters_units_outside_the_measurement(spm_unit, scope_record):
    spm_unit.loc[2] = [0.0, 0.0, 10_000.0]
    with pytest.raises(ValueError, match="group-quarters"):
        _measure(spm_unit, scope_record)


@pytest.mark.parametrize(
    "column", ["spm_unit_is_in_deep_spm_poverty", "spm_unit_net_income"]
)
def test_outside_units_must_have_no_spm_outcomes(spm_unit, scope_record, column):
    spm_unit.loc[2, column] = 0.0
    with pytest.raises(ValueError, match="outside the measurement must be missing"):
        _measure(spm_unit, scope_record)


@pytest.mark.parametrize("invalid", [np.nan, np.inf])
def test_measured_units_must_have_finite_outcomes(spm_unit, scope_record, invalid):
    spm_unit.loc[0, "spm_unit_net_income"] = invalid
    with pytest.raises(ValueError, match="measured units must be finite"):
        _measure(spm_unit, scope_record)


def test_measured_indicators_must_be_zero_or_one(spm_unit, scope_record):
    spm_unit.loc[0, "spm_unit_is_in_spm_poverty"] = 0.5
    with pytest.raises(ValueError, match="0 or 1"):
        _measure(spm_unit, scope_record)


def test_boolean_indicators_cannot_represent_the_measurement(spm_unit, scope_record):
    spm_unit["spm_unit_is_in_spm_poverty"] = [True, False, False]
    with pytest.raises(ValueError, match="floats"):
        _measure(spm_unit, scope_record)


@pytest.mark.parametrize(
    "rates",
    [
        {"spm": 0.1},
        {"spm": np.nan, "spm_deep": 0.0},
        {"spm": 0.1, "spm_deep": 0.2},
    ],
)
def test_rejects_invalid_poverty_rates(spm_unit, scope_record, rates):
    with pytest.raises(ValueError, match="poverty rates"):
        _measure(spm_unit, scope_record, rates)


@pytest.mark.parametrize("rate", [0.0, 1.0])
def test_rejects_degenerate_poverty_rates(spm_unit, scope_record, rate):
    with pytest.raises(ValueError):
        _measure(spm_unit, scope_record, {"spm": rate, "spm_deep": 0.0})
