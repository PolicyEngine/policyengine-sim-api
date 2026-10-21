"""Build and serialize the runtime simulation macro output."""

from __future__ import annotations

from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from policyengine_simulation_contract.job_progress import JobStage
from policyengine_simulation_observability.stages import (
    ANNUAL_IMPACT_STAGES,
    Stage,
)

from policyengine_simulation_executor import (
    simulation_output_budget,
    simulation_output_cliff,
    simulation_output_distribution,
    simulation_output_geographic,
    simulation_output_inequality,
    simulation_output_labor,
    simulation_output_poverty,
)
from policyengine_simulation_executor.job_progress import (
    NULL_JOB_PROGRESS,
    JobProgressReporter,
)
from policyengine_simulation_executor.release_bundle import get_country_release_bundle
from policyengine_simulation_executor.simulation_macro_output import (
    BudgetaryImpact,
    CliffImpactOutput,
    CongressionalDistrictImpactOutput,
    DecileOutput,
    DetailedBudgetOutput,
    GeographicImpactOutput,
    InequalityOutput,
    IntraDecileOutput,
    LaborSupplyResponseOutput,
    PovertyByGenderOutput,
    PovertyByRaceOutput,
    PovertyModuleOutputs,
    PovertyOutput,
    SingleYearMacroOutput,
)

if TYPE_CHECKING:
    from policyengine_observability import ObservabilityRuntime
    from policyengine_simulation_contract.uk_geography import UKLocalAuthorityMetadata


@dataclass(frozen=True)
class UKAreaSimulations:
    """A baseline and reform on the dataset the bundle routes UK area regions to.

    A UK national or nation-level run on a dataset without area codes builds
    its constituency and local-authority breakdowns from this pair, which has
    the same policies and nation filter, and from the authority boundaries
    detected on that dataset.
    """

    baseline: Any
    reform: Any
    local_authority_metadata: UKLocalAuthorityMetadata


