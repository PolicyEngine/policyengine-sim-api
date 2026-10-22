"""Tests for building PolicyEngine v4 outputs into simulation API macro results."""

from __future__ import annotations

import json
from collections import Counter
from contextlib import AbstractContextManager
from dataclasses import dataclass
from types import SimpleNamespace

import pytest
from fixtures.macro_output import (
    CURRENT_SINGLE_YEAR_MACRO_KEYS,
    CURRENT_SINGLE_YEAR_MACRO_RESULT,
)
from fixtures.spm_doubles import (
    installed_spm_selection,
    spm_capable_simulation,
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
from fixtures.weighted_frames import weighted_entity_frame
from policyengine.core.scoping_strategy import RowFilterStrategy
from policyengine_simulation_contract.job_progress import JobStage
from policyengine_simulation_contract.uk_geography import (
    UKLocalAuthorityBoundaryVersion,
    UKLocalAuthorityMetadata,
)
from policyengine_simulation_executor import simulation_output_geographic
from policyengine_simulation_executor.job_progress import NULL_JOB_PROGRESS
from policyengine_simulation_executor.release_bundle import (
    BUNDLE_RECEIPT_FILENAME,
    get_country_release_bundle,
    resolve_runtime_bundle_dataset_uri,
)
from policyengine_simulation_executor.simulation_macro_output import (
    AgePovertyOutput,
    BaselineReformValue,
    BudgetaryImpact,
    BudgetaryOutput,
    CongressionalDistrictImpactOutput,
    ConstituencyImpactRecord,
    DecileOutput,
    DetailedBudgetOutput,
    GenderPovertyOutput,
    GeographicImpactOutput,
    InequalityOutput,
    IntraDecileOutput,
    LocalAuthorityImpactRecord,
    PovertyByGenderOutput,
    PovertyModuleOutputs,
    PovertyOutput,
    SingleYearMacroOutput,
)
from policyengine_simulation_executor.simulation_output_builder import (
    SimulationOutputBuilder,
)
from policyengine_simulation_executor.simulation_output_geographic import (
    build_congressional_district_impact,
    build_congressional_district_impact_output,
)
from policyengine_simulation_executor.simulation_runtime import (
    DatasetSelection,
    RegionResolution,
    _nondefault_data_folder,
    _normalise_policy,
    _resolve_dataset_reference,
    _resolve_dataset_selection,
    _resolve_region,
    _run_simulation_impl_core,
    resolve_data_folder,
    run_simulation_impl,
)
from policyengine_simulation_executor.simulation_runtime import (
    _load_dataset as _load_selected_dataset,
)
from policyengine_simulation_observability.stages import ANNUAL_IMPACT_STAGES, Stage


def _params_with_spm(params, selection):
    """Return request parameters after the runtime resolves SPM settings."""
    return params if selection is None else {**params, "spm": selection}


def _assert_macro_result(result, selection):
    """Check the ordinary macro result plus any installed SPM record."""
    spm_keys = ("spm_provenance",)
    assert {key: value for key, value in result.items() if key not in spm_keys} == {
        key: value
        for key, value in CURRENT_SINGLE_YEAR_MACRO_RESULT.items()
        if key not in spm_keys
    }
    if selection is None:
        assert all(result[key] is None for key in spm_keys)
        return
    assert set(result["spm_provenance"]) == {
        "schema_version",
        "baseline",
        "reform",
    }


def _load_dataset(params, *, country_module=None, region_resolution=None):
    """Exercise dataset resolution and loading together in direct loader tests."""
    selection = _resolve_dataset_selection(params, region_resolution=region_resolution)
    return _load_selected_dataset(
        params, country_module=country_module, selection=selection
    )


class _FakeOutputDataset:
    def __init__(self, household):
        self.data = SimpleNamespace(household=household)


class _FakeSimulation:
    def __init__(self, household):
        self.output_dataset = _FakeOutputDataset(household)

    def ensure(self):
        msg = "test data is already materialized"
        raise AssertionError(msg)


class _TrackedSpan(AbstractContextManager):
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


class _TrackingRuntime:
    def __init__(self):
        self.roots = []
        self.stack = []
        self.names = []
        self.context = []

    def span(self, name, *, attributes=None):
        return _TrackedSpan(self, name, attributes)

    def set_context(self, **attributes):
        self.context.extend(attributes.items())


def _with_observability_timings(callback):
    runtime = _TrackingRuntime()
    result = callback(runtime)
    counts = Counter(runtime.names)
    return result, set(runtime.names), counts, runtime.roots


def _macro_baseline_reform():
    """Build households whose weighted totals are the golden macro result's budget.

    With weights 0.5 and 1.5, the weighted totals are a tax change of 100, a
    benefit change of 30, a state tax change of 20, 2 households and a baseline
    net income of 1000. Unweighted sums of the same rows would give 120, 40, 24
    and 1200, and summing the weights through the weighted frame (#767) would
    give 2.5 households, so the golden result only matches a correct build.
    """
    baseline = _FakeSimulation(
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
    reform = _FakeSimulation(
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


def _simulation_output_builder(
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


def _constituency_impact_record() -> ConstituencyImpactRecord:
    return ConstituencyImpactRecord(
        constituency_code="E14000530",
        constituency_name="Birmingham, Ladywood",
        x=34,
        y=-42,
        average_household_income_change=10.0,
        relative_household_income_change=0.01,
        population=1000.0,
    )


def _local_authority_impact_record() -> LocalAuthorityImpactRecord:
    return LocalAuthorityImpactRecord(
        local_authority_code="E06000001",
        local_authority_name="Hartlepool",
        x=63,
        y=-19,
        average_household_income_change=12.0,
        relative_household_income_change=0.02,
        population=900.0,
    )


def _stub_policyengine_output_calls(monkeypatch, baseline, _reform) -> None:
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
        lambda self: _congressional_district_output() if self.country == "us" else None,
    )
    monkeypatch.setattr(
        SimulationOutputBuilder,
        "_build_uk_constituency_impact",
        lambda self: (
            self._build_geographic_impact_output(
                [_constituency_impact_record().model_dump(mode="json")]
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
                [_local_authority_impact_record().model_dump(mode="json")]
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
        BuilderOptions(resolved_region_code="state/ut"),
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
    selection = installed_spm_selection()
    baseline_simulation = spm_capable_simulation(selection)
    reform_simulation = spm_capable_simulation(selection)
    build_calls = []

    def fake_build_simulation(
        params,
        *,
        dataset,
        dataset_selection,
        policy,
        region,
    ):
        build_calls.append(
            (params, dataset, policy, region.scoping_strategy, region.code)
        )
        return baseline_simulation if len(build_calls) == 1 else reform_simulation

    class FakeSimulationOutputBuilder:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def serialize(self):
            return dict(CURRENT_SINGLE_YEAR_MACRO_RESULT)

    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS_JSON", raising=False)
    monkeypatch.delenv("GCP_CREDENTIALS_JSON", raising=False)
    monkeypatch.delenv("GOOGLE_CREDENTIALS", raising=False)
    monkeypatch.delenv("SERVICE_ACCOUNT_JSON", raising=False)
    monkeypatch.setattr(
        "policyengine_simulation_executor.simulation_runtime._country_module",
        lambda _country: country_module,
    )
    monkeypatch.setattr(
        "policyengine_simulation_executor.simulation_runtime._resolve_region",
        lambda **_kwargs: RegionResolution(
            code="us",
            dataset_reference=None,
            scoping_strategy="mock-scoping",
        ),
    )
    monkeypatch.setattr(
        "policyengine_simulation_executor.simulation_runtime._load_dataset",
        lambda _params, country_module, selection, data_folder=None: dataset,
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
        lambda runtime: run_simulation_impl(
            {
                "country": "us",
                "baseline": {"gov.test.parameter": {"2026-01-01": 1}},
                "reform": {"gov.test.parameter": {"2026-01-01": 2}},
            },
            runtime=runtime,
        )
    )

    _assert_macro_result(result, selection)
    assert set(timings) >= {
        Stage.CREDENTIAL_SETUP,
        Stage.REQUEST_PARSE,
        Stage.COUNTRY_MODULE_LOAD,
        Stage.REGION_RESOLUTION,
        Stage.DATASET_RESOLUTION,
        Stage.DATASET_LOAD,
        Stage.POLICY_NORMALIZATION,
        Stage.SIMULATION_BUILD,
    }
    assert counts[Stage.SIMULATION_BUILD] == 2
    assert [node["name"] for node in segment_tree] == [
        Stage.CREDENTIAL_SETUP,
        Stage.REQUEST_PARSE,
        Stage.COUNTRY_MODULE_LOAD,
        Stage.REGION_RESOLUTION,
        Stage.DATASET_RESOLUTION,
        Stage.DATASET_LOAD,
        Stage.POLICY_NORMALIZATION,
        Stage.SIMULATION_BUILD,
        Stage.SIMULATION_BUILD,
    ]
    simulation_builds = [
        node for node in segment_tree if node["name"] == Stage.SIMULATION_BUILD
    ]
    assert [node["attrs"] for node in simulation_builds] == [
        {"simulation_kind": "baseline"},
        {"simulation_kind": "reform"},
    ]
    # region_code must be the RESOLVED code: it feeds the deterministic
    # baseline-id predicate, and dropping it silently disables artifact
    # reuse (every baseline becomes a miss).
    built_params = _params_with_spm(
        {
            "country": "us",
            "baseline": {"gov.test.parameter": {"2026-01-01": 1}},
            "reform": {"gov.test.parameter": {"2026-01-01": 2}},
        },
        selection,
    )
    assert build_calls == [
        (
            built_params,
            dataset,
            {"gov.test.parameter": {"2026-01-01": 1}},
            "mock-scoping",
            "us",
        ),
        (
            built_params,
            dataset,
            {"gov.test.parameter": {"2026-01-01": 2}},
            "mock-scoping",
            "us",
        ),
    ]


def test_run_simulation_impl_exports_baseline_artifact_outcome(monkeypatch):
    """run_simulation_impl must export the baseline artifact outcome attribute.

    The hit/incomplete/miss attribute is the rollout metric for the artifact
    pipeline; losing the export would blind that measurement.
    """
    dataset = object()
    country_module = SimpleNamespace(model=SimpleNamespace(version="1.715.2"))
    selection = installed_spm_selection()
    simulations = {
        "baseline": spm_capable_simulation(selection, artifact_outcome="incomplete"),
        "reform": spm_capable_simulation(selection),
    }
    build_count = [0]

    def fake_build_simulation(
        _params,
        *,
        dataset,
        dataset_selection,
        policy,
        region,
    ):
        build_count[0] += 1
        return simulations["baseline"] if build_count[0] == 1 else simulations["reform"]

    class FakeSimulationOutputBuilder:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def serialize(self):
            return dict(CURRENT_SINGLE_YEAR_MACRO_RESULT)

    for env in (
        "GOOGLE_APPLICATION_CREDENTIALS",
        "GOOGLE_APPLICATION_CREDENTIALS_JSON",
        "GCP_CREDENTIALS_JSON",
        "GOOGLE_CREDENTIALS",
        "SERVICE_ACCOUNT_JSON",
    ):
        monkeypatch.delenv(env, raising=False)
    monkeypatch.setattr(
        "policyengine_simulation_executor.simulation_runtime._country_module",
        lambda _country: country_module,
    )
    monkeypatch.setattr(
        "policyengine_simulation_executor.simulation_runtime._resolve_region",
        lambda **_kwargs: RegionResolution(
            code="us",
            dataset_reference=None,
            scoping_strategy="mock-scoping",
        ),
    )
    monkeypatch.setattr(
        "policyengine_simulation_executor.simulation_runtime._load_dataset",
        lambda _params, country_module, selection, data_folder=None: dataset,
    )
    monkeypatch.setattr(
        "policyengine_simulation_executor.simulation_runtime._build_simulation",
        fake_build_simulation,
    )
    monkeypatch.setattr(
        "policyengine_simulation_executor.simulation_runtime.SimulationOutputBuilder",
        FakeSimulationOutputBuilder,
    )
    params = {
        "country": "us",
        "baseline": {"gov.test.parameter": {"2026-01-01": 1}},
        "reform": {"gov.test.parameter": {"2026-01-01": 2}},
    }
    runtime = _TrackingRuntime()
    run_simulation_impl(params, runtime=runtime)
    assert ("baseline_artifact", "incomplete") in runtime.context

    # A plain Simulation baseline (no artifact_outcome) must emit nothing.
    runtime.context.clear()
    build_count[0] = 0
    simulations["baseline"] = spm_capable_simulation(selection)
    run_simulation_impl(params, runtime=runtime)
    assert all(key != "baseline_artifact" for key, _ in runtime.context)


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

    result, timings, counts, segment_tree = _with_observability_timings(serialize)

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


def test_builder_maps_uk_wealth_outputs_and_omits_us_only_race(monkeypatch):
    output = _build_schema_output(monkeypatch, country="uk").model_dump(mode="json")

    assert output["poverty_by_race"] is None
    assert output["wealth_decile"] == {
        "average": {"1": 30.0},
        "relative": {"1": 0.03},
    }
    assert output["intra_wealth_decile"]["deciles"]["Lose more than 5%"] == [0.1]
    assert output["constituency_impact"][0]["constituency_code"] == "E14000530"
    assert output["local_authority_impact"][0]["local_authority_code"] == "E06000001"


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
        BuilderOptions(analysis=analysis, include_cliffs=True),
    ).build()

    assert output.cliff_impact is not None
    assert output.model_dump(mode="json")["cliff_impact"] == {
        "baseline": {"cliff_gap": 10.0, "cliff_share": 0.25},
        "reform": {"cliff_gap": 20.0, "cliff_share": 0.5},
    }


def test_normalise_policy_preserves_legacy_period_range_keys():
    assert _normalise_policy({"gov.test.parameter": {"2026-01-01.2100-12-31": 1}}) == {
        "gov.test.parameter": {"2026-01-01.2100-12-31": 1}
    }


def test_run_simulation_impl_core_builds_and_serializes_macro_output(monkeypatch):
    dataset = object()
    country_module = SimpleNamespace(model=SimpleNamespace(version="1.715.2"))
    selection = installed_spm_selection()
    baseline_simulation = spm_capable_simulation(selection)
    reform_simulation = spm_capable_simulation(selection)
    build_calls = []
    builder_calls = []

    def fake_country_module(country):
        assert country == "us"
        return country_module

    def fake_build_simulation(
        params,
        *,
        dataset,
        dataset_selection,
        policy,
        region,
    ):
        build_calls.append((params, dataset, policy, region.scoping_strategy))
        return baseline_simulation if len(build_calls) == 1 else reform_simulation

    class FakeSimulationOutputBuilder:
        def __init__(self, **kwargs):
            builder_calls.append(kwargs)

        def serialize(self):
            return dict(CURRENT_SINGLE_YEAR_MACRO_RESULT)

    monkeypatch.setattr(
        "policyengine_simulation_executor.simulation_runtime._country_module",
        fake_country_module,
    )
    monkeypatch.setattr(
        "policyengine_simulation_executor.simulation_runtime._resolve_region",
        lambda **_kwargs: RegionResolution(code="us", dataset_reference=None),
    )
    monkeypatch.setattr(
        "policyengine_simulation_executor.simulation_runtime._load_dataset",
        lambda _params, country_module, selection, data_folder=None: dataset,
    )
    monkeypatch.setattr(
        "policyengine_simulation_executor.simulation_runtime._build_simulation",
        fake_build_simulation,
    )
    monkeypatch.setattr(
        "policyengine_simulation_executor.simulation_runtime.SimulationOutputBuilder",
        FakeSimulationOutputBuilder,
    )

    runtime = _TrackingRuntime()
    result = _run_simulation_impl_core(
        {
            "country": "us",
            "baseline": {"gov.test.parameter": {"2026-01-01.2100-12-31": 1}},
            "reform": {"gov.test.parameter": {"2026-01-01.2100-12-31": 2}},
        },
        runtime=runtime,
    )

    _assert_macro_result(result, selection)
    assert build_calls[0][2] == {"gov.test.parameter": {"2026-01-01.2100-12-31": 1}}
    assert build_calls[1][2] == {"gov.test.parameter": {"2026-01-01.2100-12-31": 2}}
    assert build_calls[0][3] is None
    assert build_calls[1][3] is None
    assert builder_calls == [
        {
            "country": "us",
            "simulation_params": _params_with_spm(
                {
                    "country": "us",
                    "baseline": {"gov.test.parameter": {"2026-01-01.2100-12-31": 1}},
                    "reform": {"gov.test.parameter": {"2026-01-01.2100-12-31": 2}},
                },
                selection,
            ),
            "country_module": country_module,
            "dataset": dataset,
            "baseline": baseline_simulation,
            "reform": reform_simulation,
            "resolved_data_version": None,
            "resolved_region_code": "us",
            "runtime": runtime,
            "uk_local_authority_metadata": None,
            "omit_uk_area_breakdowns": False,
            "progress": NULL_JOB_PROGRESS,
        }
    ]


def test_run_simulation_impl_core_passes_region_scoping_to_simulations(monkeypatch):
    dataset = object()
    country_module = SimpleNamespace(model=SimpleNamespace(version="1.715.2"))
    selection = installed_spm_selection()
    baseline_simulation = spm_capable_simulation(selection)
    reform_simulation = spm_capable_simulation(selection)
    scoping_strategy = object()
    region_resolution = RegionResolution(
        code="state/ut",
        dataset_reference=None,
        scoping_strategy=scoping_strategy,
    )
    build_calls = []
    load_selections = []

    def fake_build_simulation(
        params,
        *,
        dataset,
        dataset_selection,
        policy,
        region,
    ):
        build_calls.append(
            (params, dataset, policy, region.scoping_strategy, dataset_selection)
        )
        return baseline_simulation if len(build_calls) == 1 else reform_simulation

    def fake_load_dataset(_params, *, country_module, selection, data_folder=None):
        load_selections.append(selection)
        return dataset

    class FakeSimulationOutputBuilder:
        def __init__(self, **kwargs):
            pass

        def serialize(self):
            return dict(CURRENT_SINGLE_YEAR_MACRO_RESULT)

    monkeypatch.setattr(
        "policyengine_simulation_executor.simulation_runtime._country_module",
        lambda _country: country_module,
    )
    monkeypatch.setattr(
        "policyengine_simulation_executor.simulation_runtime._resolve_region",
        lambda **_kwargs: region_resolution,
    )
    monkeypatch.setattr(
        "policyengine_simulation_executor.simulation_runtime._load_dataset",
        fake_load_dataset,
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
        },
        runtime=_TrackingRuntime(),
    )

    _assert_macro_result(result, selection)
    assert build_calls[0][3] is scoping_strategy
    assert build_calls[1][3] is scoping_strategy
    assert len(load_selections) == 1
    assert build_calls[0][4] is load_selections[0]
    assert build_calls[1][4] is load_selections[0]


class _StoppedAfterLoadingError(Exception):
    """Raised by a stubbed simulation build once the dataset has loaded."""


def _load_through_runtime(
    monkeypatch, *, region_resolution, selection, baked_folder, time_period=2026
):
    """Run the annual runtime up to its dataset load and return what it did."""
    from policyengine_simulation_executor import state_datasets

    load_calls = []
    lookups = []

    def fake_load_dataset(_params, *, country_module, selection, data_folder=None):
        load_calls.append({"selection": selection, "data_folder": data_folder})
        return object()

    def fake_baked_state_data_folder(**kwargs):
        lookups.append(kwargs)
        return baked_folder

    def stop_after_loading(*_args, **_kwargs):
        raise _StoppedAfterLoadingError

    monkeypatch.setattr(
        "policyengine_simulation_executor.simulation_runtime._country_module",
        lambda _country: SimpleNamespace(model=SimpleNamespace(version="2.2.1")),
    )
    monkeypatch.setattr(
        "policyengine_simulation_executor.simulation_runtime._resolve_region",
        lambda **_kwargs: region_resolution,
    )
    monkeypatch.setattr(
        "policyengine_simulation_executor.simulation_runtime._resolve_dataset_selection",
        lambda _params, *, region_resolution: selection,
    )
    monkeypatch.setattr(
        "policyengine_simulation_executor.simulation_runtime._load_dataset",
        fake_load_dataset,
    )
    monkeypatch.setattr(
        state_datasets, "baked_state_data_folder", fake_baked_state_data_folder
    )
    monkeypatch.setattr(
        "policyengine_simulation_executor.simulation_runtime._build_simulation",
        stop_after_loading,
    )
    runtime = _TrackingRuntime()
    with pytest.raises(_StoppedAfterLoadingError):
        _run_simulation_impl_core(
            {
                "country": "us",
                "region": region_resolution.code,
                "time_period": time_period,
                "baseline": {},
                "reform": {},
            },
            runtime=runtime,
        )
    return SimpleNamespace(
        load_calls=load_calls, lookups=lookups, context=dict(runtime.context)
    )


def _regional_selection():
    return DatasetSelection(
        name="populace_us_2024_acs_local",
        uri="hf://policyengine/populace-us/populace_us_2024_acs_local.h5@rev",
        is_default=False,
    )


def test_runtime_loads_a_state_from_its_baked_folder(monkeypatch):
    loaded = _load_through_runtime(
        monkeypatch,
        region_resolution=RegionResolution(
            code="state/ut", dataset_reference="ref", state_code="UT"
        ),
        selection=_regional_selection(),
        baked_folder="/opt/policyengine/data/state-datasets/acs/UT",
    )

    assert loaded.load_calls == [
        {
            "selection": _regional_selection(),
            "data_folder": "/opt/policyengine/data/state-datasets/acs/UT",
        }
    ]
    assert loaded.lookups == [
        {
            "country": "us",
            "selection": _regional_selection(),
            "state_code": "UT",
            "year": 2026,
            "data_folder": resolve_data_folder(),
        }
    ]
    assert loaded.context["dataset_source"] == "baked_state"


def test_runtime_prepares_a_state_on_demand_without_a_baked_file(monkeypatch):
    loaded = _load_through_runtime(
        monkeypatch,
        region_resolution=RegionResolution(
            code="congressional_district/UT-01",
            dataset_reference="ref",
            state_code="UT",
        ),
        selection=_regional_selection(),
        baked_folder=None,
        time_period=2028,
    )

    assert loaded.load_calls[0]["data_folder"] is None
    assert loaded.lookups[0]["year"] == 2028
    assert loaded.context["dataset_source"] == "on_demand"


def test_runtime_loads_a_national_run_from_the_default_folder(monkeypatch):
    bundle = get_country_release_bundle("us")
    loaded = _load_through_runtime(
        monkeypatch,
        region_resolution=RegionResolution(code="us"),
        selection=DatasetSelection(
            name=bundle.default_dataset,
            uri=bundle.default_dataset_uri,
            is_default=True,
        ),
        baked_folder=None,
    )

    assert loaded.load_calls[0]["data_folder"] is None
    assert "dataset_source" not in loaded.context


def _builder_data_version(monkeypatch, *, region_resolution, dataset_selection):
    """Run the annual runtime and return the data version it gives the builder."""
    from policyengine_simulation_executor import state_datasets

    selection = installed_spm_selection()
    simulations = [spm_capable_simulation(selection), spm_capable_simulation(selection)]
    builder_calls = []

    class FakeSimulationOutputBuilder:
        def __init__(self, **kwargs):
            builder_calls.append(kwargs)

        def serialize(self):
            return dict(CURRENT_SINGLE_YEAR_MACRO_RESULT)

    monkeypatch.setattr(
        "policyengine_simulation_executor.simulation_runtime._country_module",
        lambda _country: SimpleNamespace(model=SimpleNamespace(version="2.2.1")),
    )
    monkeypatch.setattr(
        "policyengine_simulation_executor.simulation_runtime._resolve_region",
        lambda **_kwargs: region_resolution,
    )
    monkeypatch.setattr(
        "policyengine_simulation_executor.simulation_runtime._resolve_dataset_selection",
        lambda _params, *, region_resolution: dataset_selection,
    )
    monkeypatch.setattr(
        state_datasets, "baked_state_data_folder", lambda **_kwargs: None
    )
    monkeypatch.setattr(
        "policyengine_simulation_executor.simulation_runtime._load_dataset",
        lambda _params, country_module, selection, data_folder=None: object(),
    )
    build_iter = iter(simulations)
    monkeypatch.setattr(
        "policyengine_simulation_executor.simulation_runtime._build_simulation",
        lambda *_args, **_kwargs: next(build_iter),
    )
    monkeypatch.setattr(
        "policyengine_simulation_executor.simulation_runtime.SimulationOutputBuilder",
        FakeSimulationOutputBuilder,
    )

    _run_simulation_impl_core(
        {
            "country": "us",
            "region": region_resolution.code,
            "baseline": {},
            "reform": {},
        },
        runtime=_TrackingRuntime(),
    )
    return builder_calls[0]["resolved_data_version"]


def test_a_state_run_reports_the_revision_of_the_dataset_it_loads(monkeypatch):
    from policyengine.provenance.manifest import get_release_manifest

    bundle = get_country_release_bundle("us")
    regional = "populace_us_2024_acs_local"

    data_version = _builder_data_version(
        monkeypatch,
        region_resolution=RegionResolution(
            code="state/ut", dataset_reference="ref", state_code="UT"
        ),
        dataset_selection=DatasetSelection(
            name=regional, uri=bundle.dataset_uris[regional], is_default=False
        ),
    )

    assert data_version == get_release_manifest("us").datasets[regional].revision
    assert data_version != bundle.data_version


def test_a_national_run_keeps_the_release_data_version(monkeypatch):
    bundle = get_country_release_bundle("us")

    data_version = _builder_data_version(
        monkeypatch,
        region_resolution=RegionResolution(code="us"),
        dataset_selection=DatasetSelection(
            name=bundle.default_dataset,
            uri=bundle.default_dataset_uri,
            is_default=True,
        ),
    )

    assert data_version is None


def test_run_simulation_impl_core_passes_detected_uk_boundary_to_builder(monkeypatch):
    # A bundle that does not route area regions: the national file carries
    # the area codes and the run builds the area breakdowns from them.
    _routes_uk_area_regions(monkeypatch, routed=False)
    dataset = SimpleNamespace(
        data=SimpleNamespace(
            entity_data={
                "household": weighted_entity_frame(
                    {"la_code_oa": ["E06000001", "E07000026"]}
                )
            }
        )
    )
    country_module = SimpleNamespace(model=SimpleNamespace(version="2.90.0"))
    builder_calls: list[dict] = []

    class FakeSimulationOutputBuilder:
        def __init__(self, **kwargs):
            builder_calls.append(kwargs)

        def serialize(self):
            return dict(CURRENT_SINGLE_YEAR_MACRO_RESULT)

    monkeypatch.setattr(
        "policyengine_simulation_executor.simulation_runtime._country_module",
        lambda _country: country_module,
    )
    monkeypatch.setattr(
        "policyengine_simulation_executor.simulation_runtime._resolve_region",
        lambda **_kwargs: RegionResolution(code="uk", dataset_reference=None),
    )
    monkeypatch.setattr(
        "policyengine_simulation_executor.simulation_runtime._load_dataset",
        lambda _params, country_module, selection, data_folder=None: dataset,
    )
    monkeypatch.setattr(
        "policyengine_simulation_executor.simulation_runtime._build_simulation",
        lambda *_args, **_kwargs: SimpleNamespace(
            on_first_ensure=lambda _callback: None
        ),
    )
    monkeypatch.setattr(
        "policyengine_simulation_executor.simulation_runtime.SimulationOutputBuilder",
        FakeSimulationOutputBuilder,
    )

    _run_simulation_impl_core(
        {"country": "uk", "baseline": {}, "reform": {}},
        runtime=_TrackingRuntime(),
    )

    metadata = builder_calls[0]["uk_local_authority_metadata"]
    assert metadata == UKLocalAuthorityMetadata(
        boundary_version=UKLocalAuthorityBoundaryVersion.LAD22
    )
    assert builder_calls[0]["omit_uk_area_breakdowns"] is False


def _routes_uk_area_regions(monkeypatch, *, routed: bool) -> None:
    monkeypatch.setattr(
        "policyengine_simulation_executor.uk_local_authority_metadata."
        "uk_area_regions_routed",
        lambda: routed,
    )


def _uk_dataset(**household: list[object]) -> SimpleNamespace:
    return SimpleNamespace(
        data=SimpleNamespace(
            entity_data={"household": weighted_entity_frame(household)}
        )
    )


_NATIONAL_SELECTION = DatasetSelection(
    "microcosm_uk_2024_25", "hf://policyengine/uk/national.h5@cut", is_default=True
)
_LOCAL_AREA_SELECTION = DatasetSelection(
    "microcosm_uk_2024_25_dense", "hf://policyengine/uk/local.h5@cut", is_default=False
)


_LOCAL_AREA_REVISION = "microcosm-uk-2024-25-dense-20261019T000432Z-ee5c2dbe"
_WALES = RowFilterStrategy(variable_name="region", variable_value="WALES")
_TEST_POLICIES = {
    "baseline": {"gov.test.parameter": {"2026-01-01.2100-12-31": 1}},
    "reform": {"gov.test.parameter": {"2026-01-01.2100-12-31": 2}},
}
_WALES_NATIONAL_PARAMS = {"country": "uk", "region": "country/wales", **_TEST_POLICIES}
_WALES_BREAKDOWN_PARAMS = {
    "country": "uk",
    "scope": "macro",
    "region": "country/wales",
    "time_period": "2026",
    # The Public API always sends include_cliffs: false.
    "include_cliffs": False,
    "breakdown": "local_areas",
    **_TEST_POLICIES,
}


def _routed_area_dataset(*, without: str | None = None) -> SimpleNamespace:
    """Return a local-area dataset with every column its area outputs read.

    ``without`` names one column to leave out.
    """
    household: dict[str, list[object]] = {
        "region": ["WALES", "WALES"],
        "constituency_code_oa": ["E14001063", "W07000112"],
        "la_code_oa": ["E06000063", "W06000001"],
        "region_code_oa": ["WALES", "WALES"],
        "source_household_id": [1, 2],
    }
    household.pop(without or "", None)
    return _uk_dataset(**household)


@dataclass
class _UKAreaRun:
    """A UK run through ``_run_simulation_impl_core`` with every seam faked."""

    builds: list[dict]
    builder_calls: list[dict]
    runtime: _TrackingRuntime
    # Each selection _load_dataset was asked for, and each country module load.
    loads: list[DatasetSelection]
    country_modules: list[str]
    # The builder methods the run called to produce its output, and the output.
    serialized: list[str]
    output: object = None

    def dataset_load_attributes(self) -> list[dict]:
        return [
            node["attrs"]
            for node in self.runtime.roots
            if node["name"] == ANNUAL_IMPACT_STAGES.name(Stage.DATASET_LOAD)
        ]


class _RoutedUKSeams:
    """Fakes for each seam of a routed UK run, recording into one ``_UKAreaRun``."""

    def __init__(self, run, *, local_area_dataset):
        self.run = run
        self.local_area_dataset = local_area_dataset
        self.national_dataset = _uk_dataset(region=["WALES", "WALES"])

    def country_module(self, country):
        self.run.country_modules.append(country)
        return SimpleNamespace(model=SimpleNamespace(version="2.132.6"))

    def resolve_region(self, *, params, **_kwargs):
        region = params.get("region")
        if region == "country/wales":
            return RegionResolution(
                code="country/wales", dataset_reference=None, scoping_strategy=_WALES
            )
        region_type = str(region).partition("/")[0]
        if region_type in {"constituency", "local_authority"}:
            # An area region names the routed local-area dataset itself.
            return RegionResolution(
                code=region,
                dataset_reference=_LOCAL_AREA_SELECTION.name,
                scoping_strategy=RowFilterStrategy(
                    variable_name=f"{region_type}_code_oa",
                    variable_value=region.partition("/")[2],
                ),
            )
        return RegionResolution(code="uk", dataset_reference=None)

    def resolve_selection(self, _params, *, region_resolution):
        if region_resolution.dataset_reference == _LOCAL_AREA_SELECTION.name:
            return _LOCAL_AREA_SELECTION
        return _NATIONAL_SELECTION

    def load_dataset(self, _params, *, selection, country_module, data_folder=None):
        self.run.loads.append(selection)
        if selection is _LOCAL_AREA_SELECTION:
            return self.local_area_dataset
        return self.national_dataset

    def build_simulation(self, _params, *, dataset, dataset_selection, policy, region):
        simulation = SimpleNamespace(on_first_ensure=lambda _callback: None)
        self.run.builds.append(
            {
                "dataset": dataset,
                "selection": dataset_selection,
                "policy": policy,
                "scoping_strategy": region.scoping_strategy,
                "simulation": simulation,
            }
        )
        return simulation

    def output_builder(self, **kwargs):
        self.run.builder_calls.append(kwargs)
        return _RecordingOutputBuilder(self.run)


class _RecordingOutputBuilder:
    def __init__(self, run):
        self.run = run

    def serialize(self):
        self.run.serialized.append("serialize")
        return dict(CURRENT_SINGLE_YEAR_MACRO_RESULT)

    def serialize_local_area_breakdown(self):
        self.run.serialized.append("serialize_local_area_breakdown")
        return {"kind": "local_areas"}


def _install_routed_uk_fakes(
    monkeypatch, *, local_area_dataset, routed: bool = True
) -> _UKAreaRun:
    """Fake every seam of a UK run whose national file carries no area codes."""
    run = _UKAreaRun(
        builds=[],
        builder_calls=[],
        runtime=_TrackingRuntime(),
        loads=[],
        country_modules=[],
        serialized=[],
    )
    seams = _RoutedUKSeams(run, local_area_dataset=local_area_dataset)
    _routes_uk_area_regions(monkeypatch, routed=routed)
    monkeypatch.setattr(
        "policyengine_simulation_executor.uk_local_authority_metadata."
        "routed_uk_area_dataset_name",
        lambda: _LOCAL_AREA_SELECTION.name,
    )
    for name, value in {
        "_country_module": seams.country_module,
        "_resolve_region": seams.resolve_region,
        "_resolve_dataset_selection": seams.resolve_selection,
        "_load_dataset": seams.load_dataset,
        "_build_simulation": seams.build_simulation,
        "SimulationOutputBuilder": seams.output_builder,
    }.items():
        monkeypatch.setattr(
            f"policyengine_simulation_executor.simulation_runtime.{name}", value
        )
    return run


def _run_routed_uk(monkeypatch, *, params, local_area_dataset) -> _UKAreaRun:
    run = _install_routed_uk_fakes(monkeypatch, local_area_dataset=local_area_dataset)
    run.output = _run_simulation_impl_core(dict(params), runtime=run.runtime)
    return run


def _span_attributes(nodes) -> list[dict]:
    """Return the attributes of every span in a tracked span tree."""
    attributes = []
    for node in nodes:
        attributes.append(node["attrs"])
        attributes.extend(_span_attributes(node["children"]))
    return attributes


@pytest.mark.parametrize("region", ["uk", "country/wales"])
def test_a_routed_uk_wide_or_nation_run_builds_no_area_pair(monkeypatch, region):
    from policyengine_simulation_executor import uk_local_authority_metadata

    run = _run_routed_uk(
        monkeypatch,
        params={**_WALES_NATIONAL_PARAMS, "region": region},
        local_area_dataset=_routed_area_dataset(),
    )

    # One dataset, the national file, and one baseline and reform on it.
    assert run.loads == [_NATIONAL_SELECTION]
    assert [build["selection"] for build in run.builds] == [_NATIONAL_SELECTION] * 2
    (builder_kwargs,) = run.builder_calls
    assert builder_kwargs["baseline"] is run.builds[0]["simulation"]
    assert builder_kwargs["reform"] is run.builds[1]["simulation"]
    assert builder_kwargs["omit_uk_area_breakdowns"] is True
    assert builder_kwargs["omit_uk_area_breakdowns"] is (
        uk_local_authority_metadata.uk_area_breakdowns_omitted("uk", region)
    )
    assert "uk_area_simulations" not in builder_kwargs
    assert run.serialized == ["serialize"]
    assert {"dataset_role": "uk_area"} not in _span_attributes(run.runtime.roots)


@pytest.mark.parametrize(
    "region", ["constituency/E14001063", "local_authority/E06000063"]
)
@pytest.mark.parametrize(
    "missing",
    ["constituency_code_oa", "la_code_oa", "region_code_oa", "source_household_id"],
)
def test_a_routed_area_run_needs_every_column_its_outputs_read_before_simulating(
    monkeypatch, region, missing
):
    run = _install_routed_uk_fakes(
        monkeypatch, local_area_dataset=_routed_area_dataset(without=missing)
    )

    with pytest.raises(
        ValueError,
        match=rf"'microcosm_uk_2024_25_dense', which the bundle routes .* has no "
        rf"{missing} column",
    ):
        _run_simulation_impl_core(
            {**_WALES_NATIONAL_PARAMS, "region": region}, runtime=run.runtime
        )

    assert run.loads == [_LOCAL_AREA_SELECTION]
    assert run.builds == []


def test_a_routed_area_run_with_every_column_detects_its_boundaries(monkeypatch):
    run = _run_routed_uk(
        monkeypatch,
        params={**_WALES_NATIONAL_PARAMS, "region": "local_authority/E06000063"},
        local_area_dataset=_routed_area_dataset(),
    )

    (builder_kwargs,) = run.builder_calls
    assert builder_kwargs["uk_local_authority_metadata"] == UKLocalAuthorityMetadata(
        boundary_version=UKLocalAuthorityBoundaryVersion.LAD23
    )
    assert builder_kwargs["omit_uk_area_breakdowns"] is False


def test_a_local_area_breakdown_runs_on_the_local_area_dataset_alone(monkeypatch):
    local_area_dataset = _routed_area_dataset()
    monkeypatch.setattr(
        "policyengine_simulation_executor.simulation_runtime."
        "_manifest_dataset_revision",
        lambda country, name: {
            ("uk", _LOCAL_AREA_SELECTION.name): _LOCAL_AREA_REVISION
        }[(country, name)],
    )

    run = _run_routed_uk(
        monkeypatch,
        params=_WALES_BREAKDOWN_PARAMS,
        local_area_dataset=local_area_dataset,
    )

    # The national file is never loaded: one load, one baseline and reform.
    assert run.loads == [_LOCAL_AREA_SELECTION]
    assert [build["selection"] for build in run.builds] == [_LOCAL_AREA_SELECTION] * 2
    assert all(build["dataset"] is local_area_dataset for build in run.builds)
    assert all(build["scoping_strategy"] is _WALES for build in run.builds)
    assert [build["policy"] for build in run.builds] == [
        _normalise_policy(_TEST_POLICIES["baseline"]),
        _normalise_policy(_TEST_POLICIES["reform"]),
    ]
    (builder_kwargs,) = run.builder_calls
    assert builder_kwargs["baseline"] is run.builds[0]["simulation"]
    assert builder_kwargs["reform"] is run.builds[1]["simulation"]
    assert builder_kwargs["dataset"] is local_area_dataset
    assert builder_kwargs["uk_local_authority_metadata"] == UKLocalAuthorityMetadata(
        boundary_version=UKLocalAuthorityBoundaryVersion.LAD23
    )
    assert builder_kwargs["resolved_dataset_name"] == _LOCAL_AREA_SELECTION.name
    assert builder_kwargs["resolved_data_version"] == _LOCAL_AREA_REVISION
    assert builder_kwargs["resolved_region_code"] == "country/wales"
    assert builder_kwargs.get("uk_area_simulations") is None
    assert run.serialized == ["serialize_local_area_breakdown"]
    assert run.output == {"kind": "local_areas"}
    assert run.dataset_load_attributes() == [{"dataset_role": "uk_area"}]


def test_a_dataset_revision_is_read_from_the_release_manifest(monkeypatch):
    from policyengine_simulation_executor import simulation_runtime

    manifests = {
        "uk": SimpleNamespace(
            datasets={
                _LOCAL_AREA_SELECTION.name: SimpleNamespace(
                    revision=_LOCAL_AREA_REVISION
                ),
                "unrevised": SimpleNamespace(revision=""),
            }
        )
    }
    monkeypatch.setattr(
        "policyengine.provenance.manifest.get_release_manifest", manifests.__getitem__
    )

    revision = simulation_runtime._manifest_dataset_revision
    assert revision("uk", _LOCAL_AREA_SELECTION.name) == _LOCAL_AREA_REVISION
    assert revision("uk", "unrevised") is None
    assert revision("uk", "absent") is None


def test_a_uk_wide_local_area_breakdown_simulates_the_whole_dataset(monkeypatch):
    run = _run_routed_uk(
        monkeypatch,
        params={**_WALES_BREAKDOWN_PARAMS, "region": "uk"},
        local_area_dataset=_routed_area_dataset(),
    )

    assert run.loads == [_LOCAL_AREA_SELECTION]
    assert [build["scoping_strategy"] for build in run.builds] == [None, None]
    assert run.builder_calls[0]["resolved_region_code"] == "uk"


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"country": "us"}, "only for UK requests"),
        ({"region": "constituency/E14001063"}, "'constituency/E14001063'"),
        ({"region": "local_authority/E06000063"}, "'local_authority/E06000063'"),
        ({"include_cliffs": True}, "include_cliffs=true"),
        ({"region": None, "region_group": ["country/wales"]}, "region_group"),
        ({"breakdown": "districts"}, "'districts'"),
    ],
)
def test_an_invalid_local_area_breakdown_is_refused_before_any_load(
    monkeypatch, change, message
):
    run = _install_routed_uk_fakes(
        monkeypatch, local_area_dataset=_routed_area_dataset()
    )

    with pytest.raises(ValueError, match=message):
        _run_simulation_impl_core(
            {**_WALES_BREAKDOWN_PARAMS, **change}, runtime=run.runtime
        )

    assert run.country_modules == []
    assert run.loads == []
    assert run.builds == []


