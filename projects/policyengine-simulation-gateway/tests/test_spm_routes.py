"""SPM capability resolution on historical and ambiguous registry routes."""

from copy import deepcopy

import pytest

from fixtures.gateway_endpoints import TEST_ROUTING_STATE


CAPABILITY = {
    "contract_version": "canonical-spm-v1",
    "defaults": {"forecast_content_sha256": "a" * 64, "scenario": "ce_trend"},
}
ENDPOINTS = [
    ("/simulate/economy/comparison", {"scope": "macro"}),
    (
        "/simulate/economy/budget-window",
        {"region": "us", "start_year": "2026", "window_size": 2},
    ),
]


# ``generation`` is one global marker that every publish rewrites, so a
# seeded route has to resolve the same before and after a canonical deploy.
PUBLISHED_GENERATION = "5.4.0:policyengine-simulation-py5-4-0"
LEGACY_SOURCES = ["legacy-country-dict", "legacy-seed", PUBLISHED_GENERATION]


def legacy_route(mock_modal, source, model_version):
    """Two shapes of pre-canonical route: the original per-country Modal
    dicts, and a seeded routing-state country route the registry ties to no
    wrapper bundle. For the latter, ``source`` is the state's ``generation``
    marker, which must not change how the route resolves."""
    app_name = "policyengine-simulation-us1-715-2-uk2-88-20"
    if source == "legacy-country-dict":
        app_name = "legacy-app"
        del mock_modal["dicts"]["simulation-api-routing-state"]
        mock_modal["dicts"]["simulation-api-us-versions"] = {
            "latest": model_version,
            model_version: app_name,
        }
    else:
        mock_modal["dicts"]["simulation-api-routing-state"] = {
            "active": {
                "schema_version": 1,
                "generation": source,
                "latest": {"us": model_version},
                "routes": {"policyengine": {}, "us": {model_version: app_name}},
                "bundles": {},
            }
        }


@pytest.mark.parametrize("endpoint,extra", ENDPOINTS)
@pytest.mark.parametrize("source", LEGACY_SOURCES)
@pytest.mark.parametrize("version", [None, "1.715.2"])
def test_historical_no_wrapper_route_submits_without_spm(
    mock_modal, client, endpoint, extra, source, version
):
    legacy_route(mock_modal, source, "1.715.2")
    response = client.post(
        endpoint, json={"country": "us", "version": version, **extra}
    )
    assert response.status_code == 200, response.text
    assert "spm" not in mock_modal["func"].last_payload
    assert response.json()["policyengine_bundle"]["model_version"] == "1.715.2"
    assert "policyengine_version" not in response.json()["policyengine_bundle"]


@pytest.mark.parametrize("endpoint,extra", ENDPOINTS)
@pytest.mark.parametrize("source", LEGACY_SOURCES)
@pytest.mark.parametrize("selection", [{}, {"geography_kind": "national"}])
def test_historical_route_rejects_any_explicit_spm(
    mock_modal, client, endpoint, extra, source, selection
):
    legacy_route(mock_modal, source, "1.715.2")
    response = client.post(endpoint, json={"country": "us", "spm": selection, **extra})
    assert response.status_code == 400
    assert response.json()["errors"][0]["code"] == "SPM_CONFIGURATION_UNAVAILABLE"
    assert mock_modal["func"].calls == []


@pytest.mark.parametrize("endpoint,extra", ENDPOINTS)
@pytest.mark.parametrize(
    "source,model_version",
    [
        ("legacy-country-dict", "1.824.7"),
        ("legacy-seed", "1.824.7"),
        (PUBLISHED_GENERATION, "1.824.7"),
        ("legacy-seed", "future-model"),
        (PUBLISHED_GENERATION, "future-model"),
    ],
)
def test_missing_wrapper_does_not_certify_unknown_routes(
    mock_modal, client, endpoint, extra, source, model_version
):
    legacy_route(mock_modal, source, model_version)
    response = client.post(endpoint, json={"country": "us", **extra})
    assert response.status_code == 400
    assert response.json()["errors"][0]["code"] == "SPM_CONFIGURATION_UNAVAILABLE"
    assert mock_modal["func"].calls == []


