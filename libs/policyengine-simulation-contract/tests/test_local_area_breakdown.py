"""The local-area breakdown result and the worker capability that serves it."""

from __future__ import annotations

from copy import deepcopy

import pytest
from policyengine_simulation_contract.gateway_models import JobStatusResponse
from policyengine_simulation_contract.local_area_breakdown import (
    LOCAL_AREA_BREAKDOWN_CAPABILITY_KEY,
    LOCAL_AREA_BREAKDOWN_CAPABILITY_VERSION,
    local_area_breakdown_error,
    supported_local_area_breakdown_capability,
)
from policyengine_simulation_contract.macro_output import (
    LocalAreaBreakdownOutput,
    SingleYearMacroOutput,
)
from pydantic import ValidationError

CONSTITUENCY = {
    "constituency_code": "E14001063",
    "constituency_name": "Aldershot",
    "x": 56,
    "y": -40,
    "average_household_income_change": -150.0,
    "relative_household_income_change": -0.0026,
    "population": 40_530.0,
    "disclosure_tier": "reported",
    "average_household_income_change_lower": -170.0,
    "average_household_income_change_upper": -130.0,
    "relative_household_income_change_lower": -0.003,
    "relative_household_income_change_upper": -0.0022,
}
LOCAL_AUTHORITY = {
    "local_authority_code": "E06000063",
    "local_authority_name": "Cumberland",
    "x": 4,
    "y": 18,
    "average_household_income_change": -120.0,
    "relative_household_income_change": -0.002,
    "population": 120_000.0,
    "disclosure_tier": "approximate",
    "average_household_income_change_lower": -160.0,
    "average_household_income_change_upper": -80.0,
    "relative_household_income_change_lower": -0.003,
    "relative_household_income_change_upper": -0.001,
}
BREAKDOWN = {
    "kind": "local_areas",
    "model_version": "2.132.6",
    "policyengine_version": "6.10.0",
    "dataset": "microcosm_uk_2024_25_dense",
    "data_version": "microcosm-uk-2024-25-dense-20261019T000432Z-ee5c2dbe",
    "constituency_impact": [CONSTITUENCY],
    "local_authority_impact": [LOCAL_AUTHORITY],
}
_ZERO = {"baseline": 0.0, "reform": 0.0}
_AGE_POVERTY = {"child": _ZERO, "adult": _ZERO, "senior": _ZERO, "all": _ZERO}
_GENDER_POVERTY = {"male": _ZERO, "female": _ZERO}
NATIONAL = {
    "spm_provenance": None,
    "model_version": "2.132.6",
    "data_version": "microcosm-uk-2024-25-national-20261019T083323Z-6cba5d71",
    "budget": {
        "tax_revenue_impact": 0.0,
        "state_tax_revenue_impact": 0.0,
        "benefit_spending_impact": 0.0,
        "budgetary_impact": 0.0,
        "households": 1.0,
        "baseline_net_income": 1.0,
    },
    "detailed_budget": {},
    "decile": {"average": {}, "relative": {}},
    "inequality": {
        "gini": _ZERO,
        "top_10_pct_share": _ZERO,
        "top_1_pct_share": _ZERO,
    },
    "poverty": {"poverty": _AGE_POVERTY, "deep_poverty": _AGE_POVERTY},
    "poverty_by_gender": {"poverty": _GENDER_POVERTY, "deep_poverty": _GENDER_POVERTY},
    "poverty_by_race": None,
    "intra_decile": {"deciles": {}, "all": {}},
    "wealth_decile": None,
    "intra_wealth_decile": None,
    "labor_supply_response": None,
    "constituency_impact": None,
    "local_authority_impact": None,
    "congressional_district_impact": None,
    "cliff_impact": None,
}


def test_the_capability_is_declared_under_its_own_key_at_version_one():
    assert LOCAL_AREA_BREAKDOWN_CAPABILITY_KEY == "local_area_breakdown"
    assert LOCAL_AREA_BREAKDOWN_CAPABILITY_VERSION == 1


def test_the_capability_accepts_only_version_one():
    assert supported_local_area_breakdown_capability(1) == 1


@pytest.mark.parametrize("declared", [None, 0, 2, "1", 1.0, True])
def test_the_capability_rejects_every_other_declaration(declared):
    assert supported_local_area_breakdown_capability(declared) is None


def test_a_breakdown_result_carries_both_area_levels_and_their_provenance():
    output = LocalAreaBreakdownOutput.model_validate(BREAKDOWN)

    assert output.kind == "local_areas"
    assert output.dataset == "microcosm_uk_2024_25_dense"
    assert output.model_dump(mode="json") == BREAKDOWN


def test_a_job_status_reads_a_breakdown_result_as_a_breakdown():
    response = JobStatusResponse(status="complete", result=BREAKDOWN)

    assert isinstance(response.result, LocalAreaBreakdownOutput)
    assert response.model_dump(mode="json")["result"] == BREAKDOWN


def test_a_job_status_still_reads_a_national_result_as_national():
    response = JobStatusResponse(status="complete", result=NATIONAL)

    assert isinstance(response.result, SingleYearMacroOutput)
    assert response.model_dump(mode="json")["result"] == NATIONAL


def test_a_breakdown_payload_is_never_a_national_result():
    with pytest.raises(ValidationError):
        SingleYearMacroOutput.model_validate(BREAKDOWN)


def test_a_national_payload_is_never_a_breakdown():
    with pytest.raises(ValidationError):
        LocalAreaBreakdownOutput.model_validate(NATIONAL)


def test_a_breakdown_may_publish_no_area_of_either_level():
    # Disclosure control can withhold every area of a level.
    payload = {**BREAKDOWN, "constituency_impact": [], "local_authority_impact": []}

    output = LocalAreaBreakdownOutput.model_validate(payload)

    assert output.constituency_impact.root == []
    assert output.local_authority_impact.root == []


@pytest.mark.parametrize("field", ["constituency_impact", "local_authority_impact"])
def test_a_breakdown_names_both_area_levels(field):
    payload = deepcopy(BREAKDOWN)
    payload[field] = None

    with pytest.raises(ValidationError):
        LocalAreaBreakdownOutput.model_validate(payload)


@pytest.mark.parametrize(
    "change", [{"kind": "national"}, {"budget": NATIONAL["budget"]}]
)
def test_a_breakdown_has_its_own_kind_and_no_other_fields(change):
    with pytest.raises(ValidationError):
        LocalAreaBreakdownOutput.model_validate({**BREAKDOWN, **change})


def test_an_unknown_nation_is_refused_with_the_nations_a_breakdown_takes():
    error = local_area_breakdown_error(
        country="uk",
        region="country/ni",
        region_group=None,
        include_cliffs=False,
        segmented=None,
    )

    assert error is not None
    for nation in ("england", "scotland", "wales", "northern_ireland"):
        assert f"country/{nation}" in error