def test_a_local_area_breakdown_needs_a_bundle_that_routes_area_regions(monkeypatch):
    run = _install_routed_uk_fakes(
        monkeypatch, local_area_dataset=_routed_area_dataset(), routed=False
    )

    with pytest.raises(
        ValueError,
        match="does not route UK constituency and local-authority regions",
    ):
        _run_simulation_impl_core(dict(_WALES_BREAKDOWN_PARAMS), runtime=run.runtime)

    assert run.loads == []
    assert run.builds == []


@pytest.mark.parametrize(
    "missing",
    ["constituency_code_oa", "la_code_oa", "region_code_oa", "source_household_id"],
)
def test_a_local_area_breakdown_needs_every_column_its_outputs_read(
    monkeypatch, missing
):
    local_area_dataset = _routed_area_dataset(without=missing)

    with pytest.raises(
        ValueError,
        match=rf"'microcosm_uk_2024_25_dense', which the bundle routes .* has no "
        rf"{missing} column",
    ):
        _run_routed_uk(
            monkeypatch,
            params=_WALES_BREAKDOWN_PARAMS,
            local_area_dataset=local_area_dataset,
        )


_LOCAL_AREA_URI = (
    "hf://policyengine/populace-uk-private/microcosm_uk_2024_25_dense.h5"
    "@microcosm-uk-2024-25-dense-20261019T000432Z-ee5c2dbe"
)


