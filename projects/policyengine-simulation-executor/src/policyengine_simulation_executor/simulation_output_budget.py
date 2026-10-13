"""Budget output segment builders."""

from __future__ import annotations

from typing import TYPE_CHECKING

from policyengine_simulation_executor.simulation_macro_output import (
    BudgetaryImpact,
    DetailedBudgetOutput,
    DetailedBudgetProgramOutput,
)
from policyengine_simulation_executor.simulation_output_common import (
    _change_output_variable,
    _collection_records,
    _count_output_entities,
    _number,
    _sum_output_variable,
)

if TYPE_CHECKING:
    from policyengine.core import Simulation


def build_detailed_budget(analysis: object) -> DetailedBudgetOutput:
    collection = getattr(analysis, "program_statistics", None)
    if isinstance(collection, DetailedBudgetOutput):
        return collection
    detailed_budget: dict[str, DetailedBudgetProgramOutput] = {}
    for row in _collection_records(collection):
        program_name = row.get("program_name")
        if not program_name:
            continue
        baseline = _number(row.get("baseline_total"))
        reform = _number(row.get("reform_total"))
        detailed_budget[str(program_name)] = DetailedBudgetProgramOutput(
            baseline=baseline,
            reform=reform,
            difference=_number(row.get("change"), reform - baseline),
        )
    return DetailedBudgetOutput(detailed_budget)


def build_budgetary_impact(
    country: str, baseline: Simulation, reform: Simulation
) -> BudgetaryImpact:
    tax_revenue_impact = _change_output_variable(
        baseline, reform, "household_tax", entity="household"
    )
    benefit_spending_impact = _change_output_variable(
        baseline, reform, "household_benefits", entity="household"
    )
    state_tax_revenue_impact = (
        _change_output_variable(
            baseline,
            reform,
            "household_state_income_tax",
            entity="household",
        )
        if country == "us"
        else 0.0
    )

    return BudgetaryImpact(
        tax_revenue_impact=tax_revenue_impact,
        state_tax_revenue_impact=state_tax_revenue_impact,
        benefit_spending_impact=benefit_spending_impact,
        budgetary_impact=tax_revenue_impact - benefit_spending_impact,
        households=_count_output_entities(
            baseline, entity="household", weight="household_weight"
        ),
        baseline_net_income=_sum_output_variable(
            baseline, "household_net_income", entity="household"
        ),
    )
