"""Bounded canonical worker contracts; no population simulation or Modal calls."""

import json
import pickle
import sys
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from policyengine_simulation_contract.gateway_models import (
    SimulationRequest,
    BudgetWindowBatchRequest,
)
from policyengine_simulation_contract.spm import (
    SPMCapability,
    SPMSelection,
    SPMInputError,
    resolve_spm_selection,
    combine_spm_results,
)
from policyengine_simulation_executor import artifact_keys, simulation_runtime
from policyengine_simulation_executor.simulation import create_router
from src.modal.budget_window_results import (
    extract_annual_impact,
    build_budget_window_result,
)
from src.modal.fanout import build_child_payload

SELECTION = SPMSelection(
    forecast_content_sha256="a" * 64,
    scenario="ce_trend",
    geography_kind="national",
    geography_id=None,
    county_vintage="2020",
    as_of=None,
).model_dump()
CAPABILITY = SPMCapability(defaults=SELECTION).model_dump()


def result(selection=SELECTION, year="2026"):
    receipt = dict(
        forecast_id="test-only",
        forecast_sha256=selection["forecast_content_sha256"],
        scenario=selection["scenario"],
        geography_kind=selection["geography_kind"],
        runtime_versions={"policyengine-us": "test-only"},
        years={year: {"status": "forecast"}},
        geographies=[],
        composition_method="classified-inputs",
        storage_method="formula",
    )
    return {
        "spm_config": deepcopy(selection),
        "spm_provenance": {"baseline": [receipt], "reform": [deepcopy(receipt)]},
        "budget": {
            "tax_revenue_impact": 1,
            "benefit_spending_impact": 2,
            "budgetary_impact": -1,
        },
    }


@pytest.mark.parametrize(
    "model,extra",
    [
        (SimulationRequest, {}),
        (
            BudgetWindowBatchRequest,
            {"region": "us", "start_year": "2026", "window_size": 2},
        ),
    ],
)
def test_public_request_roundtrip(model, extra):
    parsed = model(country="us", spm={"geography_kind": "national"}, **extra)
    assert json.loads(parsed.model_dump_json())["spm"]["geography_kind"] == "national"
    with pytest.raises(ValueError):
        model(country="us", spm={"provider_path": "x"}, **extra)
    with pytest.raises(ValueError):
        model(
            country="us",
            spm={"geography_kind": "national", "geography_id": "12345"},
            **extra,
        )


def test_unknown_bundle_fails_closed_and_historical_uk_remain():
    for country, version, model in [
        ("us", "5.2.0", "1.764.6"),
        ("us", "5.3.0", "1.764.6"),
        ("uk", "future-test", "test"),
    ]:
        assert (
            resolve_spm_selection(
                country,
                None,
                capability=None,
                policyengine_version=version,
                model_version=model,
            )
            is None
        )
    for selection in (None, {}, {"geography_kind": "national"}):
        with pytest.raises(SPMInputError, match="no certified"):
            resolve_spm_selection(
                "us",
                selection,
                capability=None,
                policyengine_version="future-test",
                model_version="test",
            )
    assert (
        resolve_spm_selection(
            "us",
            {},
            capability=CAPABILITY,
            policyengine_version="test-only",
            model_version="test",
        )
        == SELECTION
    )
    with pytest.raises(SPMInputError, match="hash"):
        resolve_spm_selection(
            "us",
            {"forecast_content_sha256": "b" * 64},
            capability=CAPABILITY,
            policyengine_version="test-only",
            model_version="test",
        )


@pytest.mark.parametrize(
    "kind,area", [("county", None), ("national", None), ("metro", "35620")]
)
def test_partial_selection_survives_ordinary_nested_json(kind, area):
    capability = {
        "contract_version": "canonical-spm-v1",
        "defaults": {
            **SELECTION,
            "geography_kind": kind,
            "geography_id": area,
            "county_vintage": "2010",
            "as_of": "2026-01-01",
        },
    }
    partial = {"scenario": "zero_real"}
    request = SimulationRequest(country="us", spm=partial)
    wire = json.loads(request.model_dump_json())
    assert wire["spm"] == partial
    resolved = resolve_spm_selection(
        "us",
        wire["spm"],
        capability=capability,
        policyengine_version="test",
        model_version="test",
    )
    assert resolved == {**capability["defaults"], **partial}
    assert SPMSelection.model_validate(resolved).model_dump() == resolved
    explicit = {"geography_kind": "county", "county_vintage": "2020", "as_of": None}
    assert resolve_spm_selection(
        "us",
        json.loads(SPMSelection(**explicit).model_dump_json()),
        capability=capability,
        policyengine_version="test",
        model_version="test",
    ) == {**capability["defaults"], **explicit, "geography_id": None}