def _registry_area(region_type: str, code: str, label: str) -> SimpleNamespace:
    return SimpleNamespace(
        code=f"{region_type}/{code}",
        label=label,
        region_type=region_type,
        parent_code="uk",
        dataset_path=_LOCAL_AREA_URI,
        scoping_strategy=object(),
    )


def _routed_uk_country_module() -> SimpleNamespace:
    """Return a UK model whose registry lists area regions, as routing gives."""
    areas = [
        _registry_area("constituency", "E14001063", "Aldershot"),
        _registry_area("constituency", "W07000112", "Ynys Môn"),
        _registry_area("local_authority", "E07000110", "Maidstone"),
        _registry_area("local_authority", "N09000011", "Ards and North Down"),
    ]
    by_code = {area.code: area for area in areas}
    registry = SimpleNamespace(
        get_by_type=lambda region_type: [
            area for area in areas if area.region_type == region_type
        ]
    )
    return SimpleNamespace(
        model=SimpleNamespace(get_region=by_code.get, region_registry=registry)
    )


@pytest.mark.parametrize(
    ("requested", "expected"),
    [
        ("constituency/E14001063", "constituency/E14001063"),
        ("constituency/e14001063", "constituency/E14001063"),
        ("constituency/Aldershot", "constituency/E14001063"),
        ("constituency/ynys môn", "constituency/W07000112"),
        ("local_authority/Maidstone", "local_authority/E07000110"),
        ("local_authority/n09000011", "local_authority/N09000011"),
        ("local_authority/Ards and North Down", "local_authority/N09000011"),
    ],
)
def test_resolve_region_maps_routed_uk_area_names_and_codes_to_the_registry(
    monkeypatch, requested, expected
):
    _routes_uk_area_regions(monkeypatch, routed=True)
    country_module = _routed_uk_country_module()

    resolution = _resolve_region(
        country_module=country_module, country="uk", params={"region": requested}
    )

    region = country_module.model.get_region(expected)
    assert resolution.code == expected
    assert resolution.scoping_strategy is region.scoping_strategy
    assert resolution.dataset_reference == _LOCAL_AREA_URI