@pytest.mark.parametrize("endpoint,extra", ENDPOINTS)
def test_legacy_seed_cannot_erase_future_wrapper_from_app_name(
    mock_modal, client, endpoint, extra
):
    legacy_route(mock_modal, "legacy-seed", "1.715.2")
    state = mock_modal["dicts"]["simulation-api-routing-state"]["active"]
    state["routes"]["us"]["1.715.2"] = "policyengine-simulation-py99-0-0"
    response = client.post(endpoint, json={"country": "us", **extra})
    assert response.status_code == 400
    assert response.json()["errors"][0]["code"] == "SPM_CONFIGURATION_UNAVAILABLE"
    assert mock_modal["func"].calls == []


@pytest.mark.parametrize("schema_version", [None, 2])
def test_legacy_seed_requires_supported_registry_schema(
    mock_modal, client, schema_version
):
    legacy_route(mock_modal, "legacy-seed", "1.715.2")
    state = mock_modal["dicts"]["simulation-api-routing-state"]["active"]
    state["schema_version"] = schema_version
    response = client.post("/simulate/economy/comparison", json={"country": "us"})
    assert response.status_code == 400
    assert response.json()["errors"][0]["code"] == "SPM_CONFIGURATION_UNAVAILABLE"
    assert mock_modal["func"].calls == []


def shared_app_state(mock_modal, *, sibling_model):
    state = deepcopy(TEST_ROUTING_STATE)
    original = state["bundles"]["4.10.0"]
    app_name = original["app_name"]
    state["routes"]["policyengine"]["5.3.0"] = app_name
    state["bundles"]["5.3.0"] = {
        **deepcopy(original),
        "policyengine_version": "5.3.0",
        "us": {**original["us"], "model_version": sibling_model},
        "spm": deepcopy(CAPABILITY),
    }
    state["routes"]["us"][sibling_model] = app_name
    mock_modal["dicts"]["simulation-api-routing-state"] = {"active": state}
    return state


@pytest.mark.parametrize("endpoint,extra", ENDPOINTS)
def test_shared_app_resolves_unique_country_model_bundle(
    mock_modal, client, endpoint, extra
):
    shared_app_state(mock_modal, sibling_model="1.824.7")
    response = client.post(
        endpoint, json={"country": "us", "version": "1.824.7", **extra}
    )
    assert response.status_code == 200, response.text
    assert response.json()["policyengine_bundle"]["policyengine_version"] == "5.3.0"
    assert mock_modal["func"].last_payload["spm"]["scenario"] == "ce_trend"


def test_latest_route_alias_does_not_create_ambiguous_bundle(mock_modal, client):
    state = shared_app_state(mock_modal, sibling_model="1.824.7")
    state["routes"]["policyengine"]["latest"] = state["routes"]["policyengine"]["5.3.0"]
    response = client.post(
        "/simulate/economy/comparison", json={"country": "us", "version": "1.824.7"}
    )
    assert response.status_code == 200
    assert response.json()["policyengine_bundle"]["policyengine_version"] == "5.3.0"


@pytest.mark.parametrize("endpoint,extra", ENDPOINTS)
def test_shared_app_with_ambiguous_country_model_requires_explicit_bundle(
    mock_modal, client, endpoint, extra
):
    shared_app_state(mock_modal, sibling_model="1.500.0")
    response = client.post(
        endpoint, json={"country": "us", "version": "1.500.0", **extra}
    )
    assert response.status_code == 400
    assert "policyengine_version" in response.json()["detail"]
    assert mock_modal["func"].calls == []
    explicit = client.post(
        endpoint,
        json={"country": "us", "policyengine_version": "5.3.0", **extra},
    )
    assert explicit.status_code == 200
    assert mock_modal["func"].last_payload["spm"]["scenario"] == "ce_trend"


