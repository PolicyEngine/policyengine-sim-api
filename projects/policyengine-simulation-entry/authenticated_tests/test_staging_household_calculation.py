"""Staging: calculate a household through the stable URL and read the result back.

The deploy's staging household job runs this after promotion, with
``SIMULATION_STAGING_HOUSEHOLD_TEST=1``; every other run of the deployed tests
skips it. It runs against the stable URL because the household function
reports the call's end there, so only the promoted revision records it.

The calculation needs rows the Public API owns in the same database, which
sim reads itself: a catalog version and a stored household. The test finds
the staging Public API's US catalog version for the deployed PolicyEngine.py
version (or, when the Public API has not published that version yet, its
current one) and stores a one-person household there, which returns the
same household each time. It then submits that household's ID and the
catalog version's PolicyEngine.py version, forced so each deploy starts a
new Modal call, polls until it succeeds or fails, and reads the result
back, all within one ten-minute limit. An answer asking to retry (503
with ``Retry-After``) is retried after its wait, within that limit; a
submission retried after it was partly written may leave a pending run
behind, which nothing reuses, since each retry is forced too.
"""

from __future__ import annotations

import os
import time
from typing import TYPE_CHECKING, Any

import httpx
import pytest

if TYPE_CHECKING:
    from collections.abc import Callable

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
# One adult in California, as the Public API stores it, asking for the
# household's net income.
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
        {
            "id": "your household",
            "values": {
                "state_name": {str(YEAR): "CA"},
                "household_net_income": {str(YEAR): None},
            },
        }
    ],
    "family": [{"id": "your family", "values": {}}],
    "tax_unit": [{"id": "your tax unit", "values": {}}],
    "spm_unit": [{"id": "your spm unit", "values": {}}],
    "marital_unit": [{"id": "your marital unit", "values": {}}],
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


def catalog_policyengine_version(public_api_url: str, policyengine_version: str) -> str:
    """Return the deployed version if the Public API publishes it, else its current."""
    url = f"{public_api_url}/v2/tax-benefit-models/by-country/us"
    response = httpx.get(
        url, params={"policyengine_version": policyengine_version}, timeout=30
    )
    if response.status_code == httpx.codes.NOT_FOUND:
        response = httpx.get(url, timeout=30)
    response.raise_for_status()
    return response.json()["result"]["policyengine_version"]


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


def submission_body(household_id: str, policyengine_version: str) -> dict[str, Any]:
    """Return a forced calculation of the stored household, by ID only."""
    return {
        "country": "us",
        "policyengine_version": policyengine_version,
        "household_id": household_id,
        "force": True,
    }


def answer_within(
    send: Callable[[], httpx.Response], deadline: float
) -> httpx.Response:
    """Send until the service answers other than 503, or the deadline passes.

    A 503 asks to retry after its ``Retry-After`` wait; the last answer is
    returned when the deadline would pass first.
    """
    while True:
        response = send()
        if response.status_code != httpx.codes.SERVICE_UNAVAILABLE:
            return response
        wait = float(response.headers.get("retry-after", POLL_INTERVAL_SECONDS))
        if time.monotonic() + wait >= deadline:
            return response
        time.sleep(wait)


def poll_until_finished(
    base_url: str,
    calculation_id: str,
    authorization: dict[str, str],
    *,
    deadline: float,
) -> dict[str, Any]:
    """Poll the calculation until it succeeds or fails, before ``deadline``."""
    while True:
        response = answer_within(
            lambda: httpx.get(
                f"{base_url}/v2/household-calculations/{calculation_id}",
                headers=authorization,
                timeout=30,
            ),
            deadline,
        )
        assert response.status_code == httpx.codes.OK, response.text
        calculation = response.json()
        if calculation["status"] in {"succeeded", "failed"}:
            return calculation
        assert time.monotonic() < deadline, (
            f"calculation {calculation_id} still {calculation['status']} at the "
            f"{CALCULATION_DEADLINE_SECONDS}-second limit"
        )
        time.sleep(POLL_INTERVAL_SECONDS)


def test_a_household_calculation_succeeds_and_reads_back(
    base_url: str, public_api_url: str, authorization: dict[str, str]
) -> None:
    deadline = time.monotonic() + CALCULATION_DEADLINE_SECONDS
    policyengine_version = catalog_policyengine_version(
        public_api_url, _required("SIMULATION_STAGING_TEST_POLICYENGINE_VERSION")
    )
    household_id = stored_household_id(public_api_url)
    submitted = answer_within(
        lambda: httpx.post(
            f"{base_url}/v2/household-calculations",
            json=submission_body(household_id, policyengine_version),
            headers=authorization,
            timeout=60,
        ),
        deadline,
    )
    assert submitted.status_code == httpx.codes.ACCEPTED, submitted.text
    calculation_id = submitted.json()["calculation_id"]

    finished = poll_until_finished(
        base_url, calculation_id, authorization, deadline=deadline
    )

    assert finished["status"] == "succeeded", finished["error"]
    assert finished["year"] == YEAR
    result = finished["result"]
    household = result["household"]["households"]["your household"]
    net_income = household["household_net_income"][str(YEAR)]
    assert isinstance(net_income, int | float)
    assert net_income > 0
    assert result["bundle"]["policyengine_version"] == policyengine_version
    read_back = httpx.get(
        f"{base_url}/v2/household-calculations/{calculation_id}",
        headers=authorization,
        timeout=30,
    )
    assert read_back.json() == finished