@pytest.mark.parametrize(
    "requested",
    [
        # A district that April 2023 replaced: LAD22 only.
        "local_authority/E07000026",
        "local_authority/Carlisle",
        "constituency/Maidstone",
    ],
)
def test_resolve_region_rejects_uk_areas_outside_the_routed_registry(
    monkeypatch, requested
):
    _routes_uk_area_regions(monkeypatch, routed=True)

    with pytest.raises(ValueError, match="Unsupported UK region"):
        _resolve_region(
            country_module=_routed_uk_country_module(),
            country="uk",
            params={"region": requested},
        )


@pytest.mark.parametrize(
    "requested", ["constituency/Aldershot", "local_authority/E07000110"]
)
def test_resolve_region_rejects_uk_area_regions_on_an_unrouted_bundle(
    monkeypatch, requested
):
    # Without routing, policyengine.py's registry lists no area regions, and
    # no weight-matrix region stands in for one: older bundles keep their own
    # frozen apps.
    _routes_uk_area_regions(monkeypatch, routed=False)
    country_module = SimpleNamespace(model=SimpleNamespace(get_region=lambda _: None))

    with pytest.raises(ValueError, match=f"Unsupported UK region: {requested}"):
        _resolve_region(
            country_module=country_module, country="uk", params={"region": requested}
        )