@pytest.mark.parametrize("endpoint,extra", ENDPOINTS)
@pytest.mark.parametrize("sibling_model", [None, "", "   ", 42])
def test_shared_app_with_missing_model_metadata_is_ambiguous(
    mock_modal, client, endpoint, extra, sibling_model
):
    state = shared_app_state(mock_modal, sibling_model="1.824.7")
    state["bundles"]["4.10.0"]["us"]["model_version"] = sibling_model
    response = client.post(
        endpoint, json={"country": "us", "version": "1.824.7", **extra}
    )
    assert response.status_code == 400
    assert "policyengine_version" in response.json()["detail"]
    assert mock_modal["func"].calls == []


@pytest.mark.parametrize(
    "invalid_capability",
    [
        {"contract_version": "canonical-spm-v1", "defaults": {"scenario": "ce_trend"}},
        {**CAPABILITY, "contract_version": "unknown-contract"},
        {**CAPABILITY, "undeclared": True},
        {"defaults": {**CAPABILITY["defaults"], "geography_kind": "metro"}},
    ],
)
def test_versions_omits_malformed_capability_and_submission_rejects_it(
    mock_modal, client, invalid_capability
):
    state = shared_app_state(mock_modal, sibling_model="1.824.7")
    state["bundles"]["4.10.0"]["spm"] = invalid_capability
    response = client.get("/versions")
    assert response.status_code == 200
    assert set(response.json()["spm_capabilities"]) == {"5.3.0"}
    rejected = client.post(
        "/simulate/economy/comparison",
        json={"country": "us", "policyengine_version": "4.10.0"},
    )
    assert rejected.status_code == 400
    assert rejected.json()["errors"][0]["code"] == "SPM_CONFIGURATION_UNAVAILABLE"
    assert mock_modal["func"].calls == []


RESOLVED_SELECTION = {
    "forecast_content_sha256": "a" * 64,
    "scenario": "ce_trend",
    "geography_kind": "national",
    "geography_id": None,
    "county_vintage": "2020",
    "as_of": None,
}


def spm_receipt(year):
    return {
        "forecast_id": "ce-forecast",
        "forecast_sha256": "a" * 64,
        "scenario": "ce_trend",
        "geography_kind": "national",
        "runtime_versions": {"spm_calculator": "0.3.1"},
        "years": {str(year): {"entry": f"{year}-01-01"}},
        "geographies": [{"kind": "national"}],
        "composition_method": "national",
        "storage_method": "artifact",
    }


def spm_provenance(year):
    return {"baseline": [spm_receipt(year)], "reform": [spm_receipt(year)]}


def test_completed_result_body_keeps_resolved_nulls(mock_modal, client):
    """A client must be able to replay the selection it was handed back.

    ``response_model_exclude_none`` applies to every poll body, and an
    omitted option inherits the bundle default. Dropping a resolved null
    would silently re-resolve ``as_of`` and ``geography_id`` on the next
    request, changing the baseline key and the receipts.
    """
    shared_app_state(mock_modal, sibling_model="1.824.7")
    submitted = client.post(
        "/simulate/economy/comparison",
        json={
            "country": "us",
            "version": "1.824.7",
            "spm": {"geography_kind": "national", "as_of": None},
        },
    )
    assert submitted.status_code == 200, submitted.text
    assert mock_modal["func"].last_payload["spm"] == RESOLVED_SELECTION

    job_id = submitted.json()["job_id"]
    call = mock_modal["function_call"].registry[job_id]
    call.result = {
        **call.result,
        "spm_config": RESOLVED_SELECTION,
        "spm_provenance": spm_provenance(2026),
    }

    polled = client.get(f"/jobs/{job_id}")
    assert polled.status_code == 200, polled.text
    assert polled.json()["result"]["spm_config"] == RESOLVED_SELECTION


