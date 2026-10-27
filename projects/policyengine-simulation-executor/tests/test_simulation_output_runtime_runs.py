"""Tests for running a simulation through the runtime, with its calculation faked."""

from types import SimpleNamespace

from fixtures.macro_output import CURRENT_SINGLE_YEAR_MACRO_RESULT
from fixtures.spm_doubles import installed_spm_selection, spm_capable_simulation
from policyengine_simulation_executor.job_progress import NULL_JOB_PROGRESS
from policyengine_simulation_executor.simulation_runtime import (
    RegionResolution,
    _run_simulation_impl_core,
    run_simulation_impl,
)
from policyengine_simulation_observability.stages import Stage
from test_simulation_output_fakes import TrackingRuntime, with_observability_timings


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
        "policyengine_simulation_executor.simulation_runtime.resolve_region",
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

    result, timings, counts, segment_tree = with_observability_timings(
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
        "policyengine_simulation_executor.simulation_runtime.resolve_region",
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
    runtime = TrackingRuntime()
    run_simulation_impl(params, runtime=runtime)
    assert ("baseline_artifact", "incomplete") in runtime.context

    # A plain Simulation baseline (no artifact_outcome) must emit nothing.
    runtime.context.clear()
    build_count[0] = 0
    simulations["baseline"] = spm_capable_simulation(selection)
    run_simulation_impl(params, runtime=runtime)
    assert all(key != "baseline_artifact" for key, _ in runtime.context)


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
        "policyengine_simulation_executor.simulation_runtime.resolve_region",
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

    runtime = TrackingRuntime()
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
        "policyengine_simulation_executor.simulation_runtime.resolve_region",
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
        runtime=TrackingRuntime(),
    )

    _assert_macro_result(result, selection)
    assert build_calls[0][3] is scoping_strategy
    assert build_calls[1][3] is scoping_strategy
    assert len(load_selections) == 1
    assert build_calls[0][4] is load_selections[0]
    assert build_calls[1][4] is load_selections[0]
