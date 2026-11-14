"""A budget-window report request: one region's baseline and reform over many years."""

from __future__ import annotations

from typing import Any

import pytest
from economy_service_fakes import REPORT_BODY
from policyengine_simulation_entry.budget_window_api_models import (
    BudgetWindowReportRequest,
)
from pydantic import ValidationError

WINDOW_BODY: dict[str, Any] = {
    **{name: value for name, value in REPORT_BODY.items() if name != "include_cliffs"},
    "report_type": "budget_window",
    "budget_window_size": 10,
}


def _request(**changes: Any) -> BudgetWindowReportRequest:
    return BudgetWindowReportRequest.model_validate({**WINDOW_BODY, **changes})


def test_a_window_names_its_years_from_its_first() -> None:
    request = _request()

    assert request.years == tuple(range(2026, 2036))


def test_a_windows_calculation_options_ask_for_no_cliffs() -> None:
    # The report stores the SPM selection alone (stored_options); its years
    # share runs with single-year reports by key.
    assert _request().options() == {
        "include_cliffs": False,
        "spm": {"geography_kind": "national"},
    }
    without_spm = {name: value for name, value in WINDOW_BODY.items() if name != "spm"}
    assert BudgetWindowReportRequest.model_validate(without_spm).options() == {
        "include_cliffs": False
    }


@pytest.mark.parametrize(
    "changes",
    [
        {"include_cliffs": True},
        {"include_cliffs": False},
        {"budget_window_size": 0},
        {"budget_window_size": 76},
        {"year": 2195, "budget_window_size": 10},
        {"report_type": "economy"},
        {"spm": None},
    ],
    ids=[
        "cliffs",
        "cliffs-false",
        "no-years",
        "76-years",
        "past-2200",
        "economy-type",
        "null-spm",
    ],
)
def test_a_window_request_is_refused_when(changes) -> None:
    with pytest.raises(ValidationError):
        _request(**changes)


def test_a_window_of_one_year_ending_in_2200_is_accepted() -> None:
    assert _request(year=2200, budget_window_size=1).years == (2200,)
