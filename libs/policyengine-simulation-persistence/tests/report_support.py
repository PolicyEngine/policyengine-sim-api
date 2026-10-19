"""A valid single-year economy aggregate for the report tests."""

from __future__ import annotations

from copy import deepcopy


def _baseline_reform(baseline: float, reform: float) -> dict[str, float]:
    return {"baseline": baseline, "reform": reform}


_AGE_GROUPS = {
    "child": _baseline_reform(0.12, 0.11),
    "adult": _baseline_reform(0.1, 0.1),
    "senior": _baseline_reform(0.08, 0.08),
    "all": _baseline_reform(0.1, 0.09),
}

ECONOMY_AGGREGATE = {
    "spm_provenance": None,
    "model_version": "1.715.2",
    "data_version": "1.115.5",
    "budget": {
        "budgetary_impact": 70.0,
        "tax_revenue_impact": 100.0,
        "state_tax_revenue_impact": 20.0,
        "benefit_spending_impact": 30.0,
        "households": 2.0,
        "baseline_net_income": 1000.0,
    },
    "detailed_budget": {
        "income_tax": {"baseline": 100.0, "reform": 125.0, "difference": 25.0}
    },
    "decile": {"relative": {"1": 0.01, "2": 0.02}, "average": {"1": 10.0, "2": 20.0}},
    "inequality": {
        "gini": _baseline_reform(0.4, 0.39),
        "top_10_pct_share": _baseline_reform(0.3, 0.29),
        "top_1_pct_share": _baseline_reform(0.1, 0.09),
    },
    "poverty": {"poverty": _AGE_GROUPS, "deep_poverty": _AGE_GROUPS},
    "poverty_by_gender": {
        "poverty": {
            "male": _baseline_reform(0.08, 0.07),
            "female": _baseline_reform(0.09, 0.08),
        },
        "deep_poverty": {
            "male": _baseline_reform(0.02, 0.02),
            "female": _baseline_reform(0.03, 0.02),
        },
    },
    "poverty_by_race": None,
    "intra_decile": {
        "all": {"Gain more than 5%": 0.3, "No change": 0.7},
        "deciles": {"Gain more than 5%": [0.5, 0.1], "No change": [0.5, 0.9]},
    },
    "wealth_decile": None,
    "intra_wealth_decile": None,
    "labor_supply_response": None,
    "constituency_impact": None,
    "local_authority_impact": None,
    "congressional_district_impact": None,
    "cliff_impact": None,
}


def economy_aggregate(**overrides) -> dict:
    """Return a copy of the aggregate with top-level fields replaced."""
    return deepcopy(ECONOMY_AGGREGATE) | overrides
