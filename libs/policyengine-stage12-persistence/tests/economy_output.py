"""A complete single-year economy output with a distinct value in each field."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from policyengine_simulation_contract.spm import (
    SPMRuntimeVersions,
    SPMSelection,
    build_spm_comparison_provenance,
    build_spm_provenance,
)

US_ECONOMY_OUTPUT: dict[str, Any] = {
    "spm_provenance": None,
    "model_version": "1.715.2",
    "data_version": "1.115.5",
    "budget": {
        "budgetary_impact": 70.25,
        "tax_revenue_impact": 100.5,
        "state_tax_revenue_impact": 20.125,
        "benefit_spending_impact": 30.25,
        "households": 2.0,
        "baseline_net_income": 1000.75,
    },
    "detailed_budget": {
        "income_tax": {
            "baseline": 100.0,
            "reform": 125.0,
            "difference": 25.0,
            "is_tax": True,
        }
    },
    "decile": {
        "relative": {"1": 0.01, "2": 0.02},
        "average": {"1": 10.0, "2": 20.0},
    },
    "inequality": {
        "gini": {"baseline": 0.4, "reform": 0.39},
        "top_10_pct_share": {"baseline": 0.3, "reform": 0.29},
        "top_1_pct_share": {"baseline": 0.1, "reform": 0.09},
    },
    "poverty": {
        "poverty": {
            "adult": {"baseline": 0.081, "reform": 0.071},
            "all": {"baseline": 0.1, "reform": 0.09},
            "child": {"baseline": 0.12, "reform": 0.11},
            "senior": {"baseline": 0.061, "reform": 0.051},
        },
        "deep_poverty": {
            "adult": {"baseline": 0.021, "reform": 0.011},
            "all": {"baseline": 0.031, "reform": 0.022},
            "child": {"baseline": 0.04, "reform": 0.03},
            "senior": {"baseline": 0.015, "reform": 0.012},
        },
    },
    "poverty_by_gender": {
        "poverty": {
            "male": {"baseline": 0.08, "reform": 0.07},
            "female": {"baseline": 0.09, "reform": 0.085},
        },
        "deep_poverty": {
            "male": {"baseline": 0.02, "reform": 0.019},
            "female": {"baseline": 0.025, "reform": 0.024},
        },
    },
    "poverty_by_race": {
        "poverty": {
            "black": {"baseline": 0.15, "reform": 0.14},
            "hispanic": {"baseline": 0.13, "reform": 0.125},
            "other": {"baseline": 0.11, "reform": 0.105},
            "white": {"baseline": 0.06, "reform": 0.05},
        },
    },
    "intra_decile": {
        "all": {
            "Gain less than 5%": 0.30000000000000004,
            "Gain more than 5%": 0.3,
            "Lose less than 5%": 0.15000000000000002,
            "Lose more than 5%": 0.05,
            "No change": 0.44999999999999996,
        },
        "deciles": {
            "Gain less than 5%": [0.4, 0.2],
            "Gain more than 5%": [0.5, 0.1],
            "Lose less than 5%": [0.2, 0.1],
            "Lose more than 5%": [0.1, 0.0],
            "No change": [0.3, 0.6],
        },
    },
    "wealth_decile": None,
    "intra_wealth_decile": None,
    "labor_supply_response": {
        "substitution_lsr": 1.5,
        "income_lsr": -0.5,
        "relative_lsr": {"income": -0.01, "substitution": 0.02},
        "total_change": 1.0,
        "revenue_change": 3.0,
        "decile": {
            "average": {"income": {"1": -0.1}, "substitution": {"1": 0.2}},
            "relative": {"income": {"1": -0.001}, "substitution": {"1": 0.002}},
        },
        "hours": {
            "baseline": 100.0,
            "reform": 101.0,
            "change": 1.0,
            "income_effect": -0.5,
            "substitution_effect": 1.5,
        },
    },
    "constituency_impact": None,
    "local_authority_impact": None,
    "congressional_district_impact": None,
    "cliff_impact": {
        "baseline": {"cliff_gap": 10.0, "cliff_share": 0.1},
        "reform": {"cliff_gap": 9.0, "cliff_share": 0.09},
    },
}


def uk_economy_output() -> dict[str, Any]:
    """Return the output as the UK reports it: no race breakdown, with wealth parts."""
    output = deepcopy(US_ECONOMY_OUTPUT)
    output["poverty_by_race"] = None
    output["budget"]["state_tax_revenue_impact"] = 0.0
    output["intra_wealth_decile"] = deepcopy(output["intra_decile"])
    wealth = output["intra_wealth_decile"]
    wealth["deciles"]["No change"] = [0.4, 0.6]
    # Today's builder makes each overall share the mean of the decile shares.
    wealth["all"]["No change"] = (0.4 + 0.6) / 2
    return output


US_STATE_FIPS = {"AL": 1, "AK": 2, "CA": 6}
US_AT_LARGE_STATES = frozenset({"AK"})


def us_output_with_every_part() -> dict[str, Any]:
    """Return the US output with districts, SPM receipts and an unflagged program."""
    receipt = build_spm_provenance(
        forecast_id="forecast-2026",
        forecast_sha256="a" * 64,
        selection=SPMSelection(
            forecast_content_sha256="a" * 64,
            scenario="ce_trend",
            geography_kind="national",
            geography_id=None,
            county_vintage="2020",
            as_of=None,
        ),
        years=("2026",),
        runtime_versions=SPMRuntimeVersions.model_validate(
            {
                "policyengine": "0.0.0-test-policyengine",
                "policyengine-core": "0.0.0-test-policyengine-core",
                "policyengine-us": "0.0.0-test-policyengine-us",
                "spm-calculator": "0.0.0-test-spm-calculator",
            }
        ),
    )
    output = deepcopy(US_ECONOMY_OUTPUT)
    output["spm_provenance"] = build_spm_comparison_provenance(
        baseline_receipts=[receipt], reform_receipts=[receipt]
    ).model_dump(mode="json", by_alias=True)
    output["detailed_budget"]["snap"] = {
        "baseline": 80.0,
        "reform": 70.0,
        "difference": -10.0,
        "is_tax": None,
    }
    output["congressional_district_impact"] = {
        "districts": [
            {
                "district": district,
                "average_household_income_change": 10.0 + index,
                "relative_household_income_change": 0.01 * (index + 1),
                "winner_percentage": 0.6 - index / 10,
                "loser_percentage": 0.3,
                "no_change_percentage": 0.1 + index / 10,
                "population": 1000.0 + index,
            }
            for index, district in enumerate(("AL-01", "AL-07", "AK-01", "CA-12"))
        ]
    }
    return output


def uk_output_with_areas() -> dict[str, Any]:
    """Return the UK output with constituencies and local authorities."""
    output = uk_economy_output()
    output["constituency_impact"] = [
        {
            "constituency_code": "E14001063",
            "constituency_name": "Aldershot",
            "average_household_income_change": 120.0,
            "relative_household_income_change": 0.004,
            "population": 45000.0,
            "disclosure_tier": "reported",
            "average_household_income_change_lower": 100.0,
            "average_household_income_change_upper": 140.0,
            "relative_household_income_change_lower": 0.003,
            "relative_household_income_change_upper": 0.005,
        },
        {
            "constituency_code": "W07000081",
            "constituency_name": "Aberafan Maesteg",
            "average_household_income_change": 90.0,
            "relative_household_income_change": 0.003,
            "population": 40000.0,
        },
    ]
    output["local_authority_impact"] = [
        {
            "local_authority_code": "E06000001",
            "local_authority_name": "Hartlepool",
            "average_household_income_change": 80.0,
            "relative_household_income_change": 0.002,
            "population": 42000.0,
            "disclosure_tier": "approximate",
            "average_household_income_change_lower": 50.0,
            "average_household_income_change_upper": 110.0,
            "relative_household_income_change_lower": 0.001,
            "relative_household_income_change_upper": 0.003,
        }
    ]
    return output