def test_resolve_dataset_reference_uses_bundle_default():
    bundle = get_country_release_bundle("us")

    assert _resolve_dataset_reference("us") == bundle.default_dataset


def test_dataset_selection_identifies_the_actual_bundle_default():
    bundle = get_country_release_bundle("us")
    nondefault = next(
        name for name in bundle.dataset_uris if name != bundle.default_dataset
    )

    omitted = _resolve_dataset_selection({"country": "us"})
    alternate = _resolve_dataset_selection(
        {"country": "us"},
        region_resolution=RegionResolution(
            code="state/ca", dataset_reference=bundle.dataset_uris[nondefault]
        ),
    )

    assert isinstance(omitted, DatasetSelection)
    assert omitted.name == bundle.default_dataset
    assert omitted.is_default
    assert alternate.name == nondefault
    assert not alternate.is_default


def test_dataset_selection_rejects_a_bundled_uri_as_public_data():
    bundle = get_country_release_bundle("us")
    with pytest.raises(ValueError, match="Dataset overrides are not supported"):
        _resolve_dataset_selection(
            {"country": "us", "data": bundle.default_dataset_uri}
        )


@pytest.mark.parametrize("country", ["us", "uk"])
def test_load_dataset_delegates_default_selection_to_country_loader_with_receipt(
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
            "years": [2026],
            "data_folder": str(tmp_path),
        }
    ]


