"""Tests for the calculations of one household report run, as one call takes them."""

from __future__ import annotations

from uuid import UUID

import pytest
from policyengine_simulation_contract.household_execution import (
    HouseholdBundleProvenance,
    HouseholdExecutionRequest,
)
from policyengine_simulation_contract.household_report_execution import (
    HouseholdReportRunCalculation,
)
from pydantic import ValidationError

BASELINE_RUN_ID = UUID("00000000-0000-4000-8000-0000000000b1")
REFORM_RUN_ID = UUID("00000000-0000-4000-8000-0000000000b2")
HOUSEHOLD = {
    "people": {"you": {"age": {"2026": 40}, "employment_income": {"2026": 50_000}}},
    "households": {
        "your household": {
            "members": ["you"],
            "household_net_income": {"2026": None},
        }
    },
}
BUNDLE = HouseholdBundleProvenance(
    policyengine_version="6.17.1",
    core_package_version="3.33.0",
    country_package_name="policyengine-us",
    country_package_version="1.764.6",
    bundle_manifest_sha256="a" * 64,
)
CTC_REFORM = {
    "gov.irs.credits.ctc.amount.base[0].amount": {"2026-01-01.2200-12-31": 3_000}
}


def _request(calculation_id: UUID, **changes: object) -> dict[str, object]:
    """Return one side's household function request as JSON."""
    return {
        "calculation_id": str(calculation_id),
        "country": "us",
        "year": "2026",
        "household": HOUSEHOLD,
        "policy": {"parameters": {}},
        "add_missing_variables": True,
        "bundle": BUNDLE.model_dump(mode="json"),
        **changes,
    }


def _calculation(
    *, reform: dict[str, object] | None = None
) -> HouseholdReportRunCalculation:
    return HouseholdReportRunCalculation.model_validate(
        {"baseline": _request(BASELINE_RUN_ID), "reform": reform}
    )


def test_a_report_run_calculates_its_baseline_then_its_reform() -> None:
    calculation = _calculation(
        reform=_request(REFORM_RUN_ID, policy={"parameters": CTC_REFORM})
    )

    sides = calculation.sides()

    assert [side for side, _ in sides] == ["baseline", "reform"]
    assert [request.calculation_id for _, request in sides] == [
        BASELINE_RUN_ID,
        REFORM_RUN_ID,
    ]
    assert all(isinstance(request, HouseholdExecutionRequest) for _, request in sides)
    assert sides[1][1].policy.parameters == CTC_REFORM


def test_a_report_run_of_the_baseline_alone_calculates_one_side() -> None:
    calculation = _calculation()

    assert calculation.reform is None
    assert [side for side, _ in calculation.sides()] == ["baseline"]


def test_a_calculation_round_trips_through_json() -> None:
    calculation = _calculation(reform=_request(REFORM_RUN_ID))

    payload = calculation.model_dump(mode="json")

    assert payload["schema_version"] == 1
    assert HouseholdReportRunCalculation.model_validate(payload) == calculation


def test_each_side_is_its_own_calculation() -> None:
    with pytest.raises(ValidationError, match="different simulation runs"):
        _calculation(reform=_request(BASELINE_RUN_ID))


@pytest.mark.parametrize(
    "reform_change",
    [
        {"year": "2027"},
        {"household": {"people": {"you": {"age": {"2026": 41}}}}},
        {"add_missing_variables": False},
        {
            "bundle": {
                **BUNDLE.model_dump(mode="json"),
                "bundle_manifest_sha256": "b" * 64,
            }
        },
    ],
    ids=["year", "household", "add_missing_variables", "bundle"],
)
def test_both_sides_calculate_one_household_the_same_way(
    reform_change: dict[str, object],
) -> None:
    with pytest.raises(ValidationError, match="the same household"):
        _calculation(reform=_request(REFORM_RUN_ID, **reform_change))


def test_unknown_fields_are_refused() -> None:
    with pytest.raises(ValidationError, match="extra"):
        HouseholdReportRunCalculation.model_validate(
            {"baseline": _request(BASELINE_RUN_ID), "report_run_id": "x"}
        )
