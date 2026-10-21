"""UK area records stay readable before and after disclosure control."""

from __future__ import annotations

import pytest
from policyengine_simulation_contract.macro_output import (
    ConstituencyImpactRecord,
    GeographicImpactOutput,
    LocalAuthorityImpactRecord,
)
from pydantic import ValidationError

STORED_BEFORE_DISCLOSURE = {
    "constituency_code": "E14001063",
    "constituency_name": "Aldershot",
    "x": 56,
    "y": -40,
    "average_household_income_change": -150.95,
    "relative_household_income_change": -0.0026,
    "population": 40_530.65,
}
DISCLOSURE_FIELDS = {
    "disclosure_tier": "approximate",
    "average_household_income_change_lower": -170.0,
    "average_household_income_change_upper": -130.0,
    "relative_household_income_change_lower": -0.003,
    "relative_household_income_change_upper": -0.0022,
}


def test_records_stored_before_disclosure_control_still_validate() -> None:
    record = ConstituencyImpactRecord.model_validate(STORED_BEFORE_DISCLOSURE)

    assert record.disclosure_tier is None
    assert record.average_household_income_change_lower is None
    assert record.relative_household_income_change_upper is None


def test_records_carry_the_disclosure_tier_and_intervals() -> None:
    record = LocalAuthorityImpactRecord.model_validate(
        {
            "local_authority_code": "E06000063",
            "local_authority_name": "Cumberland",
            "x": 4,
            "y": 18,
            "average_household_income_change": -150.0,
            "relative_household_income_change": -0.0026,
            "population": 120_000.0,
            **DISCLOSURE_FIELDS,
        }
    )

    assert record.model_dump(mode="json")["disclosure_tier"] == "approximate"
    assert record.average_household_income_change_lower == -170.0


def test_a_withheld_tier_is_not_a_record() -> None:
    # Withheld areas are left out of the list, so no record holds that tier.
    with pytest.raises(ValidationError):
        ConstituencyImpactRecord.model_validate(
            {**STORED_BEFORE_DISCLOSURE, "disclosure_tier": "withheld"}
        )


def test_published_values_stay_required() -> None:
    record = {**STORED_BEFORE_DISCLOSURE, **DISCLOSURE_FIELDS}
    record["average_household_income_change"] = None

    with pytest.raises(ValidationError):
        ConstituencyImpactRecord.model_validate(record)


def test_an_empty_area_list_is_valid() -> None:
    assert GeographicImpactOutput.model_validate([]).root == []