def test_historical_allowance_does_not_certify_other_models():
    for version, model in [("5.3.0", "1.824.7"), ("5.3.1", "1.764.6")]:
        with pytest.raises(SPMInputError, match="no certified"):
            resolve_spm_selection(
                "us",
                None,
                capability=None,
                policyengine_version=version,
                model_version=model,
            )


def test_annual_child_and_segment_receipts_must_cover_requested_year():
    wrong_year = result(year="2025")
    with pytest.raises(SPMInputError, match="requested year"):
        extract_annual_impact(
            simulation_year="2026", child_result=wrong_year, spm=SELECTION
        )
    with pytest.raises(SPMInputError, match="requested year"):
        combine_spm_results([result(), wrong_year], SELECTION, expected_year=2026)


def test_result_transport_may_omit_nulls_but_not_resolved_options():
    transported = result()
    transported["spm_config"] = {
        key: value
        for key, value in transported["spm_config"].items()
        if value is not None
    }
    assert combine_spm_results([transported], SELECTION)["spm_config"] == SELECTION
    for field in (
        "forecast_content_sha256",
        "scenario",
        "geography_kind",
        "county_vintage",
    ):
        malformed = deepcopy(transported)
        del malformed["spm_config"][field]
        with pytest.raises(SPMInputError, match="complete resolved"):
            combine_spm_results([malformed], SELECTION)


def test_sync_compatibility_endpoint_preserves_explicit_null(monkeypatch):
    import policyengine_simulation_executor.simulation as simulation

    captured = []

    def capture(params):
        captured.append(params)
        raise SPMInputError("SPM_GEOGRAPHY_REQUIRED", "intentional test stop")

    monkeypatch.setattr(simulation, "run_simulation_impl", capture)
    app = FastAPI()
    app.include_router(create_router())
    response = TestClient(app).post(
        "/simulate/economy/comparison",
        json={"country": "us", "spm": {"as_of": None}},
    )
    assert response.status_code == 400
    assert captured[0]["spm"] == {"as_of": None}


def test_year_alias_is_validated_before_dataset_loading(monkeypatch):
    from policyengine_simulation_executor import release_bundle, spm

    monkeypatch.setattr(
        release_bundle,
        "get_country_release_bundle",
        lambda country: SimpleNamespace(
            policyengine_version="test", model_version="test"
        ),
    )
    monkeypatch.setattr(spm, "runtime_spm_capability", lambda: CAPABILITY)
    year_metadata = Mock(
        side_effect=SPMInputError("SPM_YEAR_UNAVAILABLE", "2040 is unavailable")
    )
    monkeypatch.setattr(spm, "_forecast", lambda sha: object())
    monkeypatch.setitem(
        sys.modules,
        "spm_calculator.policyengine_adapter",
        SimpleNamespace(
            PolicyEngineSPMProvider=lambda *a, **k: SimpleNamespace(
                year_metadata=year_metadata
            )
        ),
    )
    with pytest.raises(SPMInputError, match="2040 is unavailable") as error:
        spm.normalize_runtime_spm({"country": "us", "year": "2040", "spm": SELECTION})
    assert error.value.code == "SPM_YEAR_UNAVAILABLE"
    assert year_metadata.call_args.args == (2040,)


