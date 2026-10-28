"""Public schemas for completed single-year macro simulation results."""

from __future__ import annotations

from typing import Generic, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, RootModel

from policyengine_simulation_contract.spm import SPMComparisonProvenance

T = TypeVar("T")


class MacroOutputModel(BaseModel):
    """Base model for closed macro-output objects."""

    model_config = ConfigDict(extra="forbid")


class MacroRootModel(RootModel[T], Generic[T]):
    """Base model for named map and list output schemas."""


class BudgetaryImpact(MacroOutputModel):
    """Headline budget effect of the reform for the simulated year.

    Impacts are reform minus baseline totals in the country's currency.
    ``budgetary_impact`` is ``tax_revenue_impact`` minus
    ``benefit_spending_impact``; ``state_tax_revenue_impact`` covers US state
    income tax and is 0 for the UK. ``households`` is the weighted number of
    households and ``baseline_net_income`` their total baseline net income.
    """

    tax_revenue_impact: float
    state_tax_revenue_impact: float
    benefit_spending_impact: float
    budgetary_impact: float
    households: float
    baseline_net_income: float


BudgetaryOutput = BudgetaryImpact


class DetailedBudgetProgramOutput(MacroOutputModel):
    """Baseline and reform totals for one program.

    ``difference`` is reform minus baseline.
    """

    baseline: float
    reform: float
    difference: float


class DetailedBudgetOutput(MacroRootModel[dict[str, DetailedBudgetProgramOutput]]):
    """Program name mapped to its baseline and reform totals."""


class DecileOutput(MacroOutputModel):
    """Change in average household net income in each decile.

    Keys are decile numbers from ``"1"`` (lowest) to ``"10"`` (highest); the same
    shape is used for income deciles and UK wealth deciles. ``average`` is the
    absolute change and ``relative`` the change relative to the decile's
    baseline average.
    """

    average: dict[str, float]
    relative: dict[str, float]


class IntraDecileOutput(MacroOutputModel):
    """Share of people in each decile who gain or lose, grouped by size of change.

    The categories are gaining or losing more or less than 5% of household net
    income, or no change. ``deciles`` lists each category's share for deciles 1
    to 10 in order, and ``all`` gives each category's average across the deciles.
    """

    deciles: dict[str, list[float]]
    all: dict[str, float]


class BaselineReformValue(MacroOutputModel):
    """A metric's value under the baseline and under the reform."""

    baseline: float
    reform: float


class AgePovertyOutput(MacroOutputModel):
    """Baseline and reform poverty rates for children, adults, seniors and everyone."""

    child: BaselineReformValue
    adult: BaselineReformValue
    senior: BaselineReformValue
    all: BaselineReformValue


class GenderPovertyOutput(MacroOutputModel):
    """Baseline and reform poverty rates for males and females."""

    male: BaselineReformValue
    female: BaselineReformValue


class RacePovertyOutput(MacroOutputModel):
    """Baseline and reform poverty rates by race and ethnicity (US only)."""

    white: BaselineReformValue
    black: BaselineReformValue
    hispanic: BaselineReformValue
    other: BaselineReformValue


class PovertyOutput(MacroOutputModel):
    """Poverty and deep poverty rates by age group.

    In the US these are the SPM poverty and deep SPM poverty rates. In the UK,
    ``poverty`` is relative and ``deep_poverty`` absolute poverty, both measured
    before housing costs.
    """

    poverty: AgePovertyOutput
    deep_poverty: AgePovertyOutput


class PovertyByGenderOutput(MacroOutputModel):
    """Poverty and deep poverty rates by gender, as defined for ``PovertyOutput``."""

    poverty: GenderPovertyOutput
    deep_poverty: GenderPovertyOutput


class PovertyByRaceOutput(MacroOutputModel):
    """US poverty rates by race and ethnicity; there is no deep poverty breakdown."""

    poverty: RacePovertyOutput


class PovertyModuleOutputs(MacroOutputModel):
    """Overall, by-gender and by-race poverty results for one comparison.

    ``poverty_by_race`` is null outside the US.
    """

    poverty: PovertyOutput
    poverty_by_gender: PovertyByGenderOutput
    poverty_by_race: PovertyByRaceOutput | None


class InequalityOutput(MacroOutputModel):
    """Baseline and reform income inequality.

    Covers the Gini coefficient and the income shares of the top 10% and top 1%.
    """

    gini: BaselineReformValue
    top_10_pct_share: BaselineReformValue
    top_1_pct_share: BaselineReformValue


class LaborSupplyRelativeResponse(MacroOutputModel):
    """Income- and substitution-effect responses relative to baseline earnings."""

    income: float
    substitution: float


class LaborSupplyDecileMetric(MacroOutputModel):
    """Income- and substitution-effect responses keyed by income decile number."""

    income: dict[int, float]
    substitution: dict[int, float]


class LaborSupplyDecileOutput(MacroOutputModel):
    """Labor supply responses by income decile.

    ``average`` is the response per household and ``relative`` the response
    relative to the decile's baseline earnings.
    """

    average: LaborSupplyDecileMetric
    relative: LaborSupplyDecileMetric