@pytest.mark.parametrize(
    "explicit_data_params",
    [
        {"data_version": "custom-v1"},
        {"data": "populace_us_2024@custom-v1"},
        {"data": "hf://other-org/other-repo/populace_us_2024.h5"},
    ],
)
def test_load_dataset_rejects_unmanaged_dataset_requests(
    explicit_data_params,
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("POLICYENGINE_DATA_FOLDER", str(tmp_path))

    ensure_calls = []

    def ensure_datasets(**kwargs):
        ensure_calls.append(kwargs)
        return {"dataset": SimpleNamespace()}

    with pytest.raises(ValueError, match="Dataset overrides are not supported"):
        _load_dataset(
            {"country": "us", "time_period": "2026", **explicit_data_params},
            country_module=SimpleNamespace(ensure_datasets=ensure_datasets),
        )

    assert ensure_calls == []


def test_load_dataset_rejects_explicit_default(tmp_path, monkeypatch):
    monkeypatch.setenv("POLICYENGINE_DATA_FOLDER", str(tmp_path))
    ensure_calls = []

    def ensure_datasets(**kwargs):
        ensure_calls.append(kwargs)
        return {"dataset": SimpleNamespace()}

    with pytest.raises(ValueError, match="Dataset overrides are not supported"):
        _load_dataset(
            {"country": "us", "time_period": "2026", "data": "populace_us_2024"},
            country_module=SimpleNamespace(ensure_datasets=ensure_datasets),
        )

    assert ensure_calls == []


def test_load_dataset_passes_region_dataset_name_to_managed_loader(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("POLICYENGINE_DATA_FOLDER", str(tmp_path))
    bundle = get_country_release_bundle("us")
    nondefault = next(
        name for name in bundle.dataset_uris if name != bundle.default_dataset
    )
    ensure_calls = []

    def ensure_datasets(**kwargs):
        ensure_calls.append(kwargs)
        return {"dataset": SimpleNamespace()}

    _load_dataset(
        {"country": "us", "time_period": "2026"},
        country_module=SimpleNamespace(ensure_datasets=ensure_datasets),
        region_resolution=RegionResolution(
            code="state/ca", dataset_reference=bundle.dataset_uris[nondefault]
        ),
    )

    assert ensure_calls == [
        {
            "datasets": [nondefault],
            "years": [2026],
            "data_folder": _nondefault_data_folder(
                "us", nondefault, bundle.dataset_uris[nondefault]
            ),
        }
    ]


@pytest.mark.parametrize("use_default", [True, False])
def test_load_dataset_maps_region_bundle_uri_to_managed_name(
    use_default, tmp_path, monkeypatch
):
    monkeypatch.setenv("POLICYENGINE_DATA_FOLDER", str(tmp_path))
    bundle = get_country_release_bundle("us")
    name = (
        bundle.default_dataset
        if use_default
        else next(
            name for name in bundle.dataset_uris if name != bundle.default_dataset
        )
    )
    ensure_calls = []

    def ensure_datasets(**kwargs):
        ensure_calls.append(kwargs)
        return {"dataset": SimpleNamespace()}

    _load_dataset(
        {"country": "us", "time_period": "2026"},
        country_module=SimpleNamespace(ensure_datasets=ensure_datasets),
        region_resolution=SimpleNamespace(dataset_reference=bundle.dataset_uris[name]),
    )

    assert ensure_calls == [
        {
            **({} if use_default else {"datasets": [name]}),
            "years": [2026],
            "data_folder": (
                str(tmp_path)
                if use_default
                else _nondefault_data_folder("us", name, bundle.dataset_uris[name])
            ),
        }
    ]


def test_load_dataset_rejects_explicit_dataset_for_filtered_region(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("POLICYENGINE_DATA_FOLDER", str(tmp_path))
    bundle = get_country_release_bundle("us")
    nondefault = next(
        name for name in bundle.dataset_uris if name != bundle.default_dataset
    )
    ensure_calls = []

    def ensure_datasets(**kwargs):
        ensure_calls.append(kwargs)
        return {"dataset": SimpleNamespace()}

    with pytest.raises(ValueError, match="Dataset overrides are not supported"):
        _load_dataset(
            {"country": "us", "time_period": "2026", "data": nondefault},
            country_module=SimpleNamespace(ensure_datasets=ensure_datasets),
            region_resolution=SimpleNamespace(
                dataset_reference=bundle.default_dataset_uri,
                scoping_strategy=object(),
            ),
        )

    assert ensure_calls == []


def test_nondefault_dataset_cache_is_distinct_for_shared_filename_stem():
    first = _nondefault_data_folder(
        "us", "certified_a", "hf://policyengine/a/shared.h5@revision-a"
    )
    second = _nondefault_data_folder(
        "us", "certified_b", "hf://policyengine/b/shared.h5@revision-b"
    )

    assert first != second


def test_load_dataset_reads_a_nondefault_dataset_from_the_folder_it_is_given(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("POLICYENGINE_DATA_FOLDER", str(tmp_path / "image-data"))
    bundle = get_country_release_bundle("us")
    nondefault = next(
        name for name in bundle.dataset_uris if name != bundle.default_dataset
    )
    selection = DatasetSelection(
        name=nondefault, uri=bundle.dataset_uris[nondefault], is_default=False
    )
    state_folder = str(tmp_path / "state-datasets" / nondefault / "UT")
    ensure_calls = []

    def ensure_datasets(**kwargs):
        ensure_calls.append(kwargs)
        return {"dataset": SimpleNamespace()}

    _load_selected_dataset(
        {"country": "us", "time_period": "2026"},
        selection=selection,
        country_module=SimpleNamespace(ensure_datasets=ensure_datasets),
        data_folder=state_folder,
    )

    assert ensure_calls == [
        {"datasets": [nondefault], "years": [2026], "data_folder": state_folder}
    ]


def test_load_dataset_without_a_folder_keeps_the_temporary_nondefault_folder(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("POLICYENGINE_DATA_FOLDER", str(tmp_path))
    bundle = get_country_release_bundle("us")
    nondefault = next(
        name for name in bundle.dataset_uris if name != bundle.default_dataset
    )
    selection = DatasetSelection(
        name=nondefault, uri=bundle.dataset_uris[nondefault], is_default=False
    )
    ensure_calls = []

    def ensure_datasets(**kwargs):
        ensure_calls.append(kwargs)
        return {"dataset": SimpleNamespace()}

    _load_selected_dataset(
        {"country": "us", "time_period": "2026"},
        selection=selection,
        country_module=SimpleNamespace(ensure_datasets=ensure_datasets),
        data_folder=None,
    )

    assert ensure_calls[0]["data_folder"] == _nondefault_data_folder(
        "us", nondefault, bundle.dataset_uris[nondefault]
    )


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


def test_resolve_region_scopes_us_state_from_national_dataset():
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
    country_module = SimpleNamespace(model=SimpleNamespace(get_region=regions.get))

    resolution = _resolve_region(
        country_module=country_module,
        country="us",
        params={"region": "state/UT"},
    )

    assert resolution.code == "state/ut"
    assert resolution.dataset_reference == resolve_runtime_bundle_dataset_uri(
        "us", None, prefer_local=False
    )
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
    country_module = SimpleNamespace(model=SimpleNamespace(get_region=regions.get))

    resolution = _resolve_region(
        country_module=country_module,
        country="us",
        params={"region": "congressional_district/ut-01"},
    )

    assert resolution.code == "congressional_district/UT-01"
    assert resolution.dataset_reference == resolve_runtime_bundle_dataset_uri(
        "us", None, prefer_local=False
    )
    assert resolution.scoping_strategy is scoping_strategy


def _us_country_module_with_regions(regions):
    return SimpleNamespace(model=SimpleNamespace(get_region=regions.get))


@pytest.mark.parametrize(
    ("requested_region", "state_code"),
    [
        ("state/ut", "UT"),
        ("state/UT", "UT"),
        ("ut", "UT"),
        ("congressional_district/ut-01", "UT"),
        ("congressional_district/WY-00", "WY"),
    ],
)
def test_resolve_region_reports_the_us_state_code(requested_region, state_code):
    utah = SimpleNamespace(
        dataset_path=None,
        scoping_strategy=object(),
        parent_code="us",
        state_code="UT",
    )
    regions = {
        "us": SimpleNamespace(
            dataset_path=get_country_release_bundle("us").default_dataset_uri,
            scoping_strategy=None,
            parent_code=None,
            state_code=None,
        ),
        "state/ut": utah,
        "congressional_district/UT-01": SimpleNamespace(
            dataset_path=None,
            scoping_strategy=object(),
            parent_code="state/ut",
            state_code="UT",
        ),
        "congressional_district/WY-00": SimpleNamespace(
            dataset_path=None,
            scoping_strategy=object(),
            parent_code="state/wy",
            state_code="WY",
        ),
        "state/wy": SimpleNamespace(
            dataset_path=None,
            scoping_strategy=object(),
            parent_code="us",
            state_code="WY",
        ),
    }

    resolution = _resolve_region(
        country_module=_us_country_module_with_regions(regions),
        country="us",
        params={"region": requested_region},
    )

    assert resolution.state_code == state_code


def test_resolve_region_reports_no_state_code_for_a_national_us_run():
    resolution = _resolve_region(
        country_module=_us_country_module_with_regions({}),
        country="us",
        params={"region": "us"},
    )

    assert resolution.state_code is None


def test_resolve_region_reports_no_state_code_for_a_uk_region():
    scotland = SimpleNamespace(
        dataset_path=None,
        scoping_strategy=object(),
        parent_code="uk",
        state_code=None,
    )
    national = SimpleNamespace(
        dataset_path=get_country_release_bundle("uk").default_dataset_uri,
        scoping_strategy=None,
        parent_code=None,
        state_code=None,
    )
    country_module = SimpleNamespace(
        model=SimpleNamespace(
            get_region={"country/scotland": scotland, "uk": national}.get
        )
    )

    resolution = _resolve_region(
        country_module=country_module,
        country="uk",
        params={"region": "country/scotland"},
    )

    assert resolution.state_code is None


def test_resolve_region_reports_no_state_code_for_a_region_group():
    from policyengine.core.scoping_strategy import RowFilterStrategy

    regions = {
        code: SimpleNamespace(
            scoping_strategy=RowFilterStrategy(
                variable_name="state_fips", variable_value=fips
            ),
            state_code=code.removeprefix("state/").upper(),
        )
        for code, fips in (("state/ut", 49), ("state/wy", 56))
    }

    resolution = _resolve_region(
        country_module=_us_country_module_with_regions(regions),
        country="us",
        params={"region": "us", "region_group": ["state/ut", "state/wy"]},
    )

    assert resolution.state_code is None


def test_every_installed_us_state_and_district_region_names_its_state():
    from policyengine.countries.us.regions import us_region_registry

    for region_type in ("state", "congressional_district"):
        regions = us_region_registry.get_by_type(region_type)
        assert regions
        for region in regions:
            assert region.state_code
            assert region.state_code == region.state_code.upper()


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
    country_module = SimpleNamespace(model=SimpleNamespace(get_region=regions.get))

    with pytest.raises(ValueError, match="US place regions are not yet supported"):
        _resolve_region(
            country_module=country_module,
            country="us",
            params={"region": "place/ut-67000"},
        )


def test_resolve_region_scopes_uk_country_from_national_dataset():
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
    country_module = SimpleNamespace(model=SimpleNamespace(get_region=regions.get))

    resolution = _resolve_region(
        country_module=country_module,
        country="uk",
        params={"region": "England"},
    )

    assert resolution.code == "country/england"
    assert resolution.dataset_reference == resolve_runtime_bundle_dataset_uri(
        "uk", None, prefer_local=False
    )
    assert resolution.scoping_strategy is scoping_strategy


def test_builder_data_version_prefers_resolved_revision_then_dataset_metadata():
    baseline, reform = _macro_baseline_reform()

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
    baseline = _FakeSimulation(
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
    reform = _FakeSimulation(
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

    us_budget = _simulation_output_builder(
        "us", baseline, reform
    )._build_budgetary_impact()
    uk_budget = _simulation_output_builder(
        "uk", baseline, reform
    )._build_budgetary_impact()

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
    baseline, reform = _macro_baseline_reform()

    def fail_change_output_variable(*_args, **_kwargs):
        msg = "household_tax missing"
        raise RuntimeError(msg)

    monkeypatch.setattr(
        "policyengine_simulation_executor.simulation_output_budget._change_output_variable",
        fail_change_output_variable,
    )

    with pytest.raises(RuntimeError, match="household_tax missing"):
        _simulation_output_builder("us", baseline, reform)._build_budgetary_impact()


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

    builder = _simulation_output_builder("uk", baseline, reform)
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
    builder = _simulation_output_builder("us", object(), object())

    assert builder._build_uk_constituency_impact() is None
    assert builder._build_uk_local_authority_impact() is None


def test_uk_local_authority_impact_receives_detected_boundary_metadata(monkeypatch):
    expected = [_local_authority_impact_record().model_dump(mode="json")]
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

    result = _simulation_output_builder(
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
    builder = _simulation_output_builder(
        "uk",
        object(),
        object(),
        BuilderOptions(resolved_region_code="uk", omit_uk_area_breakdowns=True),
    )

    assert builder._build_uk_constituency_impact() is None
    assert builder._build_uk_local_authority_impact() is None


class _BreakdownSimulation:
    """A local-area simulation that records when it is computed."""

    def __init__(self, label, events):
        self.label = label
        self.events = events

    def ensure(self):
        self.events.append(self.label)


class _RecordingProgress:
    def __init__(self, events):
        self.events = events

    def enter(self, stage):
        self.events.append(stage)


def _breakdown_builder(monkeypatch, *, events, runtime=None):
    """Return a UK breakdown builder whose area outputs are faked.

    The country module's impact analysis fails the test if it is called.
    Each fake records into ``events``.
    """
    disclosure = simulation_output_geographic.UKGeographyDisclosureOutput()

    def compute(country, baseline_simulation, reform_simulation, *, year):
        events.append(
            ("release", country, baseline_simulation, reform_simulation, year)
        )
        return disclosure

    def constituencies(country, value):
        events.append(("constituency", value))
        return GeographicImpactOutput.model_validate(
            [_constituency_impact_record().model_dump(mode="json")]
        )

    def local_authorities(country, value, **kwargs):
        events.append(("local_authority", value, kwargs["uk_local_authority_metadata"]))
        return GeographicImpactOutput.model_validate([])

    for name, function in {
        "compute_uk_geography_disclosure_output": compute,
        "build_uk_constituency_impact": constituencies,
        "build_uk_local_authority_impact": local_authorities,
    }.items():
        monkeypatch.setattr(
            f"policyengine_simulation_executor.simulation_output_geographic.{name}",
            function,
        )

    def no_impact_analysis(*_args, **_kwargs):
        pytest.fail("a local-area breakdown computes no national impact analysis")

    builder = SimulationOutputBuilder(
        country="uk",
        simulation_params={
            "country": "uk",
            "time_period": "2026",
            "breakdown": "local_areas",
        },
        country_module=SimpleNamespace(
            model=SimpleNamespace(version="2.132.6"),
            economic_impact_analysis=no_impact_analysis,
        ),
        dataset=SimpleNamespace(metadata={}),
        baseline=_BreakdownSimulation("baseline", events),
        reform=_BreakdownSimulation("reform", events),
        resolved_data_version=_LOCAL_AREA_REVISION,
        resolved_dataset_name="microcosm_uk_2024_25_dense",
        resolved_region_code="uk",
        runtime=runtime,
        uk_local_authority_metadata=UKLocalAuthorityMetadata(
            boundary_version=UKLocalAuthorityBoundaryVersion.LAD23
        ),
        progress=_RecordingProgress(events),
    )
    return builder, disclosure


def test_a_local_area_breakdown_publishes_both_levels_from_one_release(monkeypatch):
    events: list[object] = []
    builder, disclosure = _breakdown_builder(monkeypatch, events=events)

    output = builder.serialize_local_area_breakdown()

    releases = [event for event in events if event[0] == "release"]
    assert releases == [("release", "uk", builder.baseline, builder.reform, 2026)]
    assert [event for event in events if event[0] != "release"][-2:] == [
        ("constituency", disclosure),
        ("local_authority", disclosure, builder.uk_local_authority_metadata),
    ]
    assert output == {
        "kind": "local_areas",
        "model_version": "2.132.6",
        "policyengine_version": get_country_release_bundle("uk").policyengine_version,
        "dataset": "microcosm_uk_2024_25_dense",
        "data_version": _LOCAL_AREA_REVISION,
        "constituency_impact": [_constituency_impact_record().model_dump(mode="json")],
        "local_authority_impact": [],
    }


def test_a_local_area_breakdown_enters_outputs_after_both_simulations(monkeypatch):
    events: list[object] = []
    builder, _ = _breakdown_builder(monkeypatch, events=events)

    builder.serialize_local_area_breakdown()

    simulations_and_stages = [event for event in events if not isinstance(event, tuple)]
    assert simulations_and_stages == ["baseline", "reform", JobStage.OUTPUTS]
    assert events.index(JobStage.OUTPUTS) < next(
        index for index, event in enumerate(events) if isinstance(event, tuple)
    )


def test_a_local_area_breakdown_opens_only_registered_spans(monkeypatch):
    events: list[object] = []
    runtime = _TrackingRuntime()
    builder, _ = _breakdown_builder(monkeypatch, events=events, runtime=runtime)

    builder.serialize_local_area_breakdown()

    registered = {stage.value for stage in ANNUAL_IMPACT_STAGES.stages}
    assert set(runtime.names) <= registered
    assert ANNUAL_IMPACT_STAGES.name(Stage.ECONOMIC_IMPACT_ANALYSIS) not in (
        runtime.names
    )
    (calculation,) = runtime.roots[:1]
    assert calculation["name"] == ANNUAL_IMPACT_STAGES.name(Stage.CALCULATION)
    (output_build,) = calculation["children"]
    assert output_build["name"] == ANNUAL_IMPACT_STAGES.name(
        Stage.SIMULATION_OUTPUT_BUILD
    )
    assert {
        ANNUAL_IMPACT_STAGES.name(Stage.OUTPUT_UK_CONSTITUENCY),
        ANNUAL_IMPACT_STAGES.name(Stage.OUTPUT_UK_LOCAL_AUTHORITY),
    } <= {child["name"] for child in output_build["children"]}
