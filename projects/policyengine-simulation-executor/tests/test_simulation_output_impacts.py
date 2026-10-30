"""Tests for the economic, cliff, budgetary and UK area impacts the builder reports."""

from types import SimpleNamespace

import pytest
from fixtures.test_simulation_output_builder import FakeModelOutput, fake_analysis
from fixtures.weighted_frames import weighted_entity_frame
from policyengine_simulation_contract.uk_geography import (
    UKLocalAuthorityBoundaryVersion,
    UKLocalAuthorityMetadata,
)
from policyengine_simulation_executor import simulation_output_geographic
from policyengine_simulation_executor.simulation_macro_output import (
    BudgetaryImpact,
    GeographicImpactOutput,
)
from policyengine_simulation_executor.simulation_output_builder import (
    SimulationOutputBuilder,
)
from policyengine_simulation_executor.simulation_runtime import normalise_policy
from test_simulation_output_fakes import (
    BuilderOptions,
    FakeSimulation,
    local_authority_impact_record,
    macro_baseline_reform,
    make_output_builder,
    stub_policyengine_output_calls,
)


def test_builder_calls_policyengine_economic_impact_analysis():
    baseline, reform = macro_baseline_reform()
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
    baseline, reform = macro_baseline_reform()
    analysis = fake_analysis()
    calls = []

    def economic_impact_analysis(
        _baseline_simulation,
        _reform_simulation,
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
    baseline, reform = macro_baseline_reform()
    stub_policyengine_output_calls(monkeypatch, baseline, reform)
    analysis = fake_analysis()
    analysis.cliff_impact = FakeModelOutput(
        {
            "baseline": {"cliff_gap": 10.0, "cliff_share": 0.25},
            "reform": {"cliff_gap": 20.0, "cliff_share": 0.5},
        }
    )

    output = make_output_builder(
        "us",
        baseline,
        reform,
        BuilderOptions(analysis=analysis, include_cliffs=True),
    ).build()

    assert output.cliff_impact is not None
    assert output.model_dump(mode="json")["cliff_impact"] == {
        "baseline": {"cliff_gap": 10.0, "cliff_share": 0.25},
        "reform": {"cliff_gap": 20.0, "cliff_share": 0.5},
    }


def test_normalise_policy_preserves_legacy_period_range_keys():
    assert normalise_policy({"gov.test.parameter": {"2026-01-01.2100-12-31": 1}}) == {
        "gov.test.parameter": {"2026-01-01.2100-12-31": 1}
    }


def test_builder_data_version_prefers_resolved_revision_then_dataset_metadata():
    baseline, reform = macro_baseline_reform()

    def economic_impact_analysis(
        _baseline_simulation,
        _reform_simulation,
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
    baseline = FakeSimulation(
        weighted_entity_frame(
            {
                "household_weight": [1.0, 2.0],
                "household_net_income": [100.0, 200.0],
                "household_tax": [20.0, 40.0],
                "household_benefits": [5.0, 10.0],
                "household_state_income_tax": [2.0, 3.0],
            }
        )
    )
    reform = FakeSimulation(
        weighted_entity_frame(
            {
                "household_weight": [1.0, 2.0],
                "household_net_income": [120.0, 210.0],
                "household_tax": [25.0, 50.0],
                "household_benefits": [4.0, 8.0],
                "household_state_income_tax": [4.0, 6.0],
            }
        )
    )

    us_budget = make_output_builder("us", baseline, reform)._build_budgetary_impact()
    uk_budget = make_output_builder("uk", baseline, reform)._build_budgetary_impact()

    # Changes are weighted sums, weight * (reform - baseline), over the
    # weights 1 and 2; the household count is a plain sum of the weights.
    tax_revenue_impact = 1.0 * (25.0 - 20.0) + 2.0 * (50.0 - 40.0)
    state_tax_revenue_impact = 1.0 * (4.0 - 2.0) + 2.0 * (6.0 - 3.0)
    benefit_spending_impact = 1.0 * (4.0 - 5.0) + 2.0 * (8.0 - 10.0)
    assert isinstance(us_budget, BudgetaryImpact)
    assert us_budget.model_dump(mode="json") == {
        "tax_revenue_impact": tax_revenue_impact,
        "state_tax_revenue_impact": state_tax_revenue_impact,
        "benefit_spending_impact": benefit_spending_impact,
        "budgetary_impact": tax_revenue_impact - benefit_spending_impact,
        "households": 1.0 + 2.0,
        "baseline_net_income": 1.0 * 100.0 + 2.0 * 200.0,
    }
    assert uk_budget.state_tax_revenue_impact == 0.0


def test_builder_budgetary_impact_propagates_required_calculation_errors(monkeypatch):
    baseline, reform = macro_baseline_reform()

    def fail_change_output_variable(*_args, **_kwargs):
        msg = "household_tax missing"
        raise RuntimeError(msg)

    monkeypatch.setattr(
        "policyengine_simulation_executor.simulation_output_budget._change_output_variable",
        fail_change_output_variable,
    )

    with pytest.raises(RuntimeError, match="household_tax missing"):
        make_output_builder("us", baseline, reform)._build_budgetary_impact()


def test_uk_area_impacts_share_one_disclosure_release(monkeypatch):
    baseline = object()
    reform = object()
    disclosure = simulation_output_geographic.UKGeographyDisclosureOutput()
    computed: list[tuple[str, object, object, int]] = []
    built: list[tuple[str, object]] = []

    def compute(country, baseline_simulation, reform_simulation, *, year):
        computed.append((country, baseline_simulation, reform_simulation, year))
        return disclosure

    def build_constituencies(country, value):
        built.append(("constituency", value))
        return GeographicImpactOutput.model_validate([])

    def build_authorities(country, value, **kwargs):
        built.append(("local_authority", value))
        return GeographicImpactOutput.model_validate([])

    monkeypatch.setattr(
        "policyengine_simulation_executor.simulation_output_geographic."
        "compute_uk_geography_disclosure_output",
        compute,
    )
    monkeypatch.setattr(
        "policyengine_simulation_executor.simulation_output_geographic."
        "build_uk_constituency_impact",
        build_constituencies,
    )
    monkeypatch.setattr(
        "policyengine_simulation_executor.simulation_output_geographic."
        "build_uk_local_authority_impact",
        build_authorities,
    )

    builder = make_output_builder("uk", baseline, reform)
    builder.simulation_params["time_period"] = "2026"
    builder._build_uk_constituency_impact()
    builder._build_uk_local_authority_impact()

    # Both levels come from one release, computed once.
    assert computed == [("uk", baseline, reform, 2026)]
    assert built == [("constituency", disclosure), ("local_authority", disclosure)]


def test_uk_disclosure_is_not_computed_for_other_countries(monkeypatch):
    monkeypatch.setattr(
        "policyengine_simulation_executor.simulation_output_geographic."
        "compute_uk_geography_disclosure_output",
        lambda *_args, **_kwargs: pytest.fail("US runs need no UK disclosure"),
    )
    builder = make_output_builder("us", object(), object())

    assert builder._build_uk_constituency_impact() is None
    assert builder._build_uk_local_authority_impact() is None


def test_uk_local_authority_impact_receives_detected_boundary_metadata(monkeypatch):
    expected = [local_authority_impact_record().model_dump(mode="json")]
    metadata = UKLocalAuthorityMetadata(
        boundary_version=UKLocalAuthorityBoundaryVersion.LAD23
    )
    disclosure = simulation_output_geographic.UKGeographyDisclosureOutput()
    calls: list[tuple[object, UKLocalAuthorityMetadata | None]] = []

    def build(country, value, **kwargs):
        if country != "uk":
            return None
        calls.append((value, kwargs["uk_local_authority_metadata"]))
        return GeographicImpactOutput.model_validate(expected)

    monkeypatch.setattr(
        "policyengine_simulation_executor.simulation_output_geographic."
        "compute_uk_geography_disclosure_output",
        lambda *_args, **_kwargs: disclosure,
    )
    monkeypatch.setattr(
        "policyengine_simulation_executor.simulation_output_geographic."
        "build_uk_local_authority_impact",
        build,
    )

    result = make_output_builder(
        "uk",
        object(),
        object(),
        BuilderOptions(uk_local_authority_metadata=metadata),
    )._build_uk_local_authority_impact()
    assert result is not None
    assert result.model_dump(mode="json") == expected
    assert calls == [(disclosure, metadata)]


def test_omitted_uk_area_breakdowns_compute_nothing(monkeypatch):
    for name in (
        "compute_uk_geography_disclosure_output",
        "build_uk_constituency_impact",
        "build_uk_local_authority_impact",
    ):
        monkeypatch.setattr(
            f"policyengine_simulation_executor.simulation_output_geographic.{name}",
            lambda *_args, **_kwargs: pytest.fail("no area breakdown is built"),
        )
    builder = make_output_builder(
        "uk",
        object(),
        object(),
        BuilderOptions(resolved_region_code="uk", omit_uk_area_breakdowns=True),
    )

    assert builder._build_uk_constituency_impact() is None
    assert builder._build_uk_local_authority_impact() is None