class LaborSupplyHoursOutput(MacroOutputModel):
    """Total weekly hours worked under the baseline and reform, and how they change.

    ``change`` is reform minus baseline, split into its income and substitution
    effects.
    """

    baseline: float
    reform: float
    change: float
    income_effect: float
    substitution_effect: float


class LaborSupplyResponseOutput(MacroOutputModel):
    """Behavioral labor supply response to the reform.

    ``income_lsr`` and ``substitution_lsr`` are the total earnings changes from
    the income and substitution effects, and ``total_change`` is their sum.
    """

    substitution_lsr: float
    income_lsr: float
    relative_lsr: LaborSupplyRelativeResponse
    total_change: float
    revenue_change: float
    decile: LaborSupplyDecileOutput
    hours: LaborSupplyHoursOutput


class CliffImpactInSimulation(MacroOutputModel):
    """Exposure to tax-benefit cliffs in one simulation.

    ``cliff_gap`` is the total net income people would lose from a small rise in
    their earnings, and ``cliff_share`` the share of adults on a cliff.
    """

    cliff_gap: float
    cliff_share: float


class CliffImpactOutput(MacroOutputModel):
    """Exposure to tax-benefit cliffs under the baseline and under the reform."""

    baseline: CliffImpactInSimulation
    reform: CliffImpactInSimulation


UKGeographyDisclosureTier = Literal["reported", "approximate"]


class UKAreaDisclosureFields(MacroOutputModel):
    """Disclosure tier and 90% intervals of a published UK area's income changes.

    UK area results come from licensed survey microdata and pass policyengine's
    disclosure control. An area whose income change is withheld is left out of
    the list, so every record here has values. ``disclosure_tier`` is
    ``reported``, or ``approximate`` when the area's effective sample is thin
    and the interval understates the uncertainty. The ``_lower`` and ``_upper``
    fields bound each change's 90% bootstrap interval, rounded outward.

    All of these fields are optional, so results stored before disclosure
    control still validate; there they are null.
    """

    disclosure_tier: UKGeographyDisclosureTier | None = None
    average_household_income_change_lower: float | None = None
    average_household_income_change_upper: float | None = None
    relative_household_income_change_lower: float | None = None
    relative_household_income_change_upper: float | None = None


class ConstituencyImpactRecord(UKAreaDisclosureFields):
    """Reform impact on household net income in one UK parliamentary constituency.

    ``average_household_income_change`` is the average change per household and
    ``relative_household_income_change`` the relative change in the area's total
    household net income, both rounded to the order of their standard error.
    ``population`` is the weighted number of households. ``x`` and ``y`` are
    integer map-rendering coordinates.
    """

    constituency_code: str
    constituency_name: str
    x: int | None
    y: int | None
    average_household_income_change: float
    relative_household_income_change: float
    population: float


class LocalAuthorityImpactRecord(UKAreaDisclosureFields):
    """Reform impact on household net income in one UK local authority.

    Fields mean the same as in ``ConstituencyImpactRecord``.
    """

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
    """UK constituency or local-authority impact records.

    Areas whose income change disclosure control withholds are left out.
    """


class CongressionalDistrictImpactRecord(MacroOutputModel):
    """Reform impact on household net income in one US congressional district.

    ``district`` is the state abbreviation and two-digit district number, such
    as ``"CA-12"``; at-large districts are numbered ``01``. The income changes
    are the average change per household and the relative change in the
    district's total household net income. The three percentage fields are the
    shares of people whose household net income rises, falls, or stays
    essentially unchanged.
    """

    district: str
    average_household_income_change: float
    relative_household_income_change: float
    winner_percentage: float
    loser_percentage: float
    no_change_percentage: float
    population: float


class CongressionalDistrictImpactOutput(MacroOutputModel):
    """Reform impact for each US congressional district in the simulated region."""

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


class LocalAreaBreakdownOutput(MacroOutputModel):
    """Completed result of a UK local-area breakdown run.

    A request with ``breakdown: "local_areas"`` returns this instead of a
    ``SingleYearMacroOutput``. The run covers the requested UK-wide or nation
    region on the local-area dataset alone, and computes only the
    constituency and local-authority impacts; every other figure comes from
    the national run. ``kind`` is always ``local_areas``. ``dataset`` names the
    local-area dataset, ``data_version`` its release revision, and
    ``model_version`` and ``policyengine_version`` the country model and the
    policyengine.py release that computed it. Each list holds only its own
    level's records. Areas whose income change disclosure control withholds
    are left out of each list, so a list may be empty.

    ``constituency_withheld_codes`` and ``local_authority_withheld_codes``
    hold, sorted, the ONS codes of that level's areas that the run estimated
    but disclosure control did not publish, so a client can tell a withheld
    area from an absent one. An area the run never estimated, such as one with
    no households in the dataset, is in neither list. An empty list means
    that no area of the level was withheld. Null means that the result does
    not report them, as results from older executors do not.
    """

    kind: Literal["local_areas"]
    model_version: str
    policyengine_version: str
    dataset: str
    data_version: str
    constituency_impact: list[ConstituencyImpactRecord]
    local_authority_impact: list[LocalAuthorityImpactRecord]
    constituency_withheld_codes: list[str] | None = None
    local_authority_withheld_codes: list[str] | None = None
