"""Tests for a local-area report run's area results as result-table rows."""

from __future__ import annotations

from typing import Any
from uuid import UUID

import pytest
from policyengine_simulation_contract.stage12_execution import ReportRunIds
from policyengine_stage12_persistence.local_area_results import local_area_result
from policyengine_stage12_persistence.report_run_results import (
    constituency_impacts,
    local_authority_impacts,
    withheld_area_codes,
)
from pydantic import ValidationError

RUN_IDS = ReportRunIds(
    report_run_id=UUID("00000000-0000-0000-0000-00000000000a"),
    baseline_simulation_run_id=UUID("00000000-0000-0000-0000-00000000000b"),
    reform_simulation_run_id=UUID("00000000-0000-0000-0000-00000000000c"),
)
RUN_KEYS = {
    "report_run_id": RUN_IDS.report_run_id,
    "baseline_simulation_run_id": RUN_IDS.baseline_simulation_run_id,
    "reform_simulation_run_id": RUN_IDS.reform_simulation_run_id,
}
DISCLOSURE = {
    "disclosure_tier": "reported",
    "average_household_income_change_lower": -12.0,
    "average_household_income_change_upper": -8.0,
    "relative_household_income_change_lower": -0.0004,
    "relative_household_income_change_upper": -0.0002,
}


def _area_output(**changes: Any) -> dict[str, Any]:
    """Return a local-area result with two constituencies and one authority."""
    return {
        "kind": "local_areas",
        "model_version": "2.174.1",
        "policyengine_version": "6.17.1",
        "dataset": "microcosm_uk_2024_25_dense",
        "data_version": "microcosm-uk-2024-25-dense-20261107T173210Z-38605444",
        "constituency_impact": [
            {
                "constituency_code": "E14001063",
                "constituency_name": "Aldershot",
                "average_household_income_change": -10.0,
                "relative_household_income_change": -0.0003,
                "population": 41_000.0,
                **DISCLOSURE,
            },
            {
                "constituency_code": "W07000081",
                "constituency_name": "Aberafan Maesteg",
                "average_household_income_change": -9.0,
                "relative_household_income_change": -0.0003,
                "population": 38_000.0,
                **DISCLOSURE,
                "disclosure_tier": "approximate",
            },
        ],
        "local_authority_impact": [
            {
                "local_authority_code": "E06000063",
                "local_authority_name": "Cumberland",
                "average_household_income_change": -11.0,
                "relative_household_income_change": -0.0004,
                "population": 120_000.0,
                **DISCLOSURE,
            }
        ],
        "constituency_withheld_codes": ["S14000045"],
        "local_authority_withheld_codes": ["E06000064", "W06000015"],
        **changes,
    }


def _rows_by_table(output: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    result = local_area_result(output, run_ids=RUN_IDS)
    return {
        rows.table.name: [
            {name: value for name, value in row.items() if name != "id"}
            for row in rows.rows
        ]
        for rows in result.rows
    }


def test_a_local_area_result_stores_its_areas_and_withheld_codes() -> None:
    rows = _rows_by_table(_area_output())

    assert set(rows) == {
        constituency_impacts.name,
        local_authority_impacts.name,
        withheld_area_codes.name,
    }
    assert rows["constituency_impacts"] == [
        {
            **RUN_KEYS,
            "constituency_code": "E14001063",
            "constituency_name": "Aldershot",
            "average_household_income_change": -10.0,
            "relative_household_income_change": -0.0003,
            "population": 41_000.0,
            **DISCLOSURE,
        },
        {
            **RUN_KEYS,
            "constituency_code": "W07000081",
            "constituency_name": "Aberafan Maesteg",
            "average_household_income_change": -9.0,
            "relative_household_income_change": -0.0003,
            "population": 38_000.0,
            **DISCLOSURE,
            "disclosure_tier": "approximate",
        },
    ]
    assert rows["local_authority_impacts"] == [
        {
            **RUN_KEYS,
            "local_authority_code": "E06000063",
            "local_authority_name": "Cumberland",
            "average_household_income_change": -11.0,
            "relative_household_income_change": -0.0004,
            "population": 120_000.0,
            **DISCLOSURE,
        }
    ]
    assert rows["withheld_area_codes"] == [
        {**RUN_KEYS, "area_type": "constituency", "code": "S14000045"},
        {**RUN_KEYS, "area_type": "local_authority", "code": "E06000064"},
        {**RUN_KEYS, "area_type": "local_authority", "code": "W06000015"},
    ]


def test_each_row_has_its_own_id() -> None:
    result = local_area_result(_area_output(), run_ids=RUN_IDS)

    ids = [row["id"] for rows in result.rows for row in rows.rows]
    assert len(ids) == len(set(ids)) == 6


def test_a_local_area_result_sets_only_the_runs_data_version() -> None:
    result = local_area_result(_area_output(), run_ids=RUN_IDS)

    assert result.report_run_columns == {
        "data_version": "microcosm-uk-2024-25-dense-20261107T173210Z-38605444"
    }
    assert not any(rows.shared_by_reports for rows in result.rows)


def test_a_level_with_every_area_withheld_stores_no_area_rows() -> None:
    rows = _rows_by_table(
        _area_output(
            constituency_impact=[],
            constituency_withheld_codes=["E14001063", "W07000081"],
        )
    )

    assert rows["constituency_impacts"] == []
    assert [row["code"] for row in rows["withheld_area_codes"]] == [
        "E14001063",
        "W07000081",
        "E06000064",
        "W06000015",
    ]


@pytest.mark.parametrize(
    "missing", ["constituency_withheld_codes", "local_authority_withheld_codes"]
)
def test_a_local_area_result_without_its_withheld_codes_is_refused(
    missing: str,
) -> None:
    # A Stage 12 run always lists them; null means an older result format,
    # where "none withheld" and "not reported" can't be told apart.
    output = _area_output()
    del output[missing]

    with pytest.raises(ValueError, match="withheld"):
        local_area_result(output, run_ids=RUN_IDS)


def test_a_national_economy_result_is_refused() -> None:
    with pytest.raises(ValidationError, match="kind"):
        local_area_result({"budget": {}}, run_ids=RUN_IDS)