class TestResolutionIsNotRepeatedWork:
    """One request resolves its selection about six times; pay once.

    ``_run_simulation_impl_core`` normalizes, ``_build_simulation``
    normalizes again, and the baseline and dataset identity collectors
    normalize once each. Every one of those re-read the installed
    capability and built a throwaway ``PolicyEngineSPMProvider`` to
    prevalidate the years. The installed image cannot change under a
    running container, and the prevalidation reads nothing but the
    selection and the year range, so both are now memoized -- these cases
    fix what that must and must not change.
    """

    @staticmethod
    def _stub_bundle(monkeypatch):
        from policyengine_simulation_executor import release_bundle

        monkeypatch.setattr(
            release_bundle,
            "get_country_release_bundle",
            lambda country: SimpleNamespace(
                policyengine_version="test", model_version="test"
            ),
        )

    @staticmethod
    def _stub_provider(monkeypatch, capability=CAPABILITY):
        """Count the two pieces of work a resolution repeats."""
        import policyengine_simulation_executor.spm as spm

        year_metadata = Mock(return_value={})
        constructions = Mock(
            side_effect=lambda *a, **k: SimpleNamespace(year_metadata=year_metadata)
        )
        monkeypatch.setattr(spm, "runtime_spm_capability", lambda: capability)
        monkeypatch.setattr(spm, "_forecast", lambda sha: object())
        monkeypatch.setitem(
            sys.modules,
            "spm_calculator.policyengine_adapter",
            SimpleNamespace(PolicyEngineSPMProvider=constructions),
        )
        return SimpleNamespace(
            constructions=constructions, year_metadata=year_metadata, spm=spm
        )

    def test_one_selection_and_year_range_prevalidates_once(self, monkeypatch):
        self._stub_bundle(monkeypatch)
        stubs = self._stub_provider(monkeypatch)
        request = {"country": "us", "time_period": "2026", "spm": SELECTION}

        resolutions = [stubs.spm.normalize_runtime_spm(request) for _ in range(6)]

        assert resolutions == [SELECTION] * 6
        assert stubs.constructions.call_count == 1
        assert stubs.year_metadata.call_count == 1

        # The key is the selection and the range, so neither can be served
        # the other's answer.
        stubs.spm.normalize_runtime_spm({**request, "time_period": "2027"})
        assert stubs.constructions.call_count == 2
        stubs.spm.normalize_runtime_spm(
            {**request, "spm": {**SELECTION, "scenario": "zero_real"}}
        )
        assert stubs.constructions.call_count == 3
        stubs.spm.normalize_runtime_spm({**request, "window_size": 2})
        assert stubs.constructions.call_count == 4
        # One year each for the three single-year ranges, two for the window.
        assert stubs.year_metadata.call_count == 5

        stubs.spm.reset_spm_runtime_caches()
        stubs.spm.normalize_runtime_spm(request)
        assert stubs.constructions.call_count == 5

    def test_a_rejected_selection_is_rejected_every_time(self, monkeypatch):
        """Fail-closed cannot be weakened by a cache: exceptions are not kept."""
        self._stub_bundle(monkeypatch)
        stubs = self._stub_provider(monkeypatch)
        stubs.year_metadata.side_effect = SPMInputError(
            "SPM_YEAR_UNAVAILABLE", "2040 is unavailable"
        )
        request = {"country": "us", "time_period": "2040", "spm": SELECTION}

        for _ in range(3):
            with pytest.raises(SPMInputError) as error:
                stubs.spm.normalize_runtime_spm(request)
            assert error.value.code == "SPM_YEAR_UNAVAILABLE"
        assert stubs.constructions.call_count == 3

    def test_an_unusable_year_does_not_mask_the_selection(self, monkeypatch):
        """The order the memo had to preserve.

        The year range used to be parsed between the provider's construction
        and its first use, so a selection the provider itself rejects was
        reported as such even when the year was unusable. The memo is keyed
        on the range, so the range has to be parsed first; an empty range
        keeps the old order for the one request where it differs.
        """
        self._stub_bundle(monkeypatch)
        stubs = self._stub_provider(monkeypatch)
        stubs.constructions.side_effect = ValueError(
            "Unknown forecast scenario: made-up"
        )
        unusable = {"country": "us", "time_period": "2026-01", "spm": SELECTION}

        with pytest.raises(SPMInputError) as error:
            stubs.spm.normalize_runtime_spm(unusable)
        assert error.value.code == "SPM_SCENARIO_UNAVAILABLE"
        assert stubs.year_metadata.call_count == 0

        with pytest.raises(SPMInputError) as error:
            stubs.spm.normalize_runtime_spm({**unusable, "time_period": "2026"})
        assert error.value.code == "SPM_SCENARIO_UNAVAILABLE"

    def test_an_unusable_year_alone_is_a_settings_error(self, monkeypatch):
        self._stub_bundle(monkeypatch)
        stubs = self._stub_provider(monkeypatch)

        with pytest.raises(SPMInputError) as error:
            stubs.spm.normalize_runtime_spm(
                {"country": "us", "time_period": "2026-01", "spm": SELECTION}
            )
        assert error.value.code == "SPM_SETTINGS_INVALID"
        assert stubs.constructions.call_count == 1
        assert stubs.year_metadata.call_count == 0

    def test_collecting_a_baseline_identity_prevalidates_once(self, monkeypatch):
        """The repeat the review counted, through the real collectors.

        ``collect_baseline_identity`` normalizes the request's selection and
        then ``collect_dataset_identity`` normalizes the bundle's default for
        the same year. When both resolve to the same selection that is one
        prevalidation for the pair, and repeating the collection -- as a
        request does -- adds none.
        """
        from fixtures.identity_stubs import install_identity_stubs

        install_identity_stubs(monkeypatch)
        stubs = self._stub_provider(monkeypatch)

        first = artifact_keys.collect_baseline_identity(
            "us", 2026, region="us", scope_key=None, spm={"geography_kind": "national"}
        )
        assert first.spm == SELECTION
        assert stubs.constructions.call_count == 1

        second = artifact_keys.collect_baseline_identity(
            "us", 2026, region="us", scope_key=None, spm={"geography_kind": "national"}
        )
        assert second.storage_id == first.storage_id
        assert stubs.constructions.call_count == 1

        # A request that differs from the bundle default still resolves both.
        county = artifact_keys.collect_baseline_identity(
            "us", 2026, region="us", scope_key=None, spm={"geography_kind": "county"}
        )
        assert county.storage_id != first.storage_id
        assert stubs.constructions.call_count == 2

    def test_the_installed_capability_is_read_once(self, monkeypatch):
        import policyengine.bundle
        import policyengine_simulation_executor.spm as spm

        reads = Mock(return_value={"measurements": {}})
        monkeypatch.setattr(policyengine.bundle, "get_current_bundle", reads)

        assert [spm.runtime_spm_capability() for _ in range(4)] == [None] * 4
        assert reads.call_count == 1

        spm.reset_spm_runtime_caches()
        assert spm.runtime_spm_capability() is None
        assert reads.call_count == 2

    def test_an_unavailable_capability_is_rederived_every_time(self, monkeypatch):
        import policyengine.bundle
        import policyengine_simulation_executor.spm as spm

        reads = Mock(side_effect=ValueError("bundle configuration unavailable"))
        monkeypatch.setattr(policyengine.bundle, "get_current_bundle", reads)

        for _ in range(3):
            with pytest.raises(SPMInputError) as error:
                spm.runtime_spm_capability()
            assert error.value.code == "SPM_CONFIGURATION_UNAVAILABLE"
        assert reads.call_count == 3