def test_completed_budget_window_rows_keep_resolved_nulls(mock_modal, client):
    """Each ``annualImpacts`` row carries the same replayable selection."""
    from policyengine_simulation_contract.budget_window_state import (
        put_batch_job_state,
    )
    from policyengine_simulation_contract.gateway_models import (
        BudgetWindowAnnualImpact,
        BudgetWindowBatchState,
        BudgetWindowResult,
        BudgetWindowTotals,
        PolicyEngineBundle,
    )

    shared_app_state(mock_modal, sibling_model="1.824.7")
    impact = BudgetWindowAnnualImpact(
        spm_config=RESOLVED_SELECTION,
        spm_provenance=spm_provenance(2026),
        year="2026",
        taxRevenueImpact=10,
        federalTaxRevenueImpact=7,
        stateTaxRevenueImpact=3,
        benefitSpendingImpact=5,
        budgetaryImpact=15,
    )
    put_batch_job_state(
        BudgetWindowBatchState(
            batch_job_id="mock-batch-job-id-123",
            status="complete",
            country="us",
            region="us",
            version="1.824.7",
            target="general",
            resolved_app_name="policyengine-simulation-py4-10-0",
            policyengine_bundle=PolicyEngineBundle(model_version="1.824.7"),
            start_year="2026",
            window_size=1,
            max_parallel=1,
            request_payload={"country": "us", "spm": RESOLVED_SELECTION},
            years=["2026"],
            queued_years=[],
            running_years=[],
            completed_years=["2026"],
            failed_years=[],
            child_jobs={},
            partial_annual_impacts={},
            result=BudgetWindowResult(
                startYear="2026",
                endYear="2026",
                windowSize=1,
                annualImpacts=[impact],
                totals=BudgetWindowTotals(
                    taxRevenueImpact=10,
                    federalTaxRevenueImpact=7,
                    stateTaxRevenueImpact=3,
                    benefitSpendingImpact=5,
                    budgetaryImpact=15,
                ),
            ),
            error=None,
            created_at="2026-01-01T00:00:00+00:00",
            updated_at="2026-01-01T00:00:01+00:00",
            observability_id="batch-run-123",
        )
    )

    polled = client.get("/budget-window-jobs/mock-batch-job-id-123")
    assert polled.status_code == 200, polled.text
    row = polled.json()["result"]["annualImpacts"][0]
    assert row["spm_config"] == RESOLVED_SELECTION


# Every optional field the pre-SPM gateway emitted in a raw 202/500 body.
LEGACY_BUNDLE_KEYS = {
    "model_version",
    "policyengine_version",
    "data_version",
    "dataset",
}


@pytest.mark.parametrize("source", LEGACY_SOURCES)
def test_legacy_poll_bodies_carry_no_spm_key(mock_modal, client, source):
    """The 202 and 500 bodies splat job metadata in raw, bypassing the
    routes' ``response_model_exclude_none``. A legacy no-SPM job must stay
    byte-identical to the pre-SPM gateway."""
    legacy_route(mock_modal, source, "1.715.2")
    submitted = client.post("/simulate/economy/comparison", json={"country": "us"})
    assert submitted.status_code == 200, submitted.text
    job_id = submitted.json()["job_id"]
    call = mock_modal["function_call"].registry[job_id]

    call.running = True
    running = client.get(f"/jobs/{job_id}")
    assert running.status_code == 202
    assert set(running.json()["policyengine_bundle"]) == LEGACY_BUNDLE_KEYS

    call.running = False
    call.error = RuntimeError("worker exploded")
    failed = client.get(f"/jobs/{job_id}")
    assert failed.status_code == 500
    assert set(failed.json()["policyengine_bundle"]) == LEGACY_BUNDLE_KEYS


@pytest.mark.parametrize("source", LEGACY_SOURCES)
def test_legacy_budget_window_metadata_carries_no_spm_key(mock_modal, client, source):
    """The same bundle rides to the worker in ``_metadata``."""
    legacy_route(mock_modal, source, "1.715.2")
    submitted = client.post(
        "/simulate/economy/budget-window",
        json={"country": "us", "region": "us", "start_year": "2026", "window_size": 2},
    )
    assert submitted.status_code == 200, submitted.text
    metadata = mock_modal["func"].last_payload["_metadata"]
    assert set(metadata["policyengine_bundle"]) == LEGACY_BUNDLE_KEYS


