"""Fixtures for PolicyEngine v4 output adapter tests."""

from __future__ import annotations

from types import SimpleNamespace

import pandas as pd


class FakeCollection:
    def __init__(self, records):
        self.dataframe = pd.DataFrame(records)


class FakeModelOutput:
    def __init__(self, payload):
        self.payload = payload

    def model_dump(self, *, mode):
        assert mode == "json"
        return self.payload


def fake_analysis():
    return SimpleNamespace(
        program_statistics=FakeCollection(
            [
                {
                    "program_name": "income_tax",
                    "baseline_total": 100.0,
                    "reform_total": 125.0,
                    "change": 25.0,
                }
            ]
        ),
        decile_impacts=FakeCollection(
            [
                {
                    "decile": 2,
                    "absolute_change": 20.0,
                    "relative_change": 0.02,
                },
                {
                    "decile": 1,
                    "absolute_change": 10.0,
                    "relative_change": 0.01,
                },
            ]
        ),
        wealth_decile_impacts=FakeCollection(
            [
                {
                    "decile": 1,
                    "absolute_change": 30.0,
                    "relative_change": 0.03,
                }
            ]
        ),
        intra_wealth_decile_impacts=FakeCollection(
            [
                {
                    "decile": 1,
                    "lose_more_than_5pct": 0.1,
                    "lose_less_than_5pct": 0.2,
                    "no_change": 0.3,
                    "gain_less_than_5pct": 0.4,
                    "gain_more_than_5pct": 0.5,
                }
            ]
        ),
        baseline_poverty=FakeCollection(
            [{"poverty_type": "spm", "filter_group": None, "rate": 0.10}]
        ),
        reform_poverty=FakeCollection(
            [{"poverty_type": "spm", "filter_group": None, "rate": 0.09}]
        ),
        baseline_inequality=SimpleNamespace(
            gini=0.40,
            top_10_share=0.30,
            top_1_share=0.10,
        ),
        reform_inequality=SimpleNamespace(
            gini=0.39,
            top_10_share=0.29,
            top_1_share=0.09,
        ),
        labor_supply_response=FakeModelOutput(
            {
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
            }
        ),
    )


INTRA_DECILE_COLLECTION = FakeCollection(
    [
        {
            "decile": 1,
            "lose_more_than_5pct": 0.1,
            "lose_less_than_5pct": 0.2,
            "no_change": 0.3,
            "gain_less_than_5pct": 0.4,
            "gain_more_than_5pct": 0.5,
        },
        {
            "decile": 2,
            "lose_more_than_5pct": 0.0,
            "lose_less_than_5pct": 0.1,
            "no_change": 0.6,
            "gain_less_than_5pct": 0.2,
            "gain_more_than_5pct": 0.1,
        },
    ]
)

BASELINE_POVERTY_BY_AGE = FakeCollection(
    [
        {"poverty_type": "spm", "filter_group": "child", "rate": 0.12},
        {"poverty_type": "spm_deep", "filter_group": "child", "rate": 0.04},
    ]
)
REFORM_POVERTY_BY_AGE = FakeCollection(
    [
        {"poverty_type": "spm", "filter_group": "child", "rate": 0.11},
        {"poverty_type": "spm_deep", "filter_group": "child", "rate": 0.03},
    ]
)
BASELINE_POVERTY_BY_GENDER = FakeCollection(
    [{"poverty_type": "spm", "filter_group": "male", "rate": 0.08}]
)
REFORM_POVERTY_BY_GENDER = FakeCollection(
    [{"poverty_type": "spm", "filter_group": "male", "rate": 0.07}]
)
BASELINE_POVERTY_BY_RACE = FakeCollection(
    [
        {"poverty_type": "spm", "filter_group": "white", "rate": 0.06},
        {"poverty_type": "spm_deep", "filter_group": "white", "rate": 0.02},
    ]
)
REFORM_POVERTY_BY_RACE = FakeCollection(
    [
        {"poverty_type": "spm", "filter_group": "white", "rate": 0.05},
        {"poverty_type": "spm_deep", "filter_group": "white", "rate": 0.01},
    ]
)

BUDGET = {
    "tax_revenue_impact": 100.0,
    "state_tax_revenue_impact": 20.0,
    "benefit_spending_impact": 30.0,
    "budgetary_impact": 70.0,
    "households": 2.0,
    "baseline_net_income": 1000.0,
}