@pytest.mark.parametrize(
    "code",
    [
        "SPM_GEOGRAPHY_REQUIRED",
        "SPM_GEOGRAPHY_UNAVAILABLE",
        "SPM_COMPOSITION_REQUIRED",
        "SPM_YEAR_UNAVAILABLE",
        "SPM_SCENARIO_UNAVAILABLE",
    ],
)
def test_actual_http_formula_error_contract(monkeypatch, code):
    error = SPMInputError(code, "Explicit input is required")
    assert pickle.loads(pickle.dumps(error)).to_dict() == error.to_dict()
    monkeypatch.setattr(
        "policyengine_simulation_executor.simulation.run_simulation_impl",
        Mock(side_effect=error),
    )
    app = FastAPI()
    app.include_router(create_router())
    response = TestClient(app).post(
        "/simulate/economy/comparison",
        json={"country": "us", "spm": {"geography_kind": "national"}},
    )
    assert response.status_code == 400
    assert response.json()["errors"] == [error.to_dict()]
    assert response.json()["result"] is None


@pytest.mark.parametrize(
    "key,value",
    [
        ("forecast_content_sha256", "b" * 64),
        ("scenario", "zero_real"),
        ("geography_kind", "county"),
        ("geography_id", "12345"),
        ("county_vintage", "2010"),
        ("as_of", "2025-01-01"),
    ],
)
def test_each_setting_rotates_artifact_identity(key, value):
    common = dict(
        country="us",
        region="national",
        scope_key=None,
        dataset_digest="d" * 64,
        model_version="test",
        policyengine_version="test",
    )
    changed = {**SELECTION, key: value}
    assert artifact_keys.baseline_key(
        **common, spm=SELECTION
    ) != artifact_keys.baseline_key(**common, spm=changed)
    ds = dict(
        country="us",
        dataset="test",
        year=2026,
        data_version="test",
        data_artifact_revision="test",
        source_sha256=None,
        data_build_fingerprint=None,
        model_version="test",
        policyengine_version="test",
    )
    assert artifact_keys.dataset_key(**ds, spm=SELECTION) != artifact_keys.dataset_key(
        **ds, spm=changed
    )