def test_canonical_poll_bodies_still_carry_the_capability(mock_modal, client):
    """Omission is scoped to routes with no capability, not to all routes."""
    shared_app_state(mock_modal, sibling_model="1.824.7")
    submitted = client.post(
        "/simulate/economy/comparison", json={"country": "us", "version": "1.824.7"}
    )
    assert submitted.status_code == 200, submitted.text
    job_id = submitted.json()["job_id"]
    mock_modal["function_call"].registry[job_id].running = True

    running = client.get(f"/jobs/{job_id}")
    assert running.status_code == 202
    bundle = running.json()["policyengine_bundle"]
    assert set(bundle) == LEGACY_BUNDLE_KEYS | {"spm"}
    assert bundle["spm"]["defaults"]["scenario"] == "ce_trend"


@pytest.mark.parametrize("endpoint,extra", ENDPOINTS)
def test_publishing_does_not_revoke_a_seeded_country_route(
    mock_modal, client, endpoint, extra
):
    """The route is unchanged across a publish; only the global marker moved.

    Keying the allowance on ``generation`` meant the first canonical deploy
    turned every seeded country route's bare submission into a 400.
    """
    for generation in ("legacy-seed", PUBLISHED_GENERATION):
        legacy_route(mock_modal, generation, "1.715.2")
        response = client.post(endpoint, json={"country": "us", **extra})
        assert response.status_code == 200, f"{generation}: {response.text}"
        assert "spm" not in mock_modal["func"].last_payload


def wrapper_route_without_manifest(mock_modal, wrapper):
    """A seeded wrapper route the app-release snapshot had no bundle for."""
    state = deepcopy(TEST_ROUTING_STATE)
    app_name = f"policyengine-simulation-py{wrapper.replace('.', '-')}"
    state["routes"]["policyengine"][wrapper] = app_name
    mock_modal["dicts"]["simulation-api-routing-state"] = {"active": state}


@pytest.mark.parametrize("endpoint,extra", ENDPOINTS)
@pytest.mark.parametrize("wrapper", ["5.2.0", "5.3.0"])
def test_pinned_wrapper_route_without_manifest_submits_without_spm(
    mock_modal, client, endpoint, extra, wrapper
):
    """With no manifest the response's ``model_version`` echoes the wrapper
    version. Reading that back as a country model version rejected the two
    pinned pre-canonical bundles."""
    wrapper_route_without_manifest(mock_modal, wrapper)
    response = client.post(
        endpoint, json={"country": "us", "policyengine_version": wrapper, **extra}
    )
    assert response.status_code == 200, response.text
    assert "spm" not in mock_modal["func"].last_payload
    assert response.json()["policyengine_bundle"]["model_version"] == wrapper


@pytest.mark.parametrize("endpoint,extra", ENDPOINTS)
@pytest.mark.parametrize("wrapper", ["5.2.0", "5.3.0"])
def test_pinned_wrapper_through_legacy_dicts_submits_without_spm(
    mock_modal, client, endpoint, extra, wrapper
):
    """Same route shape through the pre-registry Modal dicts."""
    del mock_modal["dicts"]["simulation-api-routing-state"]
    app_name = f"policyengine-simulation-py{wrapper.replace('.', '-')}"
    mock_modal["dicts"]["simulation-api-policyengine-versions"] = {wrapper: app_name}
    response = client.post(
        endpoint, json={"country": "us", "policyengine_version": wrapper, **extra}
    )
    assert response.status_code == 200, response.text
    assert "spm" not in mock_modal["func"].last_payload


@pytest.mark.parametrize("endpoint,extra", ENDPOINTS)
def test_unpinned_wrapper_route_without_manifest_still_fails_closed(
    mock_modal, client, endpoint, extra
):
    """Only the two pinned pre-canonical wrappers are vouched for without a
    manifest; an unrecognized bundle proves nothing."""
    wrapper_route_without_manifest(mock_modal, "5.4.0")
    response = client.post(
        endpoint, json={"country": "us", "policyengine_version": "5.4.0", **extra}
    )
    assert response.status_code == 400
    assert response.json()["errors"][0]["code"] == "SPM_CONFIGURATION_UNAVAILABLE"
    assert mock_modal["func"].calls == []


