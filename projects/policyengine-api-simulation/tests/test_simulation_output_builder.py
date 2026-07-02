"""Tests for building PolicyEngine v4 outputs into API-v2 macro results."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pandas as pd
import pytest

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
from policyengine_api_simulation.release_bundle import BUNDLE_RECEIPT_FILENAME
from policyengine_api_simulation.release_bundle import get_country_release_bundle
from policyengine_api_simulation.simulation_runtime import RegionResolution
from policyengine_api_simulation.simulation_runtime import _load_dataset
from policyengine_api_simulation.simulation_runtime import _normalise_policy
from policyengine_api_simulation.simulation_runtime import _resolve_dataset_reference
from policyengine_api_simulation.simulation_runtime import _resolve_region
from policyengine_api_simulation.simulation_runtime import _run_simulation_impl_core
from policyengine_api_simulation.simulation_macro_output import (
    BudgetaryImpact,
    BudgetaryOutput,
    DecileOutput,
    DetailedBudgetOutput,
    GeographicImpactOutput,
    IntraDecileOutput,
    PovertyOutput,
    SingleYearMacroOutput,
)
from policyengine_api_simulation.simulation_output_builder import (
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
        "policyengine_api_simulation.simulation_output_poverty._poverty_module_function",
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
            self._build_geographic_impact_output([{"district_geoid": 101}])
            if self.country == "us"
            else None
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
    assert isinstance(output.congressional_district_impact, GeographicImpactOutput)
    assert output.wealth_decile is None
    assert output.congressional_district_impact.root == [{"district_geoid": 101}]


def test_builder_returns_existing_single_year_macro_shape(monkeypatch):
    output = _build_schema_output(monkeypatch).model_dump(mode="json")

    assert set(output) == CURRENT_SINGLE_YEAR_MACRO_KEYS
    assert output == CURRENT_SINGLE_YEAR_MACRO_RESULT


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
        "policyengine_api_simulation.simulation_runtime._country_module",
        fake_country_module,
    )
    monkeypatch.setattr(
        "policyengine_api_simulation.simulation_runtime._resolve_region",
        lambda **kwargs: RegionResolution(code="us", dataset_reference="dataset"),
    )
    monkeypatch.setattr(
        "policyengine_api_simulation.simulation_runtime._load_dataset",
        lambda params, country_module, region_resolution: dataset,
    )
    monkeypatch.setattr(
        "policyengine_api_simulation.simulation_runtime._build_simulation",
        fake_build_simulation,
    )
    monkeypatch.setattr(
        "policyengine_api_simulation.simulation_runtime.SimulationOutputBuilder",
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
        "policyengine_api_simulation.simulation_runtime._country_module",
        lambda country: country_module,
    )
    monkeypatch.setattr(
        "policyengine_api_simulation.simulation_runtime._resolve_region",
        lambda **kwargs: region_resolution,
    )
    monkeypatch.setattr(
        "policyengine_api_simulation.simulation_runtime._load_dataset",
        lambda params, country_module, region_resolution: dataset,
    )
    monkeypatch.setattr(
        "policyengine_api_simulation.simulation_runtime._build_simulation",
        fake_build_simulation,
    )
    monkeypatch.setattr(
        "policyengine_api_simulation.simulation_runtime.SimulationOutputBuilder",
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
# Pins the guard that keeps revision-pinned requests away from the baked
# default-revision single-year files in POLICYENGINE_DATA_FOLDER.
@pytest.mark.parametrize(
    "revision_params",
    [
        {"data_version": "custom-v1"},
        {"data": "populace_us_2024@custom-v1"},
    ],
)
def test_load_dataset_bypasses_baked_folder_for_revision_overrides(
    revision_params,
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("POLICYENGINE_DATA_FOLDER", str(tmp_path))

    ensure_calls = []

    def ensure_datasets(**kwargs):
        ensure_calls.append(kwargs)
        return {"dataset": SimpleNamespace()}

    _load_dataset(
        {"country": "us", "time_period": "2026", **revision_params},
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
        "policyengine_api_simulation.simulation_output_budget._change_output_variable",
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
        "policyengine_api_simulation.simulation_output_geographic._output_module_function",
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
        "policyengine_api_simulation.simulation_output_geographic._output_module_function",
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
