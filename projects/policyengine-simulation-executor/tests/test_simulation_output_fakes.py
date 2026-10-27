"""Fake simulations, runtimes and output builders shared by the output tests.

It holds no tests. Its name matches tests/test_simulation_output_*.py, the files
sim.microdata.builder-tests-use-weighted-frames checks, so the entity frames it
builds stay checked.
"""

from collections import Counter
from contextlib import AbstractContextManager
from dataclasses import dataclass
from types import SimpleNamespace

from fixtures.test_simulation_output_builder import (
    BASELINE_POVERTY_BY_AGE,
    BASELINE_POVERTY_BY_GENDER,
    BASELINE_POVERTY_BY_RACE,
    INTRA_DECILE_COLLECTION,
    REFORM_POVERTY_BY_AGE,
    REFORM_POVERTY_BY_GENDER,
    REFORM_POVERTY_BY_RACE,
    fake_analysis,
)
from fixtures.weighted_frames import weighted_entity_frame
from policyengine_simulation_contract.uk_geography import UKLocalAuthorityMetadata
from policyengine_simulation_executor.simulation_macro_output import (
    CongressionalDistrictImpactOutput,
    ConstituencyImpactRecord,
    LocalAuthorityImpactRecord,
    SingleYearMacroOutput,
)
from policyengine_simulation_executor.simulation_output_builder import (
    SimulationOutputBuilder,
)


class FakeOutputDataset:
    def __init__(self, household):
        self.data = SimpleNamespace(household=household)


class FakeSimulation:
    def __init__(self, household):
        self.output_dataset = FakeOutputDataset(household)

    def ensure(self):
        msg = "test data is already materialized"
        raise AssertionError(msg)


class TrackedSpan(AbstractContextManager):
    def __init__(self, runtime, name, attributes):
        self.runtime = runtime
        self.node = {
            "name": name,
            "attrs": dict(attributes or {}),
            "children": [],
        }

    def __enter__(self):
        target = (
            self.runtime.stack[-1]["children"]
            if self.runtime.stack
            else self.runtime.roots
        )
        target.append(self.node)
        self.runtime.stack.append(self.node)
        self.runtime.names.append(self.node["name"])
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.runtime.stack.pop()


class TrackingRuntime:
    def __init__(self):
        self.roots = []
        self.stack = []
        self.names = []
        self.context = []

    def span(self, name, *, attributes=None):
        return TrackedSpan(self, name, attributes)

    def set_context(self, **attributes):
        self.context.extend(attributes.items())


def with_observability_timings(callback):
    runtime = TrackingRuntime()
    result = callback(runtime)
    counts = Counter(runtime.names)
    return result, set(runtime.names), counts, runtime.roots


def macro_baseline_reform():
    """Build households whose weighted totals are the golden macro result's budget.

    With weights 0.5 and 1.5, the weighted totals are a tax change of 100, a
    benefit change of 30, a state tax change of 20, 2 households and a baseline
    net income of 1000. Unweighted sums of the same rows would give 120, 40, 24
    and 1200, and summing the weights through the weighted frame (#767) would
    give 2.5 households, so the golden result only matches a correct build.
    """
    baseline = FakeSimulation(
        weighted_entity_frame(
            {
                "household_weight": [0.5, 1.5],
                "household_net_income": [800.0, 400.0],
                "household_tax": [60.0, 40.0],
                "household_benefits": [20.0, 30.0],
                "household_state_income_tax": [5.0, 5.0],
            }
        )
    )
    reform = FakeSimulation(
        weighted_entity_frame(
            {
                "household_weight": [0.5, 1.5],
                "household_net_income": [810.0, 420.0],
                "household_tax": [140.0, 80.0],
                "household_benefits": [50.0, 40.0],
                "household_state_income_tax": [21.0, 13.0],
            }
        )
    )
    return baseline, reform


@dataclass(frozen=True)
class BuilderOptions:
    """Optional inputs for a SimulationOutputBuilder built from fakes."""

    analysis: object = None
    include_cliffs: bool | None = None
    resolved_region_code: str | None = "us"
    uk_local_authority_metadata: UKLocalAuthorityMetadata | None = None
    omit_uk_area_breakdowns: bool = False