def test_fanout_and_budget_window_preserve_settings_and_receipts():
    child = build_child_payload(
        {"country": "us", "spm": SELECTION, "window_size": 2},
        strip_fields={"window_size"},
        overrides={"time_period": "2027"},
    )
    assert child["spm"] == SELECTION
    rows = [
        extract_annual_impact(
            simulation_year=year, child_result=result(year=year), spm=SELECTION
        )
        for year in ("2026", "2027")
    ]
    window = build_budget_window_result(
        start_year="2026", window_size=2, annual_impacts=rows
    )
    dumped = json.loads(window.model_dump_json())
    assert dumped["annualImpacts"][1]["spm_provenance"]["baseline"][0]["years"] == {
        "2027": {"status": "forecast"}
    }
    merged = combine_spm_results([result(), result()], SELECTION)
    assert len(merged["spm_provenance"]["baseline"]) == 2
    changed = result({**SELECTION, "scenario": "zero_real"})
    for outputs in ([result(), changed], [result(), {"budget": {}}]):
        with pytest.raises(SPMInputError):
            combine_spm_results(outputs, SELECTION)


def test_worker_build_passes_selection_to_baseline_and_reform(monkeypatch):
    import policyengine.core
    from policyengine_simulation_executor import baseline_artifacts
    import policyengine_simulation_executor.spm as spm

    calls = []
    monkeypatch.setattr(spm, "normalize_runtime_spm", lambda params: SELECTION)
    monkeypatch.setattr(
        simulation_runtime, "_country_module", lambda _: SimpleNamespace(model="test")
    )
    monkeypatch.setattr(
        baseline_artifacts,
        "deterministic_baseline_id",
        lambda *a, **k: "baseline-id" if k["policy"] is None else None,
    )
    monkeypatch.setattr(
        baseline_artifacts, "ArtifactBaselineSimulation", lambda **k: calls.append(k)
    )
    monkeypatch.setattr(policyengine.core, "Simulation", lambda **k: calls.append(k))
    for policy in (None, {"test": 123}):
        simulation_runtime._build_simulation(
            {"country": "us", "spm": SELECTION},
            dataset="tiny",
            dataset_selection=simulation_runtime.DatasetSelection("tiny", "tiny", True),
            policy=policy,
            region_code="us",
        )
    assert len(calls) == 2
    assert all(call["spm"] == SELECTION for call in calls)
    assert calls[0]["id"] == "baseline-id"


def test_identity_errors_do_not_degrade_to_an_unidentified_baseline(monkeypatch):
    from policyengine_simulation_executor import baseline_artifacts

    monkeypatch.setattr(
        baseline_artifacts,
        "qualifying_baseline_identity",
        Mock(side_effect=SPMInputError("SPM_CONFIGURATION_UNAVAILABLE", "Uncertified")),
    )
    with pytest.raises(SPMInputError):
        baseline_artifacts.deterministic_baseline_id(
            {},
            country="us",
            dataset_is_default=True,
            policy=None,
            region_code="us",
            scoping_strategy=None,
            year=2026,
        )


@pytest.mark.parametrize(
    "code",
    [
        "SPM_GEOGRAPHY_REQUIRED",
        "SPM_GEOGRAPHY_UNAVAILABLE",
        "SPM_COMPOSITION_REQUIRED",
        "SPM_YEAR_UNAVAILABLE",
        "SPM_SCENARIO_UNAVAILABLE",
    ],
)
def test_gateway_poll_returns_structured_400(monkeypatch, code):
    from policyengine_simulation_gateway import endpoints
    from policyengine_simulation_gateway.testing import create_gateway_app

    monkeypatch.setattr(endpoints, "_job_metadata_store", lambda: {"job": {}})
    monkeypatch.setattr(
        endpoints,
        "modal",
        SimpleNamespace(
            FunctionCall=SimpleNamespace(
                from_id=lambda _: SimpleNamespace(
                    get=Mock(
                        side_effect=SPMInputError(code, "Explicit input is required")
                    )
                )
            )
        ),
    )
    response = TestClient(create_gateway_app()).get("/jobs/job")
    assert response.status_code == 400
    assert response.json()["errors"] == [
        {"code": code, "message": "Explicit input is required"}
    ]
    assert response.json()["result"] is None


