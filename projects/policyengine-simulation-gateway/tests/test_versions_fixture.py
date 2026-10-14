"""The /versions fixture the deploy smoke tests use is the gateway's real output.

Differential: projects/policyengine-simulation-entry/tests/fixtures/
versions_body.json must equal what this gateway returns for a routing state
shaped like update_version_registry.build_next_routing_state's output after
two releases. Regenerate with REGENERATE_VERSIONS_FIXTURE=1.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

FIXTURE = (
    Path(__file__).resolve().parents[2]
    / "policyengine-simulation-entry"
    / "tests"
    / "fixtures"
    / "versions_body.json"
)


def _country(country: str, model_version: str, sha: str | None) -> dict:
    data = {
        "us": ("policyengine-us-data", "1.80.0", "enhanced_cps_2024"),
        "uk": ("policyengine-uk-data", "1.58.0", "enhanced_frs_2024_25"),
    }[country]
    repo = f"policyengine/{data[0]}{'-private' if country == 'uk' else ''}"
    return {
        "country": country,
        "model_package_name": f"policyengine-{country}",
        "model_version": model_version,
        "data_package_name": data[0],
        "data_package_version": data[1],
        "data_version": data[1],
        "data_artifact_revision": data[1],
        "default_dataset": data[2],
        "default_dataset_uri": f"hf://{repo}/{data[2]}.h5@{data[1]}",
        "dataset_uris": {},
        "dataset_repo_types": {},
        "default_dataset_sha256": sha,
    }


def _app(version: str) -> str:
    return "policyengine-simulation-py" + version.replace(".", "-")


def routing_state() -> dict:
    """Two releases, published in order; 6.2.7 is latest."""

    releases = {
        "6.2.6": ("1.700.0", "2.125.1", "5" * 64),
        "6.2.7": ("1.700.0", "2.126.8", "a" * 64),
    }
    state = {
        "schema_version": 1,
        "generation": f"6.2.7:{_app('6.2.7')}",
        "latest": {"policyengine": "6.2.7", "us": "1.700.0", "uk": "2.126.8"},
        "routes": {"policyengine": {}, "us": {}, "uk": {}},
        "bundles": {},
    }
    for version, (us, uk, sha) in releases.items():
        state["routes"]["policyengine"][version] = _app(version)
        state["routes"]["us"][us] = _app(version)
        state["routes"]["uk"][uk] = _app(version)
        state["bundles"][version] = {
            "app_name": _app(version),
            "policyengine_version": version,
            "us": _country("us", us, None),
            "uk": _country("uk", uk, sha),
        }
    return state


def test_smoke_fixture_is_the_gateway_versions_response(mock_modal, client):
    mock_modal["dicts"]["simulation-api-routing-state"]["active"] = routing_state()
    response = client.get("/versions")
    assert response.status_code == 200
    body = response.json()
    # The shape the smoke script relies on: latest holds versions.
    assert body["policyengine"]["latest"] == "6.2.7"
    assert body["policyengine"]["6.2.7"] == _app("6.2.7")
    assert body["uk"]["latest"] == "2.126.8"
    if os.environ.get("REGENERATE_VERSIONS_FIXTURE"):
        FIXTURE.write_text(json.dumps(body, indent=2, sort_keys=True) + "\n")
    assert json.loads(FIXTURE.read_text()) == body


def test_versions_survives_an_unresolvable_historical_dataset(mock_modal, client):
    """Review M1: one bad entry is reported without a dataset, not a 500."""

    state = routing_state()
    state["bundles"]["6.2.6"]["uk"]["default_dataset_uri"] = (
        "gs://policyengine-uk-data-private/enhanced_frs_2024_25.h5@1.39.0"
    )
    state["bundles"]["6.2.6"]["uk"]["data_package_version"] = "1.40.0"
    mock_modal["dicts"]["simulation-api-routing-state"]["active"] = state
    response = client.get("/versions")
    assert response.status_code == 200
    bundles = response.json()["bundles"]
    assert bundles["6.2.7"]["uk"]["dataset_sha256"] == "a" * 64
    # The bad entry keeps its model identity but never names the bad dataset.
    historical = bundles["6.2.6"]["uk"]
    assert historical["model_version"] == "2.125.1"
    assert historical.get("dataset") is None
    assert "gs://" not in json.dumps(historical)


@pytest.mark.parametrize("model_version", ["missing", "", "   ", None, 2.1])
def test_versions_skips_country_entries_without_a_model_version(
    mock_modal, client, model_version
):
    """#743 review: a blank or non-string model is no identity at all."""

    state = routing_state()
    if model_version == "missing":
        del state["bundles"]["6.2.6"]["uk"]["model_version"]
    else:
        state["bundles"]["6.2.6"]["uk"]["model_version"] = model_version
    mock_modal["dicts"]["simulation-api-routing-state"]["active"] = state
    bundles = client.get("/versions").json()["bundles"]
    assert "uk" not in bundles["6.2.6"]
    assert bundles["6.2.6"]["us"]["model_version"] == "1.700.0"


def test_versions_reports_the_manifest_wrapper_version(mock_modal, client):
    state = routing_state()
    state["bundles"]["6.2.6"]["policyengine_version"] = "6.2.6.post1"
    mock_modal["dicts"]["simulation-api-routing-state"]["active"] = state
    bundles = client.get("/versions").json()["bundles"]
    assert bundles["6.2.6"]["uk"]["policyengine_version"] == "6.2.6.post1"
