"""Characterization tests for route resolution from the active routing state."""

import re
from copy import deepcopy
from dataclasses import asdict

import pytest
from fixtures.gateway_endpoints import TEST_APP_RELEASE_BUNDLE, TEST_ROUTING_STATE
from policyengine_simulation_gateway.endpoints import _resolve_from_active_state

CURRENT_APP = "policyengine-simulation-py4-10-0"
PREVIOUS_APP = "policyengine-simulation-py3-9-0"
PREVIOUS_BUNDLE = TEST_ROUTING_STATE["bundles"]["3.9.0"]


def _resolve(state=None, *, country="us", version=None, policyengine_version=None):
    return asdict(
        _resolve_from_active_state(
            state=deepcopy(TEST_ROUTING_STATE) if state is None else state,
            country=country,
            version=version,
            policyengine_version=policyengine_version,
        )
    )


def _policyengine_route(version, app_name, bundle, *, response_version=None):
    return {
        "app_name": app_name,
        "response_version": response_version or version,
        "policyengine_version": version,
        "bundle_manifest": bundle,
        "route_provenance": None,
        "country_model_version": None,
    }


def _state_without_policyengine_latest():
    state = deepcopy(TEST_ROUTING_STATE)
    del state["latest"]["policyengine"]
    return state


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        pytest.param(
            {"policyengine_version": "4.10.0"},
            _policyengine_route("4.10.0", CURRENT_APP, TEST_APP_RELEASE_BUNDLE),
            id="pinned-wrapper",
        ),
        pytest.param(
            {"policyengine_version": "3.9.0", "version": "1.459.0"},
            _policyengine_route("3.9.0", PREVIOUS_APP, PREVIOUS_BUNDLE),
            id="pinned-wrapper-with-matching-model",
        ),
        pytest.param(
            {},
            _policyengine_route("4.10.0", CURRENT_APP, TEST_APP_RELEASE_BUNDLE),
            id="latest-wrapper",
        ),
        pytest.param(
            {"state": _state_without_policyengine_latest()},
            {
                "app_name": CURRENT_APP,
                "response_version": "1.500.0",
                "policyengine_version": "4.10.0",
                "bundle_manifest": TEST_APP_RELEASE_BUNDLE,
                "route_provenance": None,
                "country_model_version": "1.500.0",
            },
            id="latest-country-model",
        ),
        pytest.param(
            {"version": "1.459.0"},
            {
                "app_name": PREVIOUS_APP,
                "response_version": "1.459.0",
                "policyengine_version": "3.9.0",
                "bundle_manifest": PREVIOUS_BUNDLE,
                "route_provenance": None,
                "country_model_version": "1.459.0",
            },
            id="country-model",
        ),
        pytest.param(
            {"version": "3.9.0"},
            _policyengine_route("3.9.0", PREVIOUS_APP, PREVIOUS_BUNDLE),
            id="wrapper-version-as-version",
        ),
    ],
)
def test_active_state_resolves_routes(arguments, expected):
    assert _resolve(**arguments) == expected


def test_version_naming_one_app_as_both_kinds_resolves_the_wrapper():
    state = deepcopy(TEST_ROUTING_STATE)
    state["routes"]["us"]["4.10.0"] = CURRENT_APP
    state["bundles"]["4.10.0"] = {
        **TEST_APP_RELEASE_BUNDLE,
        "us": {**TEST_APP_RELEASE_BUNDLE["us"], "model_version": "4.10.0"},
    }

    assert _resolve(state, version="4.10.0") == _policyengine_route(
        "4.10.0", CURRENT_APP, state["bundles"]["4.10.0"]
    )


def test_legacy_country_route_without_a_bundle_is_marked_pre_canonical():
    state = deepcopy(TEST_ROUTING_STATE)
    state["routes"]["us"]["1.400.0"] = "legacy-app"

    assert _resolve(state, version="1.400.0") == {
        "app_name": "legacy-app",
        "response_version": "1.400.0",
        "policyengine_version": None,
        "bundle_manifest": {},
        "route_provenance": "legacy-country-route",
        "country_model_version": "1.400.0",
    }


def _ambiguous_state():
    state = deepcopy(TEST_ROUTING_STATE)
    state["routes"]["us"]["3.9.0"] = "legacy-app"
    return state


def _stale_country_route_state():
    state = deepcopy(TEST_ROUTING_STATE)
    state["routes"]["us"]["3.9.0"] = CURRENT_APP
    return state


def _state_without_latest_routes():
    state = _state_without_policyengine_latest()
    state["latest"]["us"] = "9.9.9"
    return state


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        pytest.param(
            {"policyengine_version": "9.9.9"},
            "Unknown policyengine.py version 9.9.9",
            id="unknown-wrapper",
        ),
        pytest.param(
            {"policyengine_version": "3.9.0", "version": "1.459.0", "country": "uk"},
            "Requested uk version 1.459.0 does not match policyengine.py bundle "
            "3.9.0 (uk model version 2.66.0)",
            id="pinned-wrapper-checks-the-requested-country",
        ),
        pytest.param(
            {"policyengine_version": "4.10.0", "version": "1.459.0"},
            "Requested us version 1.459.0 does not match policyengine.py bundle "
            "4.10.0 (us model version 1.500.0)",
            id="pinned-wrapper-with-other-model",
        ),
        pytest.param(
            {"state": _state_without_latest_routes()},
            "Routing state does not define a latest route",
            id="latest-country-route-missing",
        ),
        pytest.param(
            {"state": {"routes": {}}},
            "Routing state does not define a latest route",
            id="no-latest",
        ),
        pytest.param(
            {"state": _ambiguous_state(), "version": "3.9.0"},
            "Ambiguous version 3.9.0 for country us; pass policyengine_version "
            "to select a bundle explicitly",
            id="ambiguous-version",
        ),
        pytest.param(
            {"state": _stale_country_route_state(), "version": "3.9.0"},
            "Requested us version 3.9.0 does not match policyengine.py bundle "
            "4.10.0 (us model version 1.500.0)",
            id="country-route-checked-before-ambiguity",
        ),
        pytest.param(
            {"version": "0.0.1"},
            "Unknown version 0.0.1 for country us",
            id="unknown-version",
        ),
        pytest.param(
            {"version": "0.0.1", "country": "uk"},
            "Unknown version 0.0.1 for country uk",
            id="unknown-uk-version",
        ),
    ],
)
def test_active_state_rejects_unresolvable_routes(arguments, message):
    with pytest.raises(ValueError, match=f"^{re.escape(message)}$"):
        _resolve(**arguments)
