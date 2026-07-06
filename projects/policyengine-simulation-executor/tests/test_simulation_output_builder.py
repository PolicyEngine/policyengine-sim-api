"""Tests for building PolicyEngine v4 outputs into API-v2 macro results."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pandas as pd
import pytest
from policyengine_observability import (
    ObservabilityConfig,
    ObservabilityRuntime,
    set_observability_runtime,
)

from fixtures.test_simulation_api_contracts import (
    CURRENT_SINGLE_YEAR_MACRO_KEYS,
    CURRENT_SINGLE_YEAR_MACRO_RESULT,
)
from fixtures.test_simulation_output_builder import (
    BASELINE_POVERTY_BY_AGE,
    BASELINE_POVERTY_BY_GENDER,
    BASELINE_POVERTY_BY_RACE,
    INTRA_DECILE_COLLECTION,
    REFORM_POVERTY_BY_AGE,
    REFORM_POVERTY_BY_GENDER,
    REFORM_POVERTY_BY_RACE,
    FakeModelOutput,
    fake_analysis,
)
from policyengine_simulation_executor.release_bundle import BUNDLE_RECEIPT_FILENAME
from policyengine_simulation_executor.release_bundle import get_country_release_bundle
from policyengine_simulation_observability.observability import SegmentName
from policyengine_simulation_executor.simulation_runtime import RegionResolution
from policyengine_simulation_executor.simulation_runtime import _load_dataset
from policyengine_simulation_executor.simulation_runtime import _normalise_policy
from policyengine_simulation_executor.simulation_runtime import (
    _resolve_dataset_reference,
)
from policyengine_simulation_executor.simulation_runtime import _resolve_region
from policyengine_simulation_executor.simulation_runtime import (
    _run_simulation_impl_core,
)
from policyengine_simulation_executor.simulation_runtime import run_simulation_impl
from policyengine_simulation_executor.simulation_macro_output import (
    AgePovertyOutput,
    BaselineReformValue,
    BudgetaryImpact,
    BudgetaryOutput,
    DecileOutput,
    DetailedBudgetOutput,
    CongressionalDistrictImpactOutput,
    GenderPovertyOutput,
    InequalityOutput,
    IntraDecileOutput,
    LaborSupplyResponseOutput,
    PovertyByGenderOutput,
    PovertyModuleOutputs,
    PovertyOutput,
    SingleYearMacroOutput,
)
from policyengine_simulation_executor.simulation_output_geographic import (
    build_congressional_district_impact,
    build_congressional_district_impact_output,
)
from policyengine_simulation_executor.simulation_output_builder import (
    SimulationOutputBuilder,
)


class _FakeOutputDataset:
    def __init__(self, household):
        self.data = SimpleNamespace(household=household)


class _FakeSimulation:
    def __init__(self, household):
        self.output_dataset = _FakeOutputDataset(household)

    def ensure(self):
        raise AssertionError("test data is already materialized")


def _with_observability_timings(callback):
    runtime = ObservabilityRuntime(
        ObservabilityConfig(
            service_name="policyengine-simulation-executor-test",
            service_role="test",
            environment="test",
            otel_enabled=False,
        ),
        segment_registry=SegmentName,
    )
    set_observability_runtime(runtime)
    handle = runtime.start_operation("test_operation", flavor="unit")
    try:
        result = callback()
        operation = handle["operation"]
        return (
            result,
            dict(operation.timings_ms),
            dict(operation.timing_counts),
            [node.as_dict() for node in operation.segment_tree],
        )
    finally:
        runtime.end_operation(handle)
        set_observability_runtime(ObservabilityRuntime.disabled())


def _macro_baseline_reform():
    baseline = _FakeSimulation(
        pd.DataFrame(
            {
                "household_weight": [1.0, 1.0],
                "household_net_income": [400.0, 600.0],
                "household_tax": [50.0, 50.0],
                "household_benefits": [20.0, 30.0],
                "household_state_income_tax": [5.0, 5.0],
            }
        )
    )
    reform = _FakeSimulation(
        pd.DataFrame(
            {
                "household_weight": [1.0, 1.0],
                "household_net_income": [410.0, 620.0],
                "household_tax": [100.0, 100.0],
                "household_benefits": [35.0, 45.0],
                "household_state_income_tax": [15.0, 15.0],
            }
        )
    )
    return baseline, reform


def _simulation_output_builder(
    country: str,
    baseline,
    reform,
    analysis=None,
    include_cliffs: bool | None = None,
    resolved_region_code: str | None = "us",
) -> SimulationOutputBuilder:
    analysis = analysis or fake_analysis()

    def economic_impact_analysis(
        baseline_simulation,
        reform_simulation,
        *,
        include_cliff_impacts=False,
    ):
        return analysis

    simulation_params = {
        "country": country,
        "data_version": "1.115.5" if country == "us" else "1.55.10",
    }
    if include_cliffs is not None:
        simulation_params["include_cliffs"] = include_cliffs

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
        resolved_region_code=resolved_region_code,
    )


def _congressional_district_output() -> CongressionalDistrictImpactOutput:
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


def _stub_policyengine_output_calls(monkeypatch, baseline, reform) -> None:
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
            raise AssertionError(f"Unexpected poverty output: {name}")

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
        lambda self: (
            _congressional_district_output() if self.country == "us" else None
        ),
    )
    monkeypatch.setattr(
        SimulationOutputBuilder,
        "_build_uk_constituency_impact",
        lambda self: (
            self._build_geographic_impact_output([{"constituency_code": "E14000530"}])
            if self.country == "uk"
            else None
        ),
    )
    monkeypatch.setattr(
        SimulationOutputBuilder,
        "_build_uk_local_authority_impact",
        lambda self: (
            self._build_geographic_impact_output(
                [{"local_authority_code": "E06000001"}]
            )
            if self.country == "uk"
            else None
        ),
    )


def _build_schema_output(monkeypatch, *, country: str = "us") -> SingleYearMacroOutput:
    baseline, reform = _macro_baseline_reform()
    _stub_policyengine_output_calls(monkeypatch, baseline, reform)
    return _simulation_output_builder(country, baseline, reform).build()


def test_builder_returns_schema_modules_before_legacy_dict_dump(monkeypatch):
    output = _build_schema_output(monkeypatch)

    assert isinstance(output, SingleYearMacroOutput)
    assert isinstance(output.budget, BudgetaryOutput)
    assert isinstance(output.budget, BudgetaryImpact)
    assert isinstance(output.detailed_budget, DetailedBudgetOutput)
    assert isinstance(output.decile, DecileOutput)
    assert isinstance(output.intra_decile, IntraDecileOutput)
    assert isinstance(output.poverty, PovertyOutput)
    assert isinstance(
        output.congressional_district_impact, CongressionalDistrictImpactOutput
    )
    assert output.wealth_decile is None
    assert output.congressional_district_impact.districts[0].district == "AL-01"


def test_builder_returns_existing_single_year_macro_shape(monkeypatch):
    output = _build_schema_output(monkeypatch).model_dump(mode="json")

    assert set(output) == CURRENT_SINGLE_YEAR_MACRO_KEYS
    assert output == CURRENT_SINGLE_YEAR_MACRO_RESULT


def test_congressional_district_output_formats_policyengine_results():
    from policyengine.countries.us.data import US_STATE_FIPS

    output = build_congressional_district_impact_output(
        [
            {
                "district_geoid": 101,
                "state_fips": 1,
                "district_number": 1,
                "average_household_income_change": 10,
                "relative_household_income_change": "0.01",
                "winner_percentage": 0.6,
                "loser_percentage": 0.3,
                "no_change_percentage": 0.1,
                "population": 1000,
            },
            {
                "district_geoid": 200,
                "average_household_income_change": 20,
                "relative_household_income_change": 0.02,
                "winner_percentage": 0.7,
                "loser_percentage": 0.2,
                "no_change_percentage": 0.1,
                "population": 2000,
            },
            {
                "state_fips": 6,
                "district_number": 12,
                "average_household_income_change": 30,
                "relative_household_income_change": 0.03,
                "winner_percentage": 0.8,
                "loser_percentage": 0.1,
                "no_change_percentage": 0.1,
                "population": 3000,
            },
            {
                "district_geoid": US_STATE_FIPS["DC"] * 100,
                "average_household_income_change": 40,
                "relative_household_income_change": 0.04,
                "winner_percentage": 0.9,
                "loser_percentage": 0.05,
                "no_change_percentage": 0.05,
                "population": 4000,
            },
        ]
    )

    assert output is not None
    assert output.model_dump(mode="json") == {
        "districts": [
            {
                "district": "AL-01",
                "average_household_income_change": 10.0,
                "relative_household_income_change": 0.01,
                "winner_percentage": 0.6,
                "loser_percentage": 0.3,
                "no_change_percentage": 0.1,
                "population": 1000.0,
            },
            {
                "district": "AK-01",
                "average_household_income_change": 20.0,
                "relative_household_income_change": 0.02,
                "winner_percentage": 0.7,
                "loser_percentage": 0.2,
                "no_change_percentage": 0.1,
                "population": 2000.0,
            },
            {
                "district": "CA-12",
                "average_household_income_change": 30.0,
                "relative_household_income_change": 0.03,
                "winner_percentage": 0.8,
                "loser_percentage": 0.1,
                "no_change_percentage": 0.1,
                "population": 3000.0,
            },
            {
                "district": "DC-01",
                "average_household_income_change": 40.0,
                "relative_household_income_change": 0.04,
                "winner_percentage": 0.9,
                "loser_percentage": 0.05,
                "no_change_percentage": 0.05,
                "population": 4000.0,
            },
        ]
    }


def test_congressional_district_output_skips_narrow_regions():
    assert (
        build_congressional_district_impact(
            "us",
            object(),
            object(),
            region_code="congressional_district/AL-01",
        )
        is None
    )
    assert build_congressional_district_impact("uk", object(), object()) is None


def test_builder_passes_resolved_region_to_congressional_district_output(monkeypatch):
    baseline, reform = _macro_baseline_reform()
    calls = []

    def fake_build_congressional_district_impact(
        country, baseline_arg, reform_arg, *, region_code=None
    ):
        calls.append((country, baseline_arg, reform_arg, region_code))
        return CongressionalDistrictImpactOutput(districts=[])

    monkeypatch.setattr(
        "policyengine_simulation_executor.simulation_output_geographic.build_congressional_district_impact",
        fake_build_congressional_district_impact,
    )

    builder = _simulation_output_builder(
        "us",
        baseline,
        reform,
        resolved_region_code="state/ut",
    )

    assert builder._build_congressional_district_impact() == (
        CongressionalDistrictImpactOutput(districts=[])
    )
    assert calls == [("us", baseline, reform, "state/ut")]


def test_run_simulation_impl_records_runtime_timings_without_real_calculation(
    monkeypatch,
):
    dataset = object()
    country_module = SimpleNamespace(model=SimpleNamespace(version="1.715.2"))
    baseline_simulation = object()
    reform_simulation = object()
    build_calls = []

    def fake_build_simulation(params, *, dataset, policy, scoping_strategy=None):
        build_calls.append((params, dataset, policy, scoping_strategy))
        return baseline_simulation if len(build_calls) == 1 else reform_simulation

    class FakeSimulationOutputBuilder:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def serialize(self):
            return CURRENT_SINGLE_YEAR_MACRO_RESULT

    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS_JSON", raising=False)
    monkeypatch.delenv("GCP_CREDENTIALS_JSON", raising=False)
    monkeypatch.delenv("GOOGLE_CREDENTIALS", raising=False)
    monkeypatch.delenv("SERVICE_ACCOUNT_JSON", raising=False)
    monkeypatch.setattr(
        "policyengine_simulation_executor.simulation_runtime._country_module",
        lambda country: country_module,
    )
    monkeypatch.setattr(
        "policyengine_simulation_executor.simulation_runtime._resolve_region",
        lambda **kwargs: RegionResolution(
            code="us",
            dataset_reference="mock-dataset",
            scoping_strategy="mock-scoping",
        ),
    )
    monkeypatch.setattr(
        "policyengine_simulation_executor.simulation_runtime._load_dataset",
        lambda params, country_module, region_resolution: dataset,
    )
    monkeypatch.setattr(
        "policyengine_simulation_executor.simulation_runtime._build_simulation",
        fake_build_simulation,
    )
    monkeypatch.setattr(
        "policyengine_simulation_executor.simulation_runtime.SimulationOutputBuilder",
        FakeSimulationOutputBuilder,
    )

    result, timings, counts, segment_tree = _with_observability_timings(
        lambda: run_simulation_impl(
            {
                "country": "us",
                "baseline": {"gov.test.parameter": {"2026-01-01": 1}},
                "reform": {"gov.test.parameter": {"2026-01-01": 2}},
            }
        )
    )

    assert result == CURRENT_SINGLE_YEAR_MACRO_RESULT
    assert set(timings) >= {
        SegmentName.CREDENTIAL_SETUP,
        SegmentName.REQUEST_PARSE,
        SegmentName.COUNTRY_MODULE_LOAD,
        SegmentName.REGION_RESOLUTION,
        SegmentName.DATASET_LOAD,
        SegmentName.POLICY_NORMALIZATION,
        SegmentName.SIMULATION_BUILD,
    }
    assert counts[SegmentName.SIMULATION_BUILD] == 2
    assert [node["name"] for node in segment_tree] == [
        SegmentName.CREDENTIAL_SETUP,
        SegmentName.REQUEST_PARSE,
        SegmentName.COUNTRY_MODULE_LOAD,
        SegmentName.REGION_RESOLUTION,
        SegmentName.DATASET_LOAD,
        SegmentName.POLICY_NORMALIZATION,
        SegmentName.SIMULATION_BUILD,
        SegmentName.SIMULATION_BUILD,
    ]
    simulation_builds = [
        node for node in segment_tree if node["name"] == SegmentName.SIMULATION_BUILD
    ]
    assert [node["attrs"] for node in simulation_builds] == [
        {"simulation_kind": "baseline"},
        {"simulation_kind": "reform"},
    ]
    assert build_calls == [
        (
            {
                "country": "us",
                "baseline": {"gov.test.parameter": {"2026-01-01": 1}},
                "reform": {"gov.test.parameter": {"2026-01-01": 2}},
            },
            dataset,
            {"gov.test.parameter": {"2026-01-01": 1}},
            "mock-scoping",
        ),
        (
            {
                "country": "us",
                "baseline": {"gov.test.parameter": {"2026-01-01": 1}},
                "reform": {"gov.test.parameter": {"2026-01-01": 2}},
            },
            dataset,
            {"gov.test.parameter": {"2026-01-01": 2}},
            "mock-scoping",
        ),
    ]


def test_builder_records_output_timings_without_real_calculation(monkeypatch):
    baseline = object()
    reform = object()
    analysis = SimpleNamespace(
        wealth_decile_impacts=object(),
        intra_wealth_decile_impacts=object(),
    )
    analysis_calls = []

    def economic_impact_analysis(
        baseline_simulation,
        reform_simulation,
        *,
        include_cliff_impacts=False,
    ):
        analysis_calls.append(
            (baseline_simulation, reform_simulation, include_cliff_impacts)
        )
        return analysis

    value = BaselineReformValue(baseline=0.0, reform=0.0)
    age_poverty = AgePovertyOutput(
        child=value,
        adult=value,
        senior=value,
        all=value,
    )
    gender_poverty = GenderPovertyOutput(male=value, female=value)
    poverty_outputs = PovertyModuleOutputs(
        poverty=PovertyOutput(
            poverty=age_poverty,
            deep_poverty=age_poverty,
        ),
        poverty_by_gender=PovertyByGenderOutput(
            poverty=gender_poverty,
            deep_poverty=gender_poverty,
        ),
        poverty_by_race=None,
    )
    output_calls = []

    def record(name, value):
        def wrapper(*args, **kwargs):
            output_calls.append(name)
            return value

        return wrapper

    monkeypatch.setattr(
        "policyengine_simulation_executor.simulation_output_budget.build_budgetary_impact",
        record(
            "budgetary_impact",
            BudgetaryImpact(
                tax_revenue_impact=0.0,
                state_tax_revenue_impact=0.0,
                benefit_spending_impact=0.0,
                budgetary_impact=0.0,
                households=0.0,
                baseline_net_income=0.0,
            ),
        ),
    )
    monkeypatch.setattr(
        "policyengine_simulation_executor.simulation_output_budget.build_detailed_budget",
        record("detailed_budget", DetailedBudgetOutput({})),
    )
    monkeypatch.setattr(
        "policyengine_simulation_executor.simulation_output_distribution.build_decile",
        record("decile", DecileOutput(average={}, relative={})),
    )
    monkeypatch.setattr(
        "policyengine_simulation_executor.simulation_output_inequality.build_inequality",
        record(
            "inequality",
            InequalityOutput(
                gini=value,
                top_10_pct_share=value,
                top_1_pct_share=value,
            ),
        ),
    )
    monkeypatch.setattr(
        "policyengine_simulation_executor.simulation_output_poverty.build_poverty_outputs",
        record("poverty", poverty_outputs),
    )
    monkeypatch.setattr(
        "policyengine_simulation_executor.simulation_output_distribution.build_intra_decile_output",
        record("intra_decile", IntraDecileOutput(deciles={}, all={})),
    )
    monkeypatch.setattr(
        "policyengine_simulation_executor.simulation_output_distribution.build_wealth_decile",
        record("wealth_decile", None),
    )
    monkeypatch.setattr(
        "policyengine_simulation_executor.simulation_output_distribution.build_intra_wealth_decile",
        record("intra_wealth_decile", None),
    )
    monkeypatch.setattr(
        "policyengine_simulation_executor.simulation_output_labor.build_labor_supply_response",
        record("labor_supply", LaborSupplyResponseOutput({})),
    )
    monkeypatch.setattr(
        "policyengine_simulation_executor.simulation_output_cliff.build_cliff_impact",
        record("cliff", None),
    )
    monkeypatch.setattr(
        "policyengine_simulation_executor.simulation_output_geographic.build_congressional_district_impact",
        record(
            "congressional_district", CongressionalDistrictImpactOutput(districts=[])
        ),
    )
    monkeypatch.setattr(
        "policyengine_simulation_executor.simulation_output_geographic.build_uk_constituency_impact",
        record("uk_constituency", None),
    )
    monkeypatch.setattr(
        "policyengine_simulation_executor.simulation_output_geographic.build_uk_local_authority_impact",
        record("uk_local_authority", None),
    )

    builder = SimulationOutputBuilder(
        country="us",
        simulation_params={"country": "us", "data_version": "mock-data-version"},
        country_module=SimpleNamespace(
            model=SimpleNamespace(version="mock-model-version"),
            economic_impact_analysis=economic_impact_analysis,
        ),
        dataset=SimpleNamespace(metadata={}),
        baseline=baseline,
        reform=reform,
    )

    result, timings, counts, segment_tree = _with_observability_timings(
        builder.serialize
    )

    assert result["model_version"] == "mock-model-version"
    assert result["data_version"] == "mock-data-version"
    assert analysis_calls == [(baseline, reform, False)]
    assert set(output_calls) == {
        "budgetary_impact",
        "detailed_budget",
        "decile",
        "inequality",
        "poverty",
        "intra_decile",
        "wealth_decile",
        "intra_wealth_decile",
        "labor_supply",
        "cliff",
        "congressional_district",
        "uk_constituency",
        "uk_local_authority",
    }
    assert set(timings) >= {
        SegmentName.CALCULATION,
        SegmentName.SIMULATION_OUTPUT_BUILD,
        SegmentName.ECONOMIC_IMPACT_ANALYSIS,
        SegmentName.OUTPUT_MODEL_VERSION,
        SegmentName.OUTPUT_DATA_VERSION,
        SegmentName.OUTPUT_BUDGETARY_IMPACT,
        SegmentName.OUTPUT_DETAILED_BUDGET,
        SegmentName.OUTPUT_DECILE,
        SegmentName.OUTPUT_INEQUALITY,
        SegmentName.OUTPUT_POVERTY,
        SegmentName.OUTPUT_INTRA_DECILE,
        SegmentName.OUTPUT_WEALTH_DECILE,
        SegmentName.OUTPUT_INTRA_WEALTH_DECILE,
        SegmentName.OUTPUT_LABOR_SUPPLY,
        SegmentName.OUTPUT_CONGRESSIONAL_DISTRICT,
        SegmentName.OUTPUT_UK_CONSTITUENCY,
        SegmentName.OUTPUT_UK_LOCAL_AUTHORITY,
        SegmentName.OUTPUT_CLIFF,
        SegmentName.RESPONSE_SERIALIZATION,
        SegmentName.SIMULATION_OUTPUT_MODEL_DUMP,
    }
    assert counts[SegmentName.ECONOMIC_IMPACT_ANALYSIS] == 1
    assert counts[SegmentName.SIMULATION_OUTPUT_MODEL_DUMP] == 1
    assert [node["name"] for node in segment_tree] == [
        SegmentName.CALCULATION,
        SegmentName.RESPONSE_SERIALIZATION,
    ]
    calculation_children = segment_tree[0]["children"]
    assert [node["name"] for node in calculation_children] == [
        SegmentName.SIMULATION_OUTPUT_BUILD
    ]
    output_build_children = calculation_children[0]["children"]
    assert {node["name"] for node in output_build_children} >= {
        SegmentName.OUTPUT_BUDGETARY_IMPACT,
        SegmentName.OUTPUT_DETAILED_BUDGET,
        SegmentName.OUTPUT_DECILE,
        SegmentName.OUTPUT_INEQUALITY,
        SegmentName.OUTPUT_POVERTY,
        SegmentName.OUTPUT_INTRA_DECILE,
        SegmentName.OUTPUT_WEALTH_DECILE,
        SegmentName.OUTPUT_INTRA_WEALTH_DECILE,
        SegmentName.OUTPUT_LABOR_SUPPLY,
        SegmentName.OUTPUT_CONGRESSIONAL_DISTRICT,
        SegmentName.OUTPUT_UK_CONSTITUENCY,
        SegmentName.OUTPUT_UK_LOCAL_AUTHORITY,
        SegmentName.OUTPUT_CLIFF,
        SegmentName.OUTPUT_MODEL_VERSION,
        SegmentName.OUTPUT_DATA_VERSION,
    }
    poverty_node = next(
        node
        for node in output_build_children
        if node["name"] == SegmentName.OUTPUT_POVERTY
    )
    assert poverty_node["children"][0]["name"] == (SegmentName.ECONOMIC_IMPACT_ANALYSIS)
    assert segment_tree[1]["children"][0]["name"] == (
        SegmentName.SIMULATION_OUTPUT_MODEL_DUMP
    )


def test_builder_maps_uk_wealth_outputs_and_omits_us_only_race(monkeypatch):
    output = _build_schema_output(monkeypatch, country="uk").model_dump(mode="json")

    assert output["poverty_by_race"] is None
    assert output["wealth_decile"] == {
        "average": {"1": 30.0},
        "relative": {"1": 0.03},
    }
    assert output["intra_wealth_decile"]["deciles"]["Lose more than 5%"] == [0.1]
    assert output["constituency_impact"] == [{"constituency_code": "E14000530"}]
    assert output["local_authority_impact"] == [{"local_authority_code": "E06000001"}]


def test_builder_calls_policyengine_economic_impact_analysis():
    baseline, reform = _macro_baseline_reform()
    analysis = fake_analysis()
    calls = []

    def economic_impact_analysis(
        baseline_simulation,
        reform_simulation,
        *,
        include_cliff_impacts=False,
    ):
        calls.append((baseline_simulation, reform_simulation, include_cliff_impacts))
        return analysis

    country_module = SimpleNamespace(
        model=SimpleNamespace(version="1.715.2"),
        economic_impact_analysis=economic_impact_analysis,
    )
    builder = SimulationOutputBuilder(
        country="us",
        simulation_params={"country": "us", "data_version": "1.115.5"},
        country_module=country_module,
        dataset=SimpleNamespace(metadata={}),
        baseline=baseline,
        reform=reform,
    )

    assert builder.analysis is analysis
    assert builder.analysis is analysis
    assert calls == [(baseline, reform, False)]


def test_builder_passes_include_cliffs_to_policyengine_economic_impact_analysis():
    baseline, reform = _macro_baseline_reform()
    analysis = fake_analysis()
    calls = []

    def economic_impact_analysis(
        baseline_simulation,
        reform_simulation,
        *,
        include_cliff_impacts=False,
    ):
        calls.append(include_cliff_impacts)
        return analysis

    country_module = SimpleNamespace(
        model=SimpleNamespace(version="1.715.2"),
        economic_impact_analysis=economic_impact_analysis,
    )
    builder = SimulationOutputBuilder(
        country="us",
        simulation_params={
            "country": "us",
            "data_version": "1.115.5",
            "include_cliffs": True,
        },
        country_module=country_module,
        dataset=SimpleNamespace(metadata={}),
        baseline=baseline,
        reform=reform,
    )

    assert builder.analysis is analysis
    assert calls == [True]


def test_builder_serializes_cliff_impact_when_requested(monkeypatch):
    baseline, reform = _macro_baseline_reform()
    _stub_policyengine_output_calls(monkeypatch, baseline, reform)
    analysis = fake_analysis()
    analysis.cliff_impact = FakeModelOutput(
        {
            "baseline": {"cliff_gap": 10.0, "cliff_share": 0.25},
            "reform": {"cliff_gap": 20.0, "cliff_share": 0.5},
        }
    )

    output = _simulation_output_builder(
        "us",
        baseline,
        reform,
        analysis=analysis,
        include_cliffs=True,
    ).build()

    assert output.cliff_impact is not None
    assert output.model_dump(mode="json")["cliff_impact"] == {
        "baseline": {"cliff_gap": 10.0, "cliff_share": 0.25},
        "reform": {"cliff_gap": 20.0, "cliff_share": 0.5},
    }


def test_normalise_policy_converts_legacy_period_range_keys():
    assert _normalise_policy({"gov.test.parameter": {"2026-01-01.2100-12-31": 1}}) == {
        "gov.test.parameter": {"2026-01-01": 1}
    }


def test_run_simulation_impl_core_builds_and_serializes_macro_output(monkeypatch):
    dataset = object()
    country_module = SimpleNamespace(model=SimpleNamespace(version="1.715.2"))
    baseline_simulation = object()
    reform_simulation = object()
    build_calls = []
    builder_calls = []

    def fake_country_module(country):
        assert country == "us"
        return country_module

    def fake_build_simulation(params, *, dataset, policy, scoping_strategy=None):
        build_calls.append((params, dataset, policy, scoping_strategy))
        return baseline_simulation if len(build_calls) == 1 else reform_simulation

    class FakeSimulationOutputBuilder:
        def __init__(self, **kwargs):
            builder_calls.append(kwargs)

        def serialize(self):
            return CURRENT_SINGLE_YEAR_MACRO_RESULT

    monkeypatch.setattr(
        "policyengine_simulation_executor.simulation_runtime._country_module",
        fake_country_module,
    )
    monkeypatch.setattr(
        "policyengine_simulation_executor.simulation_runtime._resolve_region",
        lambda **kwargs: RegionResolution(code="us", dataset_reference="dataset"),
    )
    monkeypatch.setattr(
        "policyengine_simulation_executor.simulation_runtime._load_dataset",
        lambda params, country_module, region_resolution: dataset,
    )
    monkeypatch.setattr(
        "policyengine_simulation_executor.simulation_runtime._build_simulation",
        fake_build_simulation,
    )
    monkeypatch.setattr(
        "policyengine_simulation_executor.simulation_runtime.SimulationOutputBuilder",
        FakeSimulationOutputBuilder,
    )

    result = _run_simulation_impl_core(
        {
            "country": "us",
            "baseline": {"gov.test.parameter": {"2026-01-01.2100-12-31": 1}},
            "reform": {"gov.test.parameter": {"2026-01-01.2100-12-31": 2}},
        }
    )

    assert result == CURRENT_SINGLE_YEAR_MACRO_RESULT
    assert build_calls[0][2] == {"gov.test.parameter": {"2026-01-01": 1}}
    assert build_calls[1][2] == {"gov.test.parameter": {"2026-01-01": 2}}
    assert build_calls[0][3] is None
    assert build_calls[1][3] is None
    assert builder_calls == [
        {
            "country": "us",
            "simulation_params": {
                "country": "us",
                "baseline": {"gov.test.parameter": {"2026-01-01.2100-12-31": 1}},
                "reform": {"gov.test.parameter": {"2026-01-01.2100-12-31": 2}},
            },
            "country_module": country_module,
            "dataset": dataset,
            "baseline": baseline_simulation,
            "reform": reform_simulation,
            "resolved_data_version": None,
            "resolved_region_code": "us",
        }
    ]


def test_run_simulation_impl_core_passes_region_scoping_to_simulations(monkeypatch):
    dataset = object()
    country_module = SimpleNamespace(model=SimpleNamespace(version="1.715.2"))
    baseline_simulation = object()
    reform_simulation = object()
    scoping_strategy = object()
    region_resolution = RegionResolution(
        code="state/ut",
        dataset_reference="dataset",
        scoping_strategy=scoping_strategy,
    )
    build_calls = []

    def fake_build_simulation(params, *, dataset, policy, scoping_strategy=None):
        build_calls.append((params, dataset, policy, scoping_strategy))
        return baseline_simulation if len(build_calls) == 1 else reform_simulation

    class FakeSimulationOutputBuilder:
        def __init__(self, **kwargs):
            pass

        def serialize(self):
            return CURRENT_SINGLE_YEAR_MACRO_RESULT

    monkeypatch.setattr(
        "policyengine_simulation_executor.simulation_runtime._country_module",
        lambda country: country_module,
    )
    monkeypatch.setattr(
        "policyengine_simulation_executor.simulation_runtime._resolve_region",
        lambda **kwargs: region_resolution,
    )
    monkeypatch.setattr(
        "policyengine_simulation_executor.simulation_runtime._load_dataset",
        lambda params, country_module, region_resolution: dataset,
    )
    monkeypatch.setattr(
        "policyengine_simulation_executor.simulation_runtime._build_simulation",
        fake_build_simulation,
    )
    monkeypatch.setattr(
        "policyengine_simulation_executor.simulation_runtime.SimulationOutputBuilder",
        FakeSimulationOutputBuilder,
    )

    result = _run_simulation_impl_core(
        {
            "country": "us",
            "region": "state/ut",
            "baseline": {},
            "reform": {},
        }
    )

    assert result == CURRENT_SINGLE_YEAR_MACRO_RESULT
    assert build_calls[0][3] is scoping_strategy
    assert build_calls[1][3] is scoping_strategy


def test_resolve_dataset_reference_applies_data_version_to_logical_dataset(
    monkeypatch,
):
    bundle_uri = get_country_release_bundle("us").default_dataset_uri
    bundle_uri_without_revision = bundle_uri.rsplit("@", maxsplit=1)[0]

    assert (
        _resolve_dataset_reference(
            "us",
            {"data": "populace_us_2024", "data_version": "custom-v1"},
        )
        == f"{bundle_uri_without_revision}@custom-v1"
    )


@pytest.mark.parametrize("country", ["us", "uk"])
def test_load_dataset_passes_bundle_default_name_to_country_loader_with_receipt(
    country,
    tmp_path,
    monkeypatch,
):
    bundle = get_country_release_bundle(country)
    installed_dataset_path = tmp_path / f"{bundle.default_dataset}.h5"
    installed_dataset_path.write_bytes(b"data")
    receipt_path = tmp_path / BUNDLE_RECEIPT_FILENAME
    receipt_path.write_text(
        json.dumps(
            {
                "bundle_version": "4.18.3",
                "policyengine_version": "4.18.3",
                "datasets": [
                    {
                        "country": country,
                        "dataset": bundle.default_dataset,
                        "version": bundle.data_version,
                        "path": str(installed_dataset_path),
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("POLICYENGINE_BUNDLE_RECEIPT", str(receipt_path))
    monkeypatch.setenv("POLICYENGINE_DATA_FOLDER", str(tmp_path))
    get_country_release_bundle.cache_clear()

    dataset = SimpleNamespace()
    ensure_calls = []

    def ensure_datasets(**kwargs):
        ensure_calls.append(kwargs)
        return {"dataset": dataset}

    assert (
        _load_dataset(
            {"country": country, "time_period": "2026"},
            country_module=SimpleNamespace(ensure_datasets=ensure_datasets),
        )
        is dataset
    )

    assert ensure_calls == [
        {
            "datasets": [bundle.default_dataset],
            "years": [2026],
            "data_folder": str(tmp_path),
        }
    ]


# TEMPORARY: remove once single-year datasets are published (issue #596).
# Pins the guard that keeps every non-default dataset request away from the
# baked default-revision single-year files in POLICYENGINE_DATA_FOLDER.
# ensure_datasets keys its cache on a revision-stripped filename stem, so
# even an explicit dataset name or foreign URI can collide with the baked
# default (e.g. hf://other/repo/populace_us_2024.h5).
@pytest.mark.parametrize(
    "explicit_data_params",
    [
        {"data_version": "custom-v1"},
        {"data": "populace_us_2024@custom-v1"},
        {"data": "populace_us_2024"},
        {"data": "hf://other-org/other-repo/populace_us_2024.h5"},
    ],
)
def test_load_dataset_bypasses_baked_folder_for_explicit_dataset_requests(
    explicit_data_params,
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("POLICYENGINE_DATA_FOLDER", str(tmp_path))

    ensure_calls = []

    def ensure_datasets(**kwargs):
        ensure_calls.append(kwargs)
        return {"dataset": SimpleNamespace()}

    _load_dataset(
        {"country": "us", "time_period": "2026", **explicit_data_params},
        country_module=SimpleNamespace(ensure_datasets=ensure_datasets),
    )

    assert len(ensure_calls) == 1
    assert ensure_calls[0]["data_folder"] == "/tmp/policyengine-data"


def test_load_dataset_uses_baked_folder_for_default_requests(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("POLICYENGINE_DATA_FOLDER", str(tmp_path))

    ensure_calls = []

    def ensure_datasets(**kwargs):
        ensure_calls.append(kwargs)
        return {"dataset": SimpleNamespace()}

    _load_dataset(
        {"country": "us", "time_period": "2026"},
        country_module=SimpleNamespace(ensure_datasets=ensure_datasets),
    )

    assert ensure_calls[0]["data_folder"] == str(tmp_path)


def test_resolve_region_scopes_us_state_from_national_populace_dataset():
    bundle = get_country_release_bundle("us")
    scoping_strategy = object()
    state = SimpleNamespace(
        dataset_path=None,
        scoping_strategy=scoping_strategy,
        parent_code="us",
    )
    national = SimpleNamespace(
        dataset_path=bundle.default_dataset_uri,
        scoping_strategy=None,
        parent_code=None,
    )
    regions = {"state/ut": state, "us": national}
    country_module = SimpleNamespace(
        model=SimpleNamespace(get_region=lambda code: regions.get(code))
    )

    resolution = _resolve_region(
        country_module=country_module,
        country="us",
        params={"region": "state/UT"},
    )

    assert resolution.code == "state/ut"
    assert resolution.dataset_reference == bundle.default_dataset_uri
    assert resolution.scoping_strategy is scoping_strategy


def test_resolve_region_scopes_us_congressional_district_from_national_dataset():
    bundle = get_country_release_bundle("us")
    scoping_strategy = object()
    district = SimpleNamespace(
        dataset_path=None,
        scoping_strategy=scoping_strategy,
        parent_code="state/ut",
    )
    state = SimpleNamespace(
        dataset_path=None,
        scoping_strategy=object(),
        parent_code="us",
    )
    national = SimpleNamespace(
        dataset_path=bundle.default_dataset_uri,
        scoping_strategy=None,
        parent_code=None,
    )
    regions = {
        "congressional_district/UT-01": district,
        "state/ut": state,
        "us": national,
    }
    country_module = SimpleNamespace(
        model=SimpleNamespace(get_region=lambda code: regions.get(code))
    )

    resolution = _resolve_region(
        country_module=country_module,
        country="us",
        params={"region": "congressional_district/ut-01"},
    )

    assert resolution.code == "congressional_district/UT-01"
    assert resolution.dataset_reference == bundle.default_dataset_uri
    assert resolution.scoping_strategy is scoping_strategy


def test_resolve_region_rejects_unscoped_us_place_region():
    place = SimpleNamespace(
        dataset_path=None,
        scoping_strategy=None,
        parent_code="state/ut",
    )
    state = SimpleNamespace(
        dataset_path=None,
        scoping_strategy=object(),
        parent_code="us",
    )
    regions = {"place/UT-67000": place, "state/ut": state}
    country_module = SimpleNamespace(
        model=SimpleNamespace(get_region=lambda code: regions.get(code))
    )

    with pytest.raises(ValueError, match="US place regions are not yet supported"):
        _resolve_region(
            country_module=country_module,
            country="us",
            params={"region": "place/ut-67000"},
        )


def test_resolve_region_scopes_uk_country_from_national_populace_dataset():
    bundle = get_country_release_bundle("uk")
    scoping_strategy = object()
    england = SimpleNamespace(
        dataset_path=None,
        scoping_strategy=scoping_strategy,
        parent_code="uk",
    )
    uk = SimpleNamespace(
        dataset_path=bundle.default_dataset_uri,
        scoping_strategy=None,
        parent_code=None,
    )
    regions = {"country/england": england, "uk": uk}
    country_module = SimpleNamespace(
        model=SimpleNamespace(get_region=lambda code: regions.get(code))
    )

    resolution = _resolve_region(
        country_module=country_module,
        country="uk",
        params={"region": "England"},
    )

    assert resolution.code == "country/england"
    assert resolution.dataset_reference == bundle.default_dataset_uri
    assert resolution.scoping_strategy is scoping_strategy


def test_builder_data_version_prefers_resolved_revision_then_dataset_metadata():
    baseline, reform = _macro_baseline_reform()

    def economic_impact_analysis(
        baseline_simulation,
        reform_simulation,
        *,
        include_cliff_impacts=False,
    ):
        return fake_analysis()

    country_module = SimpleNamespace(
        model=SimpleNamespace(version="1.715.2"),
        economic_impact_analysis=economic_impact_analysis,
    )

    resolved_builder = SimulationOutputBuilder(
        country="us",
        simulation_params={"country": "us"},
        country_module=country_module,
        dataset=SimpleNamespace(metadata={"version": "metadata-version"}),
        baseline=baseline,
        reform=reform,
        resolved_data_version="1.77.0",
    )
    metadata_builder = SimulationOutputBuilder(
        country="us",
        simulation_params={"country": "us"},
        country_module=country_module,
        dataset=SimpleNamespace(metadata={"version": "metadata-version"}),
        baseline=baseline,
        reform=reform,
    )

    assert resolved_builder._data_version() == "1.77.0"
    assert metadata_builder._data_version() == "metadata-version"


def test_builder_budgetary_impact_uses_materialized_columns_and_uk_state_tax_zero():
    baseline = _FakeSimulation(
        pd.DataFrame(
            {
                "household_weight": [1.0, 2.0],
                "household_net_income": [100.0, 200.0],
                "household_tax": [20.0, 40.0],
                "household_benefits": [5.0, 10.0],
                "household_state_income_tax": [2.0, 3.0],
            }
        )
    )
    reform = _FakeSimulation(
        pd.DataFrame(
            {
                "household_weight": [1.0, 2.0],
                "household_net_income": [120.0, 210.0],
                "household_tax": [25.0, 50.0],
                "household_benefits": [4.0, 8.0],
                "household_state_income_tax": [4.0, 6.0],
            }
        )
    )

    us_budget = _simulation_output_builder(
        "us", baseline, reform
    )._build_budgetary_impact()
    uk_budget = _simulation_output_builder(
        "uk", baseline, reform
    )._build_budgetary_impact()

    assert isinstance(us_budget, BudgetaryImpact)
    assert us_budget.model_dump(mode="json") == {
        "tax_revenue_impact": 15.0,
        "state_tax_revenue_impact": 5.0,
        "benefit_spending_impact": -3.0,
        "budgetary_impact": 18.0,
        "households": 3.0,
        "baseline_net_income": 300.0,
    }
    assert uk_budget.state_tax_revenue_impact == 0.0


def test_builder_budgetary_impact_propagates_required_calculation_errors(monkeypatch):
    baseline, reform = _macro_baseline_reform()

    def fail_change_output_variable(*args, **kwargs):
        raise RuntimeError("household_tax missing")

    monkeypatch.setattr(
        "policyengine_simulation_executor.simulation_output_budget._change_output_variable",
        fail_change_output_variable,
    )

    with pytest.raises(RuntimeError, match="household_tax missing"):
        _simulation_output_builder("us", baseline, reform)._build_budgetary_impact()


def test_uk_constituency_impact_uses_policyengine_output_function(monkeypatch):
    baseline = object()
    reform = object()
    expected = [{"constituency_code": "E14000530"}]

    def fake_output_module_function(module_name, name):
        assert module_name == "constituency_impact"
        assert name == "compute_uk_constituency_impacts"

        def compute(baseline_simulation, reform_simulation):
            assert baseline_simulation is baseline
            assert reform_simulation is reform
            return SimpleNamespace(constituency_results=expected)

        return compute

    monkeypatch.setattr(
        "policyengine_simulation_executor.simulation_output_geographic._output_module_function",
        fake_output_module_function,
    )

    assert (
        _simulation_output_builder("uk", baseline, reform)
        ._build_uk_constituency_impact()
        .root
        == expected
    )
    assert (
        _simulation_output_builder(
            "us", baseline, reform
        )._build_uk_constituency_impact()
        is None
    )


def test_uk_local_authority_impact_uses_policyengine_output_function(monkeypatch):
    baseline = object()
    reform = object()
    expected = [{"local_authority_code": "E06000001"}]

    def fake_output_module_function(module_name, name):
        assert module_name == "local_authority_impact"
        assert name == "compute_uk_local_authority_impacts"

        def compute(baseline_simulation, reform_simulation):
            assert baseline_simulation is baseline
            assert reform_simulation is reform
            return SimpleNamespace(local_authority_results=expected)

        return compute

    monkeypatch.setattr(
        "policyengine_simulation_executor.simulation_output_geographic._output_module_function",
        fake_output_module_function,
    )

    assert (
        _simulation_output_builder("uk", baseline, reform)
        ._build_uk_local_authority_impact()
        .root
        == expected
    )
    assert (
        _simulation_output_builder(
            "us", baseline, reform
        )._build_uk_local_authority_impact()
        is None
    )
