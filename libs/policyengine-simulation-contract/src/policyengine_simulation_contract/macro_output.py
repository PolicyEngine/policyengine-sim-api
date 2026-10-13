"""Public schemas for completed single-year macro simulation results."""

from __future__ import annotations

from typing import Generic, TypeVar

from pydantic import BaseModel, ConfigDict, RootModel

from policyengine_simulation_contract.spm import SPMComparisonProvenance

T = TypeVar("T")


class MacroOutputModel(BaseModel):
    """Base model for closed macro-output objects."""

    model_config = ConfigDict(extra="forbid")


class MacroRootModel(RootModel[T], Generic[T]):
    """Base model for named map and list output schemas."""


class BudgetaryImpact(MacroOutputModel):  # noqa: D101  # docstring would change the published OpenAPI description
    tax_revenue_impact: float
    state_tax_revenue_impact: float
    benefit_spending_impact: float
    budgetary_impact: float
    households: float
    baseline_net_income: float


BudgetaryOutput = BudgetaryImpact


class DetailedBudgetProgramOutput(MacroOutputModel):  # noqa: D101  # docstring would change the published OpenAPI description
    baseline: float
    reform: float
    difference: float


class DetailedBudgetOutput(MacroRootModel[dict[str, DetailedBudgetProgramOutput]]):
    """Program name mapped to its baseline and reform totals."""


class DecileOutput(MacroOutputModel):  # noqa: D101  # docstring would change the published OpenAPI description
    average: dict[str, float]
    relative: dict[str, float]


class IntraDecileOutput(MacroOutputModel):  # noqa: D101  # docstring would change the published OpenAPI description
    deciles: dict[str, list[float]]
    all: dict[str, float]


class BaselineReformValue(MacroOutputModel):  # noqa: D101  # docstring would change the published OpenAPI description
    baseline: float
    reform: float


class AgePovertyOutput(MacroOutputModel):  # noqa: D101  # docstring would change the published OpenAPI description
    child: BaselineReformValue
    adult: BaselineReformValue
    senior: BaselineReformValue
    all: BaselineReformValue


class GenderPovertyOutput(MacroOutputModel):  # noqa: D101  # docstring would change the published OpenAPI description
    male: BaselineReformValue
    female: BaselineReformValue


class RacePovertyOutput(MacroOutputModel):  # noqa: D101  # docstring would change the published OpenAPI description
    white: BaselineReformValue
    black: BaselineReformValue
    hispanic: BaselineReformValue
    other: BaselineReformValue


class PovertyOutput(MacroOutputModel):  # noqa: D101  # docstring would change the published OpenAPI description
    poverty: AgePovertyOutput
    deep_poverty: AgePovertyOutput


class PovertyByGenderOutput(MacroOutputModel):  # noqa: D101  # docstring would change the published OpenAPI description
    poverty: GenderPovertyOutput
    deep_poverty: GenderPovertyOutput


class PovertyByRaceOutput(MacroOutputModel):  # noqa: D101  # docstring would change the published OpenAPI description
    poverty: RacePovertyOutput


class PovertyModuleOutputs(MacroOutputModel):  # noqa: D101  # docstring would change the published JSON schema description
    poverty: PovertyOutput
    poverty_by_gender: PovertyByGenderOutput
    poverty_by_race: PovertyByRaceOutput | None


class InequalityOutput(MacroOutputModel):  # noqa: D101  # docstring would change the published OpenAPI description
    gini: BaselineReformValue
    top_10_pct_share: BaselineReformValue
    top_1_pct_share: BaselineReformValue


class LaborSupplyRelativeResponse(MacroOutputModel):  # noqa: D101  # docstring would change the published OpenAPI description
    income: float
    substitution: float


class LaborSupplyDecileMetric(MacroOutputModel):  # noqa: D101  # docstring would change the published OpenAPI description
    income: dict[int, float]
    substitution: dict[int, float]


class LaborSupplyDecileOutput(MacroOutputModel):  # noqa: D101  # docstring would change the published OpenAPI description
    average: LaborSupplyDecileMetric
    relative: LaborSupplyDecileMetric


class LaborSupplyHoursOutput(MacroOutputModel):  # noqa: D101  # docstring would change the published OpenAPI description
    baseline: float
    reform: float
    change: float
    income_effect: float
    substitution_effect: float


class LaborSupplyResponseOutput(MacroOutputModel):  # noqa: D101  # docstring would change the published OpenAPI description
    substitution_lsr: float
    income_lsr: float
    relative_lsr: LaborSupplyRelativeResponse
    total_change: float
    revenue_change: float
    decile: LaborSupplyDecileOutput
    hours: LaborSupplyHoursOutput


class CliffImpactInSimulation(MacroOutputModel):  # noqa: D101  # docstring would change the published OpenAPI description
    cliff_gap: float
    cliff_share: float


class CliffImpactOutput(MacroOutputModel):  # noqa: D101  # docstring would change the published OpenAPI description
    baseline: CliffImpactInSimulation
    reform: CliffImpactInSimulation


class ConstituencyImpactRecord(MacroOutputModel):  # noqa: D101  # docstring would change the published OpenAPI description
    constituency_code: str
    constituency_name: str
    x: int | None
    y: int | None
    average_household_income_change: float
    relative_household_income_change: float
    population: float


class LocalAuthorityImpactRecord(MacroOutputModel):  # noqa: D101  # docstring would change the published OpenAPI description
    local_authority_code: str
    local_authority_name: str
    x: int | None
    y: int | None
    average_household_income_change: float
    relative_household_income_change: float
    population: float


class GeographicImpactOutput(
    MacroRootModel[list[ConstituencyImpactRecord | LocalAuthorityImpactRecord]]
):
    """UK constituency or local-authority impact records."""


class CongressionalDistrictImpactRecord(MacroOutputModel):  # noqa: D101  # docstring would change the published OpenAPI description
    district: str
    average_household_income_change: float
    relative_household_income_change: float
    winner_percentage: float
    loser_percentage: float
    no_change_percentage: float
    population: float


class CongressionalDistrictImpactOutput(MacroOutputModel):  # noqa: D101  # docstring would change the published OpenAPI description
    districts: list[CongressionalDistrictImpactRecord]


class SingleYearMacroOutput(MacroOutputModel):
    """Completed response returned by a single-year macro simulation."""

    spm_provenance: SPMComparisonProvenance | None = None

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
    congressional_district_impact: CongressionalDistrictImpactOutput | None
    cliff_impact: CliffImpactOutput | None = None
