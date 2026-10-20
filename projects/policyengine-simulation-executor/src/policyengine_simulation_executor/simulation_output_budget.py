"""Budget output segment builders."""

from __future__ import annotations

import math
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

# Upper bounds on the published household count. Each is well above any real
# run and far below the sum of squared weights that #767 published (about 10^4
# times the count). The US has about 131 million households (US Census Bureau,
# Current Population Survey 2023) and the UK about 28 million (ONS, Families and
# households in the UK 2023).
MAX_PLAUSIBLE_HOUSEHOLDS = {"us": 2e8, "uk": 6e7}


class ImplausibleEntityCountError(ValueError):
    """A published entity count is negative, not finite or above its bound."""


def build_detailed_budget(analysis: object) -> DetailedBudgetOutput:
    """Return per-program baseline, reform and difference totals from the analysis.

    Rows without a program name are skipped, and a missing or non-finite
    ``change`` falls back to reform minus baseline.
    """
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


def _require_plausible_households(country: str, households: float) -> None:
    """Refuse a household count no real run of the country could produce.

    A count is a plain sum of weights, so it is never negative, and one far
    above the country's population means the weights were aggregated as
    values. A country without a bound only needs a finite, non-negative count.
    """
    maximum = MAX_PLAUSIBLE_HOUSEHOLDS.get(country.lower(), math.inf)
    if not (math.isfinite(households) and 0 <= households <= maximum):
        msg = (
            f"{country.upper()} household count {households:,.0f} is outside the "
            f"plausible range 0 to {maximum:,.0f}; check that the household "
            "weights are summed plainly, not aggregated as values"
        )
        raise ImplausibleEntityCountError(msg)


def build_budgetary_impact(
    country: str, baseline: Simulation, reform: Simulation
) -> BudgetaryImpact:
    """Compute headline budget figures from household tax and benefit changes.

    ``budgetary_impact`` is the tax revenue change minus the benefit spending
    change. State income tax is only modelled for the US; elsewhere its impact
    is 0.0. A household count outside the country's plausible range raises
    ``ImplausibleEntityCountError``.
    """
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

    households = _count_output_entities(
        baseline, entity="household", weight="household_weight"
    )
    _require_plausible_households(country, households)

    return BudgetaryImpact(
        tax_revenue_impact=tax_revenue_impact,
        state_tax_revenue_impact=state_tax_revenue_impact,
        benefit_spending_impact=benefit_spending_impact,
        budgetary_impact=tax_revenue_impact - benefit_spending_impact,
        households=households,
        baseline_net_income=_sum_output_variable(
            baseline, "household_net_income", entity="household"
        ),
    )
