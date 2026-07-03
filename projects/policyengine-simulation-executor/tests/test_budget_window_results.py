"""Tests for budget-window batch result helpers."""

import pytest

from src.modal.budget_window_results import (
    build_budget_window_result,
    extract_annual_impact,
    sum_annual_impacts,
)
from src.modal.gateway.models import BudgetWindowAnnualImpact


def test_extract_annual_impact_matches_v1_shape():
    annual = extract_annual_impact(
        simulation_year="2026",
        child_result={
            "budget": {
                "tax_revenue_impact": 100,
                "state_tax_revenue_impact": 40,
                "benefit_spending_impact": 20,
                "budgetary_impact": 80,
            }
        },
    )

    assert annual == BudgetWindowAnnualImpact(
        year="2026",
        taxRevenueImpact=100,
        federalTaxRevenueImpact=60,
        stateTaxRevenueImpact=40,
        benefitSpendingImpact=20,
        budgetaryImpact=80,
    )


def test_extract_annual_impact_defaults_state_tax_for_uk_child_result():
    """UK worker results omit ``state_tax_revenue_impact`` because the UK
    microsimulation has no devolved fiscal layer. The aggregator should
    accept these results and treat the state component as zero instead of
    failing with "missing numeric budget.state_tax_revenue_impact"."""

    annual = extract_annual_impact(
        simulation_year="2026",
        child_result={
            "budget": {
                # UK payload shape: tax_revenue_impact is the full HMRC total,
                # state_tax_revenue_impact is absent.
                "tax_revenue_impact": 250,
                "benefit_spending_impact": 40,
                "budgetary_impact": 210,
            }
        },
    )

    assert annual == BudgetWindowAnnualImpact(
        year="2026",
        taxRevenueImpact=250,
        federalTaxRevenueImpact=250,
        stateTaxRevenueImpact=0,
        benefitSpendingImpact=40,
        budgetaryImpact=210,
    )


def test_extract_annual_impact_rejects_malformed_child_result():
    with pytest.raises(
        ValueError,
        match="Malformed budget-window child result: missing numeric budget.tax_revenue_impact",
    ):
        extract_annual_impact(
            simulation_year="2026",
            child_result={
                "budget": {
                    "state_tax_revenue_impact": 40,
                    "benefit_spending_impact": 20,
                    "budgetary_impact": 80,
                }
            },
        )


def test_sum_annual_impacts_avoids_binary_float_drift():
    """0.1 + 0.2 + 0.3 = 0.6 exactly when accumulated in Decimal.

    With float addition this sequence drifts by ~5e-17 which is enough to
    break bit-exact deduplication on the client side. Accumulating in
    :class:`decimal.Decimal` collapses the drift before the float downcast.
    """
    from src.modal.budget_window_results import sum_annual_impacts

    totals = sum_annual_impacts(
        [
            BudgetWindowAnnualImpact(
                year="2026",
                taxRevenueImpact=0.1,
                federalTaxRevenueImpact=0,
                stateTaxRevenueImpact=0,
                benefitSpendingImpact=0,
                budgetaryImpact=0,
            ),
            BudgetWindowAnnualImpact(
                year="2027",
                taxRevenueImpact=0.2,
                federalTaxRevenueImpact=0,
                stateTaxRevenueImpact=0,
                benefitSpendingImpact=0,
                budgetaryImpact=0,
            ),
            BudgetWindowAnnualImpact(
                year="2028",
                taxRevenueImpact=0.3,
                federalTaxRevenueImpact=0,
                stateTaxRevenueImpact=0,
                benefitSpendingImpact=0,
                budgetaryImpact=0,
            ),
        ]
    )
    assert totals.taxRevenueImpact == 0.6


def test_build_budget_window_result_sums_totals():
    annual_impacts = [
        BudgetWindowAnnualImpact(
            year="2026",
            taxRevenueImpact=10,
            federalTaxRevenueImpact=7,
            stateTaxRevenueImpact=3,
            benefitSpendingImpact=5,
            budgetaryImpact=15,
        ),
        BudgetWindowAnnualImpact(
            year="2027",
            taxRevenueImpact=11,
            federalTaxRevenueImpact=8,
            stateTaxRevenueImpact=3,
            benefitSpendingImpact=6,
            budgetaryImpact=17,
        ),
    ]

    totals = sum_annual_impacts(annual_impacts)
    result = build_budget_window_result(
        start_year="2026",
        window_size=2,
        annual_impacts=annual_impacts,
    )

    assert totals.budgetaryImpact == 32
    assert result.endYear == "2027"
    assert result.totals.taxRevenueImpact == 21
    assert result.totals.budgetaryImpact == 32