@pytest.mark.parametrize("endpoint,extra", ENDPOINTS)
def test_stated_model_version_still_contradicts_the_wrapper_pin(
    mock_modal, client, endpoint, extra
):
    """A manifest that states a post-canonical model under a pinned wrapper
    is a republish we cannot vouch for."""
    state = deepcopy(TEST_ROUTING_STATE)
    original = state["bundles"]["4.10.0"]
    state["routes"]["policyengine"]["5.3.0"] = original["app_name"]
    state["bundles"]["5.3.0"] = {
        **deepcopy(original),
        "policyengine_version": "5.3.0",
        "us": {**original["us"], "model_version": "1.824.7"},
    }
    mock_modal["dicts"]["simulation-api-routing-state"] = {"active": state}
    response = client.post(
        endpoint, json={"country": "us", "policyengine_version": "5.3.0", **extra}
    )
    assert response.status_code == 400
    assert response.json()["errors"][0]["code"] == "SPM_CONFIGURATION_UNAVAILABLE"
    assert mock_modal["func"].calls == []


def republished_wrapper_state(mock_modal, *, model_version):
    """The registry a re-publish leaves behind.

    ``update_version_registry`` only ever adds country routes and it
    overwrites the bundle manifest for the wrapper version it deploys, so
    re-publishing one wrapper with an upgraded country model leaves
    ``routes["us"]["1.459.0"]`` pointing at an app whose manifest now states
    something else. Nothing prunes it.
    """
    state = deepcopy(TEST_ROUTING_STATE)
    state["bundles"]["3.9.0"]["us"] = {
        **state["bundles"]["3.9.0"]["us"],
        "model_version": model_version,
    }
    mock_modal["dicts"]["simulation-api-routing-state"] = {"active": state}
    return state


@pytest.mark.parametrize("endpoint,extra", ENDPOINTS)
def test_stale_country_route_is_refused_rather_than_served(
    mock_modal, client, endpoint, extra
):
    """The behaviour change. Before, this route resolved and the 202 body
    contradicted itself: ``version`` from the stale route,
    ``policyengine_bundle.model_version`` from the live manifest."""
    republished_wrapper_state(mock_modal, model_version="1.470.0")
    response = client.post(
        endpoint, json={"country": "us", "version": "1.459.0", **extra}
    )
    assert response.status_code == 400
    detail = response.json()["detail"]
    assert "1.459.0" in detail and "1.470.0" in detail
    # A routing refusal, not an SPM one.
    assert "errors" not in response.json()
    assert mock_modal["func"].calls == []


@pytest.mark.parametrize("endpoint,extra", ENDPOINTS)
def test_a_country_route_its_bundle_still_states_is_served(
    mock_modal, client, endpoint, extra
):
    """The other half: a route the manifest agrees with keeps working, so
    the refusal above is about disagreement and not about single-candidate
    country routes in general."""
    republished_wrapper_state(mock_modal, model_version="1.459.0")
    response = client.post(
        endpoint, json={"country": "us", "version": "1.459.0", **extra}
    )
    assert response.status_code == 200, response.text
    bundle = response.json()["policyengine_bundle"]
    assert bundle["policyengine_version"] == "3.9.0"
    assert bundle["model_version"] == "1.459.0"


@pytest.mark.parametrize("endpoint,extra", ENDPOINTS)
@pytest.mark.parametrize("model_version", [None, "", "   ", 42])
def test_a_bundle_that_states_no_model_version_contradicts_nothing(
    mock_modal, client, endpoint, extra, model_version
):
    """Unstated is unstated, whichever way it is unstated.

    The ambiguity check classifies a blank or whitespace model version as
    unstated; the shared validator reads any string as stated. Deriving the
    same fact twice made a blank manifest entry a 400 here while
    ``test_shared_app_with_missing_model_metadata_is_ambiguous`` treats the
    identical value as missing metadata.
    """
    state = republished_wrapper_state(mock_modal, model_version=model_version)
    if model_version is None:
        del state["bundles"]["3.9.0"]["us"]["model_version"]
    response = client.post(
        endpoint, json={"country": "us", "version": "1.459.0", **extra}
    )
    assert response.status_code == 200, response.text
    assert response.json()["policyengine_bundle"]["policyengine_version"] == "3.9.0"
