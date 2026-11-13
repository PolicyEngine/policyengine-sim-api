"""Tests for the withheld area codes of a UK result, as result-table rows."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from economy_output import (
    uk_economy_output,
    uk_output_with_areas,
    uk_output_with_withheld_codes,
)
from policyengine_simulation_contract.macro_output import SingleYearMacroOutput
from policyengine_simulation_contract.stage12_execution import ReportRunIds
from policyengine_stage12_persistence.report_run_results import (
    report_run_result,
    withheld_area_codes,
)

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


def _withheld_rows(output: dict[str, Any]) -> list[dict[str, Any]]:
    result = report_run_result(output, run_ids=RUN_IDS, country="uk")
    (rows,) = [rows for rows in result.rows if rows.table is withheld_area_codes]
    assert rows.shared_by_reports is False
    return [
        {name: value for name, value in row.items() if name != "id"}
        for row in rows.rows
    ]


def test_a_uk_result_stores_one_row_per_withheld_area() -> None:
    assert _withheld_rows(uk_output_with_withheld_codes()) == [
        {**RUN_KEYS, "area_type": "constituency", "code": "S14000045"},
        {**RUN_KEYS, "area_type": "local_authority", "code": "E06000064"},
        {**RUN_KEYS, "area_type": "local_authority", "code": "W06000015"},
    ]


def test_withheld_rows_have_their_own_ids() -> None:
    output = {
        **uk_output_with_areas(),
        "constituency_withheld_codes": ["S14000045", "S14000046"],
        "local_authority_withheld_codes": [],
    }
    result = report_run_result(output, run_ids=RUN_IDS, country="uk")

    (rows,) = [rows for rows in result.rows if rows.table is withheld_area_codes]
    assert len({row["id"] for row in rows.rows}) == 2


def test_a_result_that_lists_no_withheld_codes_stores_none() -> None:
    assert _withheld_rows(uk_output_with_areas()) == []


def test_a_result_without_withheld_codes_dumps_as_before() -> None:
    # National results, and every result calculated before the field, are
    # written and compared exactly as they were.
    dumped = SingleYearMacroOutput.model_validate(uk_economy_output()).model_dump(
        mode="json"
    )

    assert "constituency_withheld_codes" not in dumped
    assert "local_authority_withheld_codes" not in dumped


def test_a_result_with_withheld_codes_keeps_them() -> None:
    output = {
        **uk_output_with_areas(),
        "constituency_withheld_codes": [],
        "local_authority_withheld_codes": ["E06000064"],
    }

    dumped = SingleYearMacroOutput.model_validate(output).model_dump(mode="json")

    assert dumped["constituency_withheld_codes"] == []
    assert dumped["local_authority_withheld_codes"] == ["E06000064"]
