"""Fixtures for simulation API contract tests."""

CURRENT_SINGLE_YEAR_MACRO_KEYS = {
    "model_version",
    "data_version",
    "budget",
    "detailed_budget",
    "decile",
    "inequality",
    "poverty",
    "poverty_by_gender",
    "poverty_by_race",
    "intra_decile",
    "wealth_decile",
    "intra_wealth_decile",
    "labor_supply_response",
    "constituency_impact",
    "local_authority_impact",
    "congressional_district_impact",
    "cliff_impact",
}

CURRENT_REQUIRED_BUDGET_KEYS = {
    "budgetary_impact",
    "tax_revenue_impact",
    "state_tax_revenue_impact",
    "benefit_spending_impact",
    "households",
    "baseline_net_income",
}

CURRENT_SINGLE_YEAR_MACRO_RESULT = {
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
        "income_tax": {
            "baseline": 100.0,
            "reform": 125.0,
            "difference": 25.0,
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
            "adult": {"baseline": 0.0, "reform": 0.0},
            "all": {"baseline": 0.1, "reform": 0.09},
            "child": {"baseline": 0.12, "reform": 0.11},
            "senior": {"baseline": 0.0, "reform": 0.0},
        },
        "deep_poverty": {
            "adult": {"baseline": 0.0, "reform": 0.0},
            "all": {"baseline": 0.0, "reform": 0.0},
            "child": {"baseline": 0.04, "reform": 0.03},
            "senior": {"baseline": 0.0, "reform": 0.0},
        },
    },
    "poverty_by_gender": {
        "poverty": {
            "male": {"baseline": 0.08, "reform": 0.07},
            "female": {"baseline": 0.0, "reform": 0.0},
        },
        "deep_poverty": {
            "male": {"baseline": 0.0, "reform": 0.0},
            "female": {"baseline": 0.0, "reform": 0.0},
        },
    },
    "poverty_by_race": {
        "poverty": {
            "black": {"baseline": 0.0, "reform": 0.0},
            "hispanic": {"baseline": 0.0, "reform": 0.0},
            "other": {"baseline": 0.0, "reform": 0.0},
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
        "substitution_lsr": 0.0,
        "income_lsr": 0.0,
        "relative_lsr": {"income": 0.0, "substitution": 0.0},
        "total_change": 0.0,
        "revenue_change": 0.0,
        "decile": {
            "average": {"income": {}, "substitution": {}},
            "relative": {"income": {}, "substitution": {}},
        },
        "hours": {
            "baseline": 0.0,
            "reform": 0.0,
            "change": 0.0,
            "income_effect": 0.0,
            "substitution_effect": 0.0,
        },
    },
    "constituency_impact": None,
    "local_authority_impact": None,
    "congressional_district_impact": {
        "districts": [
            {
                "district": "AL-01",
                "average_household_income_change": 10.0,
                "relative_household_income_change": 0.01,
                "winner_percentage": 0.6,
                "loser_percentage": 0.3,
                "no_change_percentage": 0.1,
                "population": 1000.0,
            }
        ]
    },
    "cliff_impact": None,
}
