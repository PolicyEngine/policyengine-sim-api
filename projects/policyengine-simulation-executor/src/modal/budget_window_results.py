"""Budget-window annual result extraction and aggregation helpers."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from src.modal.gateway.models import (
    BudgetWindowAnnualImpact,
    BudgetWindowResult,
    BudgetWindowTotals,
)

# The UK microsimulation has no state/province fiscal layer, so worker child
# results for ``country="uk"`` never emit ``state_tax_revenue_impact``. The
# parent aggregator treats it as optional with a zero default; US results are
# expected to supply it as a real number. All other keys remain mandatory.
REQUIRED_BUDGET_KEYS = (
    "tax_revenue_impact",
    "benefit_spending_impact",
    "budgetary_impact",
)
OPTIONAL_BUDGET_KEYS = ("state_tax_revenue_impact",)


def _as_decimal(value: float | int) -> Decimal:
    """Convert an annual impact float to Decimal without reintroducing
    binary-float quantisation noise. ``Decimal(str(...))`` is the canonical
    idiom because it serialises the float to its shortest round-trippable
    decimal form before parsing."""

    return Decimal(str(value))


def extract_annual_impact(
    *,
    simulation_year: str,
    child_result: dict[str, Any],
) -> BudgetWindowAnnualImpact:
    budget = child_result.get("budget", {})
    if not isinstance(budget, dict):
        raise ValueError("Malformed budget-window child result: missing budget object")

    missing_keys = [
        key
        for key in REQUIRED_BUDGET_KEYS
        if not isinstance(budget.get(key), int | float)
    ]
    if missing_keys:
        missing = ", ".join(f"budget.{key}" for key in missing_keys)
        raise ValueError(
            f"Malformed budget-window child result: missing numeric {missing}"
        )

    tax_revenue_impact = budget["tax_revenue_impact"]
    # UK worker results omit the state fiscal layer entirely; coerce to 0.0
    # so the parent aggregator can still report federal/state splits with a
    # uniform shape across countries.
    state_tax_revenue_impact = budget.get("state_tax_revenue_impact")
    if not isinstance(state_tax_revenue_impact, int | float):
        state_tax_revenue_impact = 0.0

    return BudgetWindowAnnualImpact(
        year=simulation_year,
        taxRevenueImpact=tax_revenue_impact,
        federalTaxRevenueImpact=tax_revenue_impact - state_tax_revenue_impact,
        stateTaxRevenueImpact=state_tax_revenue_impact,
        benefitSpendingImpact=budget["benefit_spending_impact"],
        budgetaryImpact=budget["budgetary_impact"],
    )


def sum_annual_impacts(
    annual_impacts: list[BudgetWindowAnnualImpact],
) -> BudgetWindowTotals:
    """Sum per-year impacts using Decimal accumulators.

    Binary-float addition accumulates rounding error for long budget windows
    (10-year sums over billion-dollar baselines quickly drift by ``1e-6`` or
    more). Accumulating in :class:`decimal.Decimal` keeps the answer exact
    to the input precision; we cast back to ``float`` at the serialisation
    boundary so the JSON schema stays numeric and clients that parse the
    response as ``number`` continue to work unchanged. Clients that need
    bit-exact accounting should request the individual per-year impacts and
    sum them in their preferred numeric type.
    """

    totals: dict[str, Decimal] = {
        "taxRevenueImpact": Decimal(0),
        "federalTaxRevenueImpact": Decimal(0),
        "stateTaxRevenueImpact": Decimal(0),
        "benefitSpendingImpact": Decimal(0),
        "budgetaryImpact": Decimal(0),
    }

    for annual_impact in annual_impacts:
        totals["taxRevenueImpact"] += _as_decimal(annual_impact.taxRevenueImpact)
        totals["federalTaxRevenueImpact"] += _as_decimal(
            annual_impact.federalTaxRevenueImpact
        )
        totals["stateTaxRevenueImpact"] += _as_decimal(
            annual_impact.stateTaxRevenueImpact
        )
        totals["benefitSpendingImpact"] += _as_decimal(
            annual_impact.benefitSpendingImpact
        )
        totals["budgetaryImpact"] += _as_decimal(annual_impact.budgetaryImpact)

    return BudgetWindowTotals(**{key: float(value) for key, value in totals.items()})


def build_budget_window_result(
    *,
    start_year: str,
    window_size: int,
    annual_impacts: list[BudgetWindowAnnualImpact],
) -> BudgetWindowResult:
    return BudgetWindowResult(
        startYear=start_year,
        endYear=str(int(start_year) + window_size - 1),
        windowSize=window_size,
        annualImpacts=annual_impacts,
        totals=sum_annual_impacts(annual_impacts),
    )
