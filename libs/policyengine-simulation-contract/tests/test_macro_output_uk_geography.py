"""UK area records stay readable before and after disclosure control."""

from __future__ import annotations

import json

import pytest
from policyengine_simulation_contract.macro_output import (
    ConstituencyImpactRecord,
    GeographicImpactOutput,
    LocalAuthorityImpactRecord,
)
from pydantic import ValidationError

RECORD_WITHOUT_DISCLOSURE = {
    "constituency_code": "E14001063",
    "constituency_name": "Aldershot",
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


def test_records_without_disclosure_fields_still_validate() -> None:
    record = ConstituencyImpactRecord.model_validate(RECORD_WITHOUT_DISCLOSURE)

    assert record.disclosure_tier is None
    assert record.average_household_income_change_lower is None
    assert record.relative_household_income_change_upper is None


def test_records_carry_the_disclosure_tier_and_intervals() -> None:
    record = LocalAuthorityImpactRecord.model_validate(
        {
            "local_authority_code": "E06000063",
            "local_authority_name": "Cumberland",
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
            {**RECORD_WITHOUT_DISCLOSURE, "disclosure_tier": "withheld"}
        )


def test_published_values_stay_required() -> None:
    record = {**RECORD_WITHOUT_DISCLOSURE, **DISCLOSURE_FIELDS}
    record["average_household_income_change"] = None

    with pytest.raises(ValidationError):
        ConstituencyImpactRecord.model_validate(record)


def test_an_empty_area_list_is_valid() -> None:
    assert GeographicImpactOutput.model_validate([]).root == []


# The records have no x or y (an area's integer map position). Apps deployed
# for older policyengine.py versions still send both, and their records are
# refused, so this contract is deployed only once no such app is routed.
CONSTITUENCY_WITHOUT_MAP_POSITION = {**RECORD_WITHOUT_DISCLOSURE, **DISCLOSURE_FIELDS}
LOCAL_AUTHORITY_WITHOUT_MAP_POSITION = {
    "local_authority_code": "E06000063",
    "local_authority_name": "Cumberland",
    "average_household_income_change": -150.0,
    "relative_household_income_change": -0.0026,
    "population": 120_000.0,
    **DISCLOSURE_FIELDS,
}
AREA_RECORDS_WITHOUT_MAP_POSITION = [
    pytest.param(
        ConstituencyImpactRecord, CONSTITUENCY_WITHOUT_MAP_POSITION, id="constituency"
    ),
    pytest.param(
        LocalAuthorityImpactRecord,
        LOCAL_AUTHORITY_WITHOUT_MAP_POSITION,
        id="local-authority",
    ),
]


@pytest.mark.parametrize(("record_type", "payload"), AREA_RECORDS_WITHOUT_MAP_POSITION)
def test_records_validate_and_dump_without_x_or_y(
    record_type: type[ConstituencyImpactRecord | LocalAuthorityImpactRecord],
    payload: dict[str, object],
) -> None:
    record = record_type.model_validate(payload)

    assert {"x", "y"}.isdisjoint(record_type.model_fields)
    assert record.model_dump(mode="python") == payload
    assert record.model_dump(mode="json") == payload
    assert json.loads(record.model_dump_json()) == payload


@pytest.mark.parametrize(("record_type", "payload"), AREA_RECORDS_WITHOUT_MAP_POSITION)
@pytest.mark.parametrize(
    "map_position",
    [
        pytest.param({"x": 56, "y": -40}, id="both"),
        pytest.param({"x": 56}, id="x-only"),
        pytest.param({"y": -40}, id="y-only"),
        pytest.param({"x": None, "y": None}, id="null"),
    ],
)
def test_records_with_x_or_y_are_refused(
    record_type: type[ConstituencyImpactRecord | LocalAuthorityImpactRecord],
    payload: dict[str, object],
    map_position: dict[str, int | None],
) -> None:
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        record_type.model_validate({**payload, **map_position})


def test_an_area_list_without_map_positions_dumps_without_them() -> None:
    payload = [CONSTITUENCY_WITHOUT_MAP_POSITION, LOCAL_AUTHORITY_WITHOUT_MAP_POSITION]

    output = GeographicImpactOutput.model_validate(payload)

    assert [type(record) for record in output.root] == [
        ConstituencyImpactRecord,
        LocalAuthorityImpactRecord,
    ]
    assert output.model_dump(mode="json") == payload
    assert json.loads(output.model_dump_json()) == payload