def make_output_builder(
    country: str,
    baseline,
    reform,
    options: BuilderOptions | None = None,
) -> SimulationOutputBuilder:
    options = options or BuilderOptions()
    analysis = options.analysis or fake_analysis()

    def economic_impact_analysis(
        _baseline_simulation,
        _reform_simulation,
        *,
        include_cliff_impacts=False,
    ):
        return analysis

    simulation_params = {
        "country": country,
        "data_version": "1.115.5" if country == "us" else "1.55.10",
    }
    if options.include_cliffs is not None:
        simulation_params["include_cliffs"] = options.include_cliffs

    country_module = SimpleNamespace(
        model=SimpleNamespace(version="1.715.2" if country == "us" else "2.88.20"),
        economic_impact_analysis=economic_impact_analysis,
    )
    return SimulationOutputBuilder(
        country=country,
        simulation_params=simulation_params,
        country_module=country_module,
        dataset=SimpleNamespace(metadata={}),
        baseline=baseline,
        reform=reform,
        resolved_region_code=options.resolved_region_code,
        uk_local_authority_metadata=options.uk_local_authority_metadata,
        omit_uk_area_breakdowns=options.omit_uk_area_breakdowns,
    )


def congressional_district_output() -> CongressionalDistrictImpactOutput:
    return CongressionalDistrictImpactOutput(
        districts=[
            {
                "district": "AL-01",
                "average_household_income_change": 10.0,
                "relative_household_income_change": 0.01,
                "winner_percentage": 0.6,
                "loser_percentage": 0.3,
                "no_change_percentage": 0.1,
                "population": 1000.0,
            }
        ]
    )


def constituency_impact_record() -> ConstituencyImpactRecord:
    return ConstituencyImpactRecord(
        constituency_code="E14000530",
        constituency_name="Birmingham, Ladywood",
        x=34,
        y=-42,
        average_household_income_change=10.0,
        relative_household_income_change=0.01,
        population=1000.0,
    )


def local_authority_impact_record() -> LocalAuthorityImpactRecord:
    return LocalAuthorityImpactRecord(
        local_authority_code="E06000001",
        local_authority_name="Hartlepool",
        x=63,
        y=-19,
        average_household_income_change=12.0,
        relative_household_income_change=0.02,
        population=900.0,
    )


def stub_policyengine_output_calls(monkeypatch, baseline, _reform) -> None:
    def fake_poverty_module_function(name):
        def compute(simulation):
            if "by_age" in name:
                return (
                    BASELINE_POVERTY_BY_AGE
                    if simulation is baseline
                    else REFORM_POVERTY_BY_AGE
                )
            if "by_gender" in name:
                return (
                    BASELINE_POVERTY_BY_GENDER
                    if simulation is baseline
                    else REFORM_POVERTY_BY_GENDER
                )
            if "by_race" in name:
                return (
                    BASELINE_POVERTY_BY_RACE
                    if simulation is baseline
                    else REFORM_POVERTY_BY_RACE
                )
            msg = f"Unexpected poverty output: {name}"
            raise AssertionError(msg)

        return compute

    monkeypatch.setattr(
        "policyengine_simulation_executor.simulation_output_poverty._poverty_module_function",
        fake_poverty_module_function,
    )
    monkeypatch.setattr(
        SimulationOutputBuilder,
        "_build_intra_decile_output",
        lambda self: self._build_intra_decile_output_from_collection(
            INTRA_DECILE_COLLECTION
        ),
    )
    monkeypatch.setattr(
        SimulationOutputBuilder,
        "_build_congressional_district_impact",
        lambda self: congressional_district_output() if self.country == "us" else None,
    )
    monkeypatch.setattr(
        SimulationOutputBuilder,
        "_build_uk_constituency_impact",
        lambda self: (
            self._build_geographic_impact_output(
                [constituency_impact_record().model_dump(mode="json")]
            )
            if self.country == "uk"
            else None
        ),
    )
    monkeypatch.setattr(
        SimulationOutputBuilder,
        "_build_uk_local_authority_impact",
        lambda self: (
            self._build_geographic_impact_output(
                [local_authority_impact_record().model_dump(mode="json")]
            )
            if self.country == "uk"
            else None
        ),
    )


def build_schema_output(monkeypatch, *, country: str = "us") -> SingleYearMacroOutput:
    baseline, reform = macro_baseline_reform()
    stub_policyengine_output_calls(monkeypatch, baseline, reform)
    return make_output_builder(country, baseline, reform).build()
