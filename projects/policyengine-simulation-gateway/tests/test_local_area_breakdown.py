"""The gateway sends breakdown=local_areas only to workers that declare it.

Worker apps built before local-area breakdowns existed ignore the field and
would return a national result, so the gateway refuses a breakdown for any
route whose bundle manifest does not declare the capability.
"""

import pytest
from fastapi.testclient import TestClient
from policyengine_simulation_contract.local_area_breakdown import (
    LOCAL_AREA_BREAKDOWN_CAPABILITY_KEY,
    LOCAL_AREA_BREAKDOWN_CAPABILITY_VERSION,
)

ROUTING_STATE_DICT_NAME = "simulation-api-routing-state"
BREAKDOWN_REQUEST = {
    "country": "uk",
    "scope": "macro",
    "region": "uk",
    "time_period": "2026",
    "reform": {},
    "include_cliffs": False,
    "breakdown": "local_areas",
}


@pytest.fixture
def breakdown_worker(mock_modal):
    """Declare the capability on the routing state's latest bundle only."""
    bundles = mock_modal["dicts"][ROUTING_STATE_DICT_NAME]["active"]["bundles"]
    bundles["4.10.0"] = {
        **bundles["4.10.0"],
        LOCAL_AREA_BREAKDOWN_CAPABILITY_KEY: LOCAL_AREA_BREAKDOWN_CAPABILITY_VERSION,
    }
    return mock_modal


def test_a_declaring_worker_gets_the_breakdown(breakdown_worker, client: TestClient):
    response = client.post("/simulate/economy/comparison", json=BREAKDOWN_REQUEST)

    assert response.status_code == 200
    assert breakdown_worker["func"].last_from_name_call == (
        "policyengine-simulation-py4-10-0",
        "run_simulation",
    )
    assert breakdown_worker["func"].last_payload["breakdown"] == "local_areas"


def test_a_worker_without_the_capability_is_refused_before_spawning(
    mock_modal, client: TestClient
):
    response = client.post("/simulate/economy/comparison", json=BREAKDOWN_REQUEST)

    assert response.status_code == 400
    detail = response.json()["detail"]
    assert "local_areas" in detail
    assert "policyengine.py 4.10.0" in detail
    assert mock_modal["func"].last_payload is None


def test_an_older_pinned_worker_is_refused_before_spawning(
    breakdown_worker, client: TestClient
):
    response = client.post(
        "/simulate/economy/comparison",
        json={**BREAKDOWN_REQUEST, "policyengine_version": "3.9.0"},
    )

    assert response.status_code == 400
    assert "policyengine.py 3.9.0" in response.json()["detail"]
    assert breakdown_worker["func"].last_payload is None


def test_a_worker_pinned_by_uk_model_version_without_it_is_refused(
    breakdown_worker, client: TestClient
):
    # An older app serves UK model 2.60.0 and declares no breakdowns.
    state = breakdown_worker["dicts"][ROUTING_STATE_DICT_NAME]["active"]
    state["routes"]["uk"]["2.60.0"] = "policyengine-simulation-py3-9-0"
    older = state["bundles"]["3.9.0"]
    state["bundles"]["3.9.0"] = {
        **older,
        "uk": {**older["uk"], "model_version": "2.60.0"},
    }
    pinned = {**BREAKDOWN_REQUEST, "version": "2.60.0"}
    national = {key: value for key, value in pinned.items() if key != "breakdown"}

    refused = client.post("/simulate/economy/comparison", json=pinned)

    assert refused.status_code == 400
    assert "policyengine.py 3.9.0" in refused.json()["detail"]
    assert breakdown_worker["func"].last_payload is None
    # The same pinned route still serves a national request.
    assert client.post("/simulate/economy/comparison", json=national).status_code == (
        200
    )
    assert breakdown_worker["func"].last_from_name_call == (
        "policyengine-simulation-py3-9-0",
        "run_simulation",
    )


def test_legacy_only_routes_are_refused_before_spawning(mock_modal, client: TestClient):
    # Without a routing state no bundle manifest can declare the capability.
    del mock_modal["dicts"][ROUTING_STATE_DICT_NAME]
    mock_modal["dicts"]["simulation-api-uk-versions"] = {
        "latest": "2.66.0",
        "2.66.0": "policyengine-simulation-py4-10-0",
    }

    response = client.post("/simulate/economy/comparison", json=BREAKDOWN_REQUEST)

    assert response.status_code == 400
    assert "policyengine.py 4.10.0" in response.json()["detail"]
    assert mock_modal["func"].last_payload is None


def test_a_national_request_needs_no_capability(mock_modal, client: TestClient):
    national = {
        key: value for key, value in BREAKDOWN_REQUEST.items() if key != "breakdown"
    }

    response = client.post("/simulate/economy/comparison", json=national)

    assert response.status_code == 200
    assert "breakdown" not in mock_modal["func"].last_payload