def test_gateway_submission_uses_registry_capability_before_spawn(monkeypatch):
    from policyengine_simulation_gateway import endpoints
    from policyengine_simulation_gateway.testing import create_gateway_app
    from policyengine_simulation_contract.gateway_models import PolicyEngineBundle

    route = SimpleNamespace(
        app_name="test-app",
        response_version="test-only",
        policyengine_version="test-only",
        bundle_manifest={},
        country_model_version="test-only",
        route_provenance=None,
    )
    monkeypatch.setattr(endpoints, "resolve_route", lambda *args: route)
    bundle = PolicyEngineBundle(
        model_version="test-only", policyengine_version="test-only", spm=CAPABILITY
    )
    monkeypatch.setattr(endpoints, "_build_policyengine_bundle", lambda *args: bundle)
    spawn = Mock(return_value=SimpleNamespace(object_id="job"))
    monkeypatch.setattr(
        endpoints,
        "modal",
        SimpleNamespace(
            Function=SimpleNamespace(
                from_name=lambda *args: SimpleNamespace(spawn=spawn)
            )
        ),
    )
    monkeypatch.setattr(endpoints, "_job_metadata_store", lambda: {})
    response = TestClient(create_gateway_app()).post(
        "/simulate/economy/comparison",
        json={"country": "us", "spm": {"geography_kind": "national"}},
    )
    assert response.status_code == 200
    assert spawn.call_args.args[0]["spm"] == SELECTION
    assert (
        SPMCapability.model_validate(
            response.json()["policyengine_bundle"]["spm"]
        ).model_dump()
        == CAPABILITY
    )
    bundle.spm = None
    spawn.reset_mock()
    response = TestClient(create_gateway_app()).post(
        "/simulate/economy/comparison", json={"country": "us"}
    )
    assert response.status_code == 400
    spawn.assert_not_called()


def test_segmented_child_input_error_is_not_redacted_or_retried(monkeypatch):
    from src.modal.segmented_national import SegmentedNationalRunner

    child = SimpleNamespace(
        get=Mock(
            side_effect=SPMInputError("SPM_GEOGRAPHY_REQUIRED", "County required")
        ),
        cancel=Mock(),
    )
    runner = object.__new__(SegmentedNationalRunner)
    runner.poll_interval_initial_seconds = 0.001
    runner.poll_interval_max_seconds = 0.001
    with pytest.raises(SPMInputError) as error:
        runner._collect([(["state/ca"], child)])
    assert error.value.code == "SPM_GEOGRAPHY_REQUIRED"
    child.get.assert_called_once()
    child.cancel.assert_called_once()


def test_budget_window_state_keeps_typed_failure_on_replay():
    from policyengine_simulation_contract.gateway_models import BudgetWindowBatchState
    from policyengine_simulation_contract.budget_window_state import (
        build_batch_status_response,
    )
    from policyengine_simulation_gateway.responses import batch_status_response

    state = BudgetWindowBatchState(
        batch_job_id="job",
        status="failed",
        country="us",
        region="us",
        version="test",
        resolved_app_name="app",
        policyengine_bundle={"model_version": "test"},
        start_year="2026",
        window_size=2,
        max_parallel=2,
        created_at="test",
        updated_at="test",
        error="County required",
        errors=[{"code": "SPM_GEOGRAPHY_REQUIRED", "message": "County required"}],
    )
    replay = BudgetWindowBatchState.model_validate_json(state.model_dump_json())
    response = batch_status_response(build_batch_status_response(replay))
    assert response.status_code == 400
    assert json.loads(response.body)["errors"][0]["code"] == "SPM_GEOGRAPHY_REQUIRED"


