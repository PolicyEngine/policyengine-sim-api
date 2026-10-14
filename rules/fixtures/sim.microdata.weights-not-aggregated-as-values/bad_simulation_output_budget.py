"""Proof fixture: a weight aggregated as a value. The rule must reject it.

This is the budget builder before #767. The household frame is a MicroDataFrame
weighted by ``household_weight``, so summing ``household_weight`` through it
returned the sum of squared weights, about 10^4 times the household count.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from policyengine_simulation_executor.simulation_macro_output import BudgetaryImpact
from policyengine_simulation_executor.simulation_output_common import (
    _change_output_variable,
    _sum_output_variable,
)

if TYPE_CHECKING:
    from policyengine.core import Simulation


def build_budgetary_impact(baseline: Simulation, reform: Simulation) -> BudgetaryImpact:
    """Compute headline budget figures as the builder did before #767."""
    tax_revenue_impact = _change_output_variable(
        baseline, reform, "household_tax", entity="household"
    )
    benefit_spending_impact = _change_output_variable(
        baseline, reform, "household_benefits", entity="household"
    )
    return BudgetaryImpact(
        tax_revenue_impact=tax_revenue_impact,
        state_tax_revenue_impact=0.0,
        benefit_spending_impact=benefit_spending_impact,
        budgetary_impact=tax_revenue_impact - benefit_spending_impact,
        households=_sum_output_variable(
            baseline, "household_weight", entity="household"
        ),
        baseline_net_income=_sum_output_variable(
            baseline, "household_net_income", entity="household"
        ),
    )
