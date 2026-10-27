"""Staging: calculate a household through the stable URL and read the result back.

The deploy's staging household job runs this after promotion, with
``SIMULATION_STAGING_HOUSEHOLD_TEST=1``; every other run of the deployed tests
skips it. It runs against the stable URL because the household function
reports the call's end there, so only the promoted revision records it.

The calculation needs rows the Public API owns in the same database: a
catalog version and a stored household. The test reads the staging Public
API's US catalog version for the deployed PolicyEngine.py version (or, when
the Public API has not published that version yet, its current one) and
stores a one-person household there, which returns the same household each
time. It then submits the calculation, forced so each deploy starts a new
Modal call, polls until it succeeds or fails within a bounded time, and
reads the result back.
"""

from __future__ import annotations

import os
import time
from typing import Any

import httpx
import pytest

STAGING_HOUSEHOLD_TEST_FLAG = "SIMULATION_STAGING_HOUSEHOLD_TEST"
pytestmark = pytest.mark.skipif(
    os.environ.get(STAGING_HOUSEHOLD_TEST_FLAG) != "1",
    reason="Runs only in the staging household job, after promotion.",
)

YEAR = 2026
POLL_INTERVAL_SECONDS = 5
# A household container that starts cold installs nothing but loads the US
# model, about a minute; the rest allows for a queued container and for the
# call's report to be recorded.
CALCULATION_DEADLINE_SECONDS = 600
# One adult in California, as the Public API stores it.
STORED_HOUSEHOLD = {
    "people": [
        {
            "id": "you",
            "values": {
                "age": {str(YEAR): 40},
                "employment_income": {str(YEAR): 30_000},
            },
            "memberships": {
                "household": "your household",
                "family": "your family",
                "tax_unit": "your tax unit",
                "spm_unit": "your spm unit",
                "marital_unit": "your marital unit",
            },
        }
    ],
    "household": [
        {"id": "your household", "values": {"state_name": {str(YEAR): "CA"}}}
    ],
    "family": [{"id": "your family", "values": {}}],
    "tax_unit": [{"id": "your tax unit", "values": {}}],
    "spm_unit": [{"id": "your spm unit", "values": {}}],
    "marital_unit": [{"id": "your marital unit", "values": {}}],
}
# The same household in the situation shape the household functions take,
# asking for its net income.
SITUATION = {
    "people": {
        "you": {"age": {str(YEAR): 40}, "employment_income": {str(YEAR): 30_000}}
    },
    "families": {"your family": {"members": ["you"]}},
    "tax_units": {"your tax unit": {"members": ["you"]}},
    "spm_units": {"your spm unit": {"members": ["you"]}},
    "marital_units": {"your marital unit": {"members": ["you"]}},
    "households": {
        "your household": {
            "members": ["you"],
            "state_name": {str(YEAR): "CA"},
            "household_net_income": {str(YEAR): None},
        }
    },
}


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        msg = f"{name} is required for the staging household test."
        raise RuntimeError(msg)
    return value


@pytest.fixture(scope="session")
def base_url() -> str:
    """Return the staging stable URL, without a trailing slash."""
    return _required("SIMULATION_STAGING_TEST_BASE_URL").rstrip("/")


@pytest.fixture(scope="session")
def public_api_url() -> str:
    """Return the staging Public API's URL, without a trailing slash."""
    return _required("SIMULATION_STAGING_TEST_PUBLIC_API_URL").rstrip("/")


@pytest.fixture(scope="session")
def authorization() -> dict[str, str]:
    """Return a bearer header with a client-credentials token for the service."""
    issuer = _required("SIMULATION_ENTRYPOINT_TEST_AUTH_ISSUER").rstrip("/")
    response = httpx.post(
        f"{issuer}/oauth/token",
        json={
            "client_id": _required("SIMULATION_ENTRYPOINT_TEST_AUTH_CLIENT_ID"),
            "client_secret": _required("SIMULATION_ENTRYPOINT_TEST_AUTH_CLIENT_SECRET"),
            "audience": _required("SIMULATION_ENTRYPOINT_TEST_AUTH_AUDIENCE"),
            "grant_type": "client_credentials",
        },
        timeout=30,
    )
    response.raise_for_status()
    token = response.json().get("access_token")
    if not isinstance(token, str) or not token:
        msg = "Auth0 response omitted access_token."
        raise RuntimeError(msg)
    return {"Authorization": f"Bearer {token}"}


def catalog_version(public_api_url: str, policyengine_version: str) -> dict[str, str]:
    """Return the US catalog version for the deployed version, or the current one."""
    url = f"{public_api_url}/v2/tax-benefit-models/by-country/us"
    response = httpx.get(
        url, params={"policyengine_version": policyengine_version}, timeout=30
    )
    if response.status_code == httpx.codes.NOT_FOUND:
        response = httpx.get(url, timeout=30)
    response.raise_for_status()
    result = response.json()["result"]
    return {
        "catalog_version_id": result["model_version"]["id"],
        "policyengine_version": result["policyengine_version"],
    }


def stored_household_id(public_api_url: str) -> str:
    """Store the household in the Public API, or find it, and return its ID."""
    response = httpx.post(
        f"{public_api_url}/v2/households",
        params={"country_id": "us"},
        json={
            "country_id": "us",
            "default_year": YEAR,
            "household_data": STORED_HOUSEHOLD,
        },
        timeout=30,
    )
    assert response.status_code in {httpx.codes.OK, httpx.codes.CREATED}, response.text
    return response.json()["result"]["item"]["id"]


def poll_until_finished(
    base_url: str, calculation_id: str, authorization: dict[str, str]
) -> dict[str, Any]:
    """Poll the calculation until it succeeds or fails, within the deadline."""
    deadline = time.monotonic() + CALCULATION_DEADLINE_SECONDS
    while True:
        response = httpx.get(
            f"{base_url}/v2/household-calculations/{calculation_id}",
            headers=authorization,
            timeout=30,
        )
        assert response.status_code == httpx.codes.OK, response.text
        calculation = response.json()
        if calculation["status"] in {"succeeded", "failed"}:
            return calculation
        assert time.monotonic() < deadline, (
            f"calculation {calculation_id} still {calculation['status']} after "
            f"{CALCULATION_DEADLINE_SECONDS} seconds"
        )
        time.sleep(POLL_INTERVAL_SECONDS)


def test_a_household_calculation_succeeds_and_reads_back(
    base_url: str, public_api_url: str, authorization: dict[str, str]
) -> None:
    catalog = catalog_version(
        public_api_url, _required("SIMULATION_STAGING_TEST_POLICYENGINE_VERSION")
    )
    submitted = httpx.post(
        f"{base_url}/v2/household-calculations",
        json={
            "country": "us",
            **catalog,
            "year": YEAR,
            "household_id": stored_household_id(public_api_url),
            "household": SITUATION,
            "force": True,
        },
        headers=authorization,
        timeout=60,
    )
    assert submitted.status_code == httpx.codes.ACCEPTED, submitted.text
    calculation_id = submitted.json()["calculation_id"]

    finished = poll_until_finished(base_url, calculation_id, authorization)

    assert finished["status"] == "succeeded", finished["error"]
    result = finished["result"]
    household = result["household"]["households"]["your household"]
    net_income = household["household_net_income"][str(YEAR)]
    assert isinstance(net_income, int | float)
    assert net_income > 0
    assert result["bundle"]["policyengine_version"] == catalog["policyengine_version"]
    read_back = httpx.get(
        f"{base_url}/v2/household-calculations/{calculation_id}",
        headers=authorization,
        timeout=30,
    )
    assert read_back.json() == finished