@pytest.mark.parametrize(
    "code",
    [
        "SPM_GEOGRAPHY_REQUIRED",
        "SPM_GEOGRAPHY_UNAVAILABLE",
        "SPM_COMPOSITION_REQUIRED",
        "SPM_YEAR_UNAVAILABLE",
        "SPM_SCENARIO_UNAVAILABLE",
    ],
)
def test_country_error_is_transportable_without_country_package(monkeypatch, code):
    from contextlib import nullcontext

    class CountryError(ValueError):
        def __init__(self, code, message):
            self.code = code
            super().__init__(message)

    monkeypatch.setattr(simulation_runtime, "setup_gcp_credentials", nullcontext)
    monkeypatch.setattr(
        simulation_runtime,
        "_run_simulation_impl_core",
        Mock(side_effect=CountryError(code, "Observed input required")),
    )
    with pytest.raises(SPMInputError) as caught:
        simulation_runtime.run_simulation_impl({"country": "us"})
    transported = pickle.loads(pickle.dumps(caught.value))
    assert transported.to_dict() == {"code": code, "message": "Observed input required"}


@pytest.mark.parametrize(
    "code",
    [
        "SPM_GEOGRAPHY_REQUIRED",
        "SPM_GEOGRAPHY_UNAVAILABLE",
        "SPM_COMPOSITION_REQUIRED",
        "SPM_YEAR_UNAVAILABLE",
        "SPM_SCENARIO_UNAVAILABLE",
    ],
)
def test_optional_analysis_does_not_swallow_spm_input_errors(code):
    from policyengine_simulation_executor.simulation_output_common import (
        _try_compute_output,
    )

    with pytest.raises(SPMInputError):
        _try_compute_output(
            "optional impact",
            Mock(side_effect=SPMInputError(code, "Explicit input required")),
        )
    assert (
        _try_compute_output(
            "optional impact", Mock(side_effect=RuntimeError("legacy optional output"))
        )
        is None
    )


@pytest.mark.parametrize(
    "date_fields,expected_as_of",
    [({"as_of": None}, None), ({}, "2026-01-01")],
)
def test_as_of_presence_survives_budget_parent(date_fields, expected_as_of):
    from policyengine_simulation_gateway.endpoints import (
        _resolve_request_spm,
        _build_budget_window_parent_payload,
    )
    from policyengine_simulation_contract.gateway_models import PolicyEngineBundle

    request = BudgetWindowBatchRequest(
        country="us",
        region="us",
        start_year="2026",
        window_size=2,
        spm={"geography_kind": "national", **date_fields},
    )
    bundle = PolicyEngineBundle(
        model_version="test",
        policyengine_version="test",
        spm={
            "contract_version": "canonical-spm-v1",
            "defaults": {**SELECTION, "as_of": "2026-01-01"},
        },
    )
    selection = _resolve_request_spm(
        request,
        bundle,
        SimpleNamespace(
            bundle_manifest={}, country_model_version=None, route_provenance=None
        ),
    )
    assert selection["as_of"] == expected_as_of
    parent = _build_budget_window_parent_payload(
        request, resolved_version="test", resolved_app_name="test", bundle=bundle
    )
    assert parent["spm"] == selection


def test_versions_http_retains_capability_contract_version(monkeypatch):
    from policyengine_simulation_gateway import endpoints
    from policyengine_simulation_gateway.testing import create_gateway_app
    from src.modal.utils import update_version_registry as registry

    metadata = {
        "app_name": "test-app",
        "policyengine_version": "9.9.9",
        "us": {"model_version": "test-us"},
        "uk": {"model_version": "test-uk"},
        "spm": CAPABILITY,
    }
    monkeypatch.setattr(
        registry, "build_bundle_manifest_metadata", lambda **kwargs: metadata
    )
    state = registry.build_next_routing_state(
        current_state=None,
        app_name="test-app",
        policyengine_version="9.9.9",
        us_version="test-us",
        uk_version="test-uk",
    )
    monkeypatch.setattr(endpoints, "_active_routing_state", lambda: state)
    response = TestClient(create_gateway_app()).get("/versions")
    assert response.status_code == 200
    payload = response.json()
    assert payload["us"]["test-us"] == "test-app"
    assert payload["policyengine"]["9.9.9"] == "test-app"
    assert payload["spm_capabilities"] == {"9.9.9": CAPABILITY}
    assert set(state["bundles"]) == {"9.9.9"}