@dataclass
class SimulationOutputBuilder:
    """Assemble one year's macro output from a computed baseline and reform.

    Each output segment is built inside its own observability span when a
    ``runtime`` is given. The UK constituency and local-authority breakdowns
    come from ``uk_area_simulations`` when it is given, and otherwise from
    ``baseline`` and ``reform``. ``omit_uk_area_breakdowns`` leaves them out:
    only Stage 12 sets it, for a routed national report whose stored frames
    carry no area codes.
    """

    country: str
    simulation_params: dict[str, Any]
    country_module: Any
    dataset: Any
    baseline: Any
    reform: Any
    resolved_data_version: str | None = None
    resolved_region_code: str | None = None
    runtime: ObservabilityRuntime | None = None
    uk_local_authority_metadata: UKLocalAuthorityMetadata | None = None
    uk_area_simulations: UKAreaSimulations | None = None
    omit_uk_area_breakdowns: bool = False
    progress: JobProgressReporter = NULL_JOB_PROGRESS
    _analysis: Any = field(default=None, init=False)
    _uk_geography_disclosure: Any = field(default=None, init=False)

    def __post_init__(self) -> None:
        """Normalize ``country`` to lower case."""
        self.country = self.country.lower()

    def _span(self, name: str) -> AbstractContextManager[object]:
        return self.runtime.span(name) if self.runtime is not None else nullcontext()

    @property
    def analysis(self) -> object:
        """Country economic impact analysis, computed on first access and reused.

        Cliff impacts are included only when the request sets
        ``include_cliffs`` to True.
        """
        if self._analysis is None:
            with self._span(ANNUAL_IMPACT_STAGES.name(Stage.ECONOMIC_IMPACT_ANALYSIS)):
                self._analysis = self.country_module.economic_impact_analysis(
                    self.baseline,
                    self.reform,
                    include_cliff_impacts=self._include_cliff_impacts(),
                )
            # Both simulations have run; the rest is output aggregation.
            self.progress.enter(JobStage.OUTPUTS)
        return self._analysis

    def _include_cliff_impacts(self) -> bool:
        return self.simulation_params.get("include_cliffs") is True

    def build(self) -> SingleYearMacroOutput:
        """Build every output segment into the typed single-year macro output."""
        with self._span(ANNUAL_IMPACT_STAGES.name(Stage.SIMULATION_OUTPUT_BUILD)):
            poverty_outputs = self._build_poverty_outputs()
            wealth_decile = getattr(self.analysis, "wealth_decile_impacts", None)
            intra_wealth_decile = getattr(
                self.analysis, "intra_wealth_decile_impacts", None
            )

            return SingleYearMacroOutput(
                model_version=self._model_version(),
                data_version=self._data_version(),
                budget=self._build_budgetary_impact(),
                detailed_budget=self._build_detailed_budget(),
                decile=self._build_decile(),
                inequality=self._build_inequality(),
                poverty=poverty_outputs.poverty,
                poverty_by_gender=poverty_outputs.poverty_by_gender,
                poverty_by_race=poverty_outputs.poverty_by_race,
                intra_decile=self._build_intra_decile_output(),
                wealth_decile=self._build_wealth_decile(wealth_decile),
                intra_wealth_decile=self._build_intra_wealth_decile(
                    intra_wealth_decile
                ),
                labor_supply_response=self._build_labor_supply_response(),
                congressional_district_impact=(
                    self._build_congressional_district_impact()
                ),
                constituency_impact=self._build_uk_constituency_impact(),
                local_authority_impact=self._build_uk_local_authority_impact(),
                cliff_impact=self._build_cliff_impact(),
            )

    def serialize(self) -> dict[str, Any]:
        """Build the output and dump it to JSON-compatible data."""
        with self._span(ANNUAL_IMPACT_STAGES.name(Stage.CALCULATION)):
            output = self.build()
        with (
            self._span(ANNUAL_IMPACT_STAGES.name(Stage.RESPONSE_SERIALIZATION)),
            self._span(ANNUAL_IMPACT_STAGES.name(Stage.SIMULATION_OUTPUT_MODEL_DUMP)),
        ):
            return output.model_dump(mode="json")

    def _build_detailed_budget(self) -> DetailedBudgetOutput:
        with self._span(ANNUAL_IMPACT_STAGES.name(Stage.OUTPUT_DETAILED_BUDGET)):
            return simulation_output_budget.build_detailed_budget(self.analysis)

    def _build_decile(self) -> DecileOutput:
        with self._span(ANNUAL_IMPACT_STAGES.name(Stage.OUTPUT_DECILE)):
            return simulation_output_distribution.build_decile(self.analysis)

    def _build_inequality(self) -> InequalityOutput:
        with self._span(ANNUAL_IMPACT_STAGES.name(Stage.OUTPUT_INEQUALITY)):
            return simulation_output_inequality.build_inequality(self.analysis)

    def _build_budgetary_impact(self) -> BudgetaryImpact:
        with self._span(ANNUAL_IMPACT_STAGES.name(Stage.OUTPUT_BUDGETARY_IMPACT)):
            return simulation_output_budget.build_budgetary_impact(
                self.country, self.baseline, self.reform
            )

    def _build_poverty_outputs(self) -> PovertyModuleOutputs:
        with self._span(ANNUAL_IMPACT_STAGES.name(Stage.OUTPUT_POVERTY)):
            return simulation_output_poverty.build_poverty_outputs(
                self.country, self.baseline, self.reform, self.analysis
            )

    def _build_intra_decile_output(self) -> IntraDecileOutput:
        with self._span(ANNUAL_IMPACT_STAGES.name(Stage.OUTPUT_INTRA_DECILE)):
            return simulation_output_distribution.build_intra_decile_output(
                self.baseline, self.reform
            )

    def _build_wealth_decile(self, wealth_decile: object) -> DecileOutput | None:
        with self._span(ANNUAL_IMPACT_STAGES.name(Stage.OUTPUT_WEALTH_DECILE)):
            return simulation_output_distribution.build_wealth_decile(
                self.country, wealth_decile
            )

    def _build_intra_wealth_decile(
        self, intra_wealth_decile: object
    ) -> IntraDecileOutput | None:
        with self._span(ANNUAL_IMPACT_STAGES.name(Stage.OUTPUT_INTRA_WEALTH_DECILE)):
            return simulation_output_distribution.build_intra_wealth_decile(
                self.country, intra_wealth_decile
            )

    def _build_labor_supply_response(self) -> LaborSupplyResponseOutput | None:
        with self._span(ANNUAL_IMPACT_STAGES.name(Stage.OUTPUT_LABOR_SUPPLY)):
            return simulation_output_labor.build_labor_supply_response(self.analysis)

    def _build_cliff_impact(self) -> CliffImpactOutput | None:
        with self._span(ANNUAL_IMPACT_STAGES.name(Stage.OUTPUT_CLIFF)):
            return simulation_output_cliff.build_cliff_impact(self.analysis)

    def _build_geographic_impact_output(
        self, value: object
    ) -> GeographicImpactOutput | None:
        return simulation_output_geographic.build_geographic_impact_output(value)

    def _build_decile_output(self, collection: object) -> DecileOutput:
        return simulation_output_distribution.build_decile_output(collection)

    def _build_intra_decile_output_from_collection(
        self, collection: object
    ) -> IntraDecileOutput:
        return simulation_output_distribution.build_intra_decile_output_from_collection(
            collection
        )

    def _build_poverty_output(
        self,
        *,
        baseline: object,
        reform: object,
        baseline_by_age: object,
        reform_by_age: object,
    ) -> PovertyOutput:
        return simulation_output_poverty.build_poverty_output(
            country=self.country,
            baseline=baseline,
            reform=reform,
            baseline_by_age=baseline_by_age,
            reform_by_age=reform_by_age,
        )

    def _build_poverty_by_gender_output(
        self,
        *,
        baseline_by_gender: object,
        reform_by_gender: object,
    ) -> PovertyByGenderOutput:
        return simulation_output_poverty.build_poverty_by_gender_output(
            country=self.country,
            baseline_by_gender=baseline_by_gender,
            reform_by_gender=reform_by_gender,
        )

    def _build_poverty_by_race_output(
        self,
        *,
        baseline_by_race: object,
        reform_by_race: object,
    ) -> PovertyByRaceOutput:
        return simulation_output_poverty.build_poverty_by_race_output(
            baseline_by_race=baseline_by_race,
            reform_by_race=reform_by_race,
        )

    def _build_congressional_district_impact(
        self,
    ) -> CongressionalDistrictImpactOutput | None:
        with self._span(ANNUAL_IMPACT_STAGES.name(Stage.OUTPUT_CONGRESSIONAL_DISTRICT)):
            return simulation_output_geographic.build_congressional_district_impact(
                self.country,
                self.baseline,
                self.reform,
                region_code=self.resolved_region_code,
            )

    def _uk_area_breakdown_inputs(
        self,
    ) -> tuple[Any, Any, UKLocalAuthorityMetadata | None]:
        """Return the baseline, reform and boundaries the UK area breakdowns use.

        The impact analysis computes the run's own pair before any breakdown;
        a separate area pair is computed here, on first use (``ensure()``
        returns a computed simulation from policyengine.py's cache).
        """
        area = self.uk_area_simulations
        if area is None:
            return self.baseline, self.reform, self.uk_local_authority_metadata
        area.baseline.ensure()
        area.reform.ensure()
        return area.baseline, area.reform, area.local_authority_metadata

    def _uk_geography_disclosure_output(
        self,
    ) -> simulation_output_geographic.UKGeographyDisclosureOutput | None:
        """Return UK area changes under disclosure control, computed once.

        Constituencies and local authorities go through one release, so the
        disclosure check sees both. The release reads the pair the area
        breakdowns use (see ``_uk_area_breakdown_inputs``). None for other
        countries.
        """
        if self.country != "uk":
            return None
        if self._uk_geography_disclosure is None:
            baseline, reform, _ = self._uk_area_breakdown_inputs()
            self._uk_geography_disclosure = (
                simulation_output_geographic.compute_uk_geography_disclosure_output(
                    self.country,
                    baseline,
                    reform,
                    year=self._simulation_year(),
                )
            )
        return self._uk_geography_disclosure

    def _simulation_year(self) -> int:
        """Return the simulated year, which labels the disclosure release."""
        for value in (
            self.simulation_params.get("time_period"),
            self.simulation_params.get("year"),
            getattr(self.dataset, "year", None),
        ):
            if value is None:
                continue
            try:
                return int(value)
            except (TypeError, ValueError):
                continue
        return 0

    def _build_uk_constituency_impact(self) -> GeographicImpactOutput | None:
        with self._span(ANNUAL_IMPACT_STAGES.name(Stage.OUTPUT_UK_CONSTITUENCY)):
            if self.omit_uk_area_breakdowns:
                return None
            return simulation_output_geographic.build_uk_constituency_impact(
                self.country, self._uk_geography_disclosure_output()
            )

    def _build_uk_local_authority_impact(self) -> GeographicImpactOutput | None:
        with self._span(ANNUAL_IMPACT_STAGES.name(Stage.OUTPUT_UK_LOCAL_AUTHORITY)):
            if self.omit_uk_area_breakdowns:
                return None
            metadata = self._uk_area_breakdown_inputs()[2]
            return simulation_output_geographic.build_uk_local_authority_impact(
                self.country,
                self._uk_geography_disclosure_output(),
                uk_local_authority_metadata=metadata,
            )

    def _model_version(self) -> str:
        with self._span(ANNUAL_IMPACT_STAGES.name(Stage.OUTPUT_MODEL_VERSION)):
            return str(getattr(self.country_module.model, "version", ""))

    def _data_version(self) -> str:
        with self._span(ANNUAL_IMPACT_STAGES.name(Stage.OUTPUT_DATA_VERSION)):
            if self.resolved_data_version:
                return str(self.resolved_data_version)
            if self.simulation_params.get("data_version"):
                return str(self.simulation_params["data_version"])
            metadata = getattr(self.dataset, "metadata", {}) or {}
            for key in ("data_version", "version"):
                value = metadata.get(key)
                if value is not None:
                    return str(value)
            try:
                return get_country_release_bundle(self.country).data_version
            except ValueError:
                pass
            return ""
