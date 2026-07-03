"""Internal schemas for the simulation API single-year macro output.

These models define the legacy dictionary contract the simulation API returns
without exposing that schema through the gateway OpenAPI surface. The gateway
still treats job results as unstructured dictionaries for older callers.
"""

from __future__ import annotations

from typing import Any, Generic, TypeVar

from pydantic import BaseModel, ConfigDict, RootModel

T = TypeVar("T")


class MacroOutputModel(BaseModel):
    """Base model for internal macro output schemas."""

    model_config = ConfigDict(extra="forbid")


class MacroRootModel(RootModel[T], Generic[T]):
    """Base model for internal root schemas that dump to dict/list values."""


class BudgetaryImpact(MacroOutputModel):
    tax_revenue_impact: float
    state_tax_revenue_impact: float
    benefit_spending_impact: float
    budgetary_impact: float
    households: float
    baseline_net_income: float


BudgetaryOutput = BudgetaryImpact


class DetailedBudgetProgramOutput(MacroOutputModel):
    baseline: float
    reform: float
    difference: float


class DetailedBudgetOutput(MacroRootModel[dict[str, DetailedBudgetProgramOutput]]):
    pass


class DecileOutput(MacroOutputModel):
    average: dict[str, float]
    relative: dict[str, float]


class IntraDecileOutput(MacroOutputModel):
    deciles: dict[str, list[float]]
    all: dict[str, float]


class BaselineReformValue(MacroOutputModel):
    baseline: float
    reform: float


class AgePovertyOutput(MacroOutputModel):
    child: BaselineReformValue
    adult: BaselineReformValue
    senior: BaselineReformValue
    all: BaselineReformValue


class GenderPovertyOutput(MacroOutputModel):
    male: BaselineReformValue
    female: BaselineReformValue


class RacePovertyOutput(MacroOutputModel):
    white: BaselineReformValue
    black: BaselineReformValue
    hispanic: BaselineReformValue
    other: BaselineReformValue


class PovertyOutput(MacroOutputModel):
    poverty: AgePovertyOutput
    deep_poverty: AgePovertyOutput


class PovertyByGenderOutput(MacroOutputModel):
    poverty: GenderPovertyOutput
    deep_poverty: GenderPovertyOutput


class PovertyByRaceOutput(MacroOutputModel):
    poverty: RacePovertyOutput


class PovertyModuleOutputs(MacroOutputModel):
    poverty: PovertyOutput
    poverty_by_gender: PovertyByGenderOutput
    poverty_by_race: PovertyByRaceOutput | None


class InequalityOutput(MacroOutputModel):
    gini: BaselineReformValue
    top_10_pct_share: BaselineReformValue
    top_1_pct_share: BaselineReformValue


class LaborSupplyResponseOutput(MacroRootModel[dict[str, Any]]):
    pass


class CliffImpactInSimulation(MacroOutputModel):
    cliff_gap: float
    cliff_share: float


class CliffImpactOutput(MacroOutputModel):
    baseline: CliffImpactInSimulation
    reform: CliffImpactInSimulation


class GeographicImpactOutput(MacroRootModel[list[dict[str, Any]]]):
    pass


class SingleYearMacroOutput(MacroOutputModel):
    model_version: str
    data_version: str
    budget: BudgetaryImpact
    detailed_budget: DetailedBudgetOutput
    decile: DecileOutput
    inequality: InequalityOutput
    poverty: PovertyOutput
    poverty_by_gender: PovertyByGenderOutput
    poverty_by_race: PovertyByRaceOutput | None
    intra_decile: IntraDecileOutput
    wealth_decile: DecileOutput | None
    intra_wealth_decile: IntraDecileOutput | None
    labor_supply_response: LaborSupplyResponseOutput | None
    constituency_impact: GeographicImpactOutput | None
    local_authority_impact: GeographicImpactOutput | None
    congressional_district_impact: GeographicImpactOutput | None
    cliff_impact: CliffImpactOutput | None = None
