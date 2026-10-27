"""Tests for the simulation output builder's schema, regions and timings."""

from types import SimpleNamespace

import pytest
from fixtures.macro_output import (
    CURRENT_SINGLE_YEAR_MACRO_KEYS,
    CURRENT_SINGLE_YEAR_MACRO_RESULT,
)
from fixtures.test_simulation_output_builder import FakeCollection
from policyengine_simulation_executor.simulation_macro_output import (
    AgePovertyOutput,
    BaselineReformValue,
    BudgetaryImpact,
    BudgetaryOutput,
    CongressionalDistrictImpactOutput,
    DecileOutput,
    DetailedBudgetOutput,
    GenderPovertyOutput,
    InequalityOutput,
    IntraDecileOutput,
    PovertyByGenderOutput,
    PovertyModuleOutputs,
    PovertyOutput,
    SingleYearMacroOutput,
)
from policyengine_simulation_executor.simulation_output_builder import (
    SimulationOutputBuilder,
)
from policyengine_simulation_executor.simulation_output_distribution import (
    build_decile_output,
)
from policyengine_simulation_executor.simulation_output_geographic import (
    build_congressional_district_impact,
    build_congressional_district_impact_output,
)
from policyengine_simulation_observability.stages import Stage
from test_simulation_output_fakes import (
    BuilderOptions,
    build_schema_output,
    macro_baseline_reform,
    make_output_builder,
    with_observability_timings,
)


def test_builder_returns_schema_modules_before_legacy_dict_dump(monkeypatch):
    output = build_schema_output(monkeypatch)

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
    output = build_schema_output(monkeypatch).model_dump(mode="json")

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
    baseline, reform = macro_baseline_reform()
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

    builder = make_output_builder(
        "us",
        baseline,
        reform,
        BuilderOptions(resolved_region_code="state/ut"),
    )

    assert builder._build_congressional_district_impact() == (
        CongressionalDistrictImpactOutput(districts=[])
    )
    assert calls == [("us", baseline, reform, "state/ut")]


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
        def wrapper(*_args, **_kwargs):
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
        record("labor_supply", None),
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

    def serialize(runtime):
        builder.runtime = runtime
        return builder.serialize()

    result, timings, counts, segment_tree = with_observability_timings(serialize)

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
        Stage.CALCULATION,
        Stage.SIMULATION_OUTPUT_BUILD,
        Stage.ECONOMIC_IMPACT_ANALYSIS,
        Stage.OUTPUT_MODEL_VERSION,
        Stage.OUTPUT_DATA_VERSION,
        Stage.OUTPUT_BUDGETARY_IMPACT,
        Stage.OUTPUT_DETAILED_BUDGET,
        Stage.OUTPUT_DECILE,
        Stage.OUTPUT_INEQUALITY,
        Stage.OUTPUT_POVERTY,
        Stage.OUTPUT_INTRA_DECILE,
        Stage.OUTPUT_WEALTH_DECILE,
        Stage.OUTPUT_INTRA_WEALTH_DECILE,
        Stage.OUTPUT_LABOR_SUPPLY,
        Stage.OUTPUT_CONGRESSIONAL_DISTRICT,
        Stage.OUTPUT_UK_CONSTITUENCY,
        Stage.OUTPUT_UK_LOCAL_AUTHORITY,
        Stage.OUTPUT_CLIFF,
        Stage.RESPONSE_SERIALIZATION,
        Stage.SIMULATION_OUTPUT_MODEL_DUMP,
    }
    assert counts[Stage.ECONOMIC_IMPACT_ANALYSIS] == 1
    assert counts[Stage.SIMULATION_OUTPUT_MODEL_DUMP] == 1
    assert [node["name"] for node in segment_tree] == [
        Stage.CALCULATION,
        Stage.RESPONSE_SERIALIZATION,
    ]
    calculation_children = segment_tree[0]["children"]
    assert [node["name"] for node in calculation_children] == [
        Stage.SIMULATION_OUTPUT_BUILD
    ]
    output_build_children = calculation_children[0]["children"]
    assert {node["name"] for node in output_build_children} >= {
        Stage.OUTPUT_BUDGETARY_IMPACT,
        Stage.OUTPUT_DETAILED_BUDGET,
        Stage.OUTPUT_DECILE,
        Stage.OUTPUT_INEQUALITY,
        Stage.OUTPUT_POVERTY,
        Stage.OUTPUT_INTRA_DECILE,
        Stage.OUTPUT_WEALTH_DECILE,
        Stage.OUTPUT_INTRA_WEALTH_DECILE,
        Stage.OUTPUT_LABOR_SUPPLY,
        Stage.OUTPUT_CONGRESSIONAL_DISTRICT,
        Stage.OUTPUT_UK_CONSTITUENCY,
        Stage.OUTPUT_UK_LOCAL_AUTHORITY,
        Stage.OUTPUT_CLIFF,
        Stage.OUTPUT_MODEL_VERSION,
        Stage.OUTPUT_DATA_VERSION,
    }
    poverty_node = next(
        node for node in output_build_children if node["name"] == Stage.OUTPUT_POVERTY
    )
    assert poverty_node["children"][0]["name"] == (Stage.ECONOMIC_IMPACT_ANALYSIS)
    assert segment_tree[1]["children"][0]["name"] == (
        Stage.SIMULATION_OUTPUT_MODEL_DUMP
    )


def test_decile_relative_is_a_fraction_of_the_baseline_mean():
    # policyengine.py reports relative_change as a percentage (1.1254 is
    # 1.1254%); the API's relative field is a fraction (0.011254).
    output = build_decile_output(
        FakeCollection(
            [
                {"decile": 1, "absolute_change": 180.7, "relative_change": 1.1254},
                {"decile": 10, "absolute_change": 1410.3, "relative_change": 0.8227},
                {"decile": 2, "absolute_change": 0.0, "relative_change": None},
            ]
        )
    )

    assert output.average == {"1": 180.7, "2": 0.0, "10": 1410.3}
    assert output.relative == pytest.approx({"1": 0.011254, "2": 0.0, "10": 0.008227})


def test_builder_maps_uk_wealth_outputs_and_omits_us_only_race(monkeypatch):
    output = build_schema_output(monkeypatch, country="uk").model_dump(mode="json")

    assert output["poverty_by_race"] is None
    assert output["wealth_decile"] == {
        "average": {"1": 30.0},
        "relative": {"1": 0.03},
    }
    assert output["intra_wealth_decile"]["deciles"]["Lose more than 5%"] == [0.1]
    assert output["constituency_impact"][0]["constituency_code"] == "E14000530"
    assert output["local_authority_impact"][0]["local_authority_code"] == "E06000001"
