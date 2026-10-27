"""A small v2 economy report on staging, submitted and read back once compared.

The staging deployment runs this after promoting the revision it tested, so
it reaches that revision through the stable URL: it submits a report on
Wales, current law compared with itself, runs it again with fresh runs so
this deployment's Modal functions calculate it (about a minute or two of
Modal time), waits until the run's comparison is recorded, and reads the
result back. It needs the staging simulation database, sim's Modal token,
and a staging catalog version for the deployed PolicyEngine.py version.

The deployed-auth runs collect this directory too; the test runs only where
``SIMULATION_STAGING_ECONOMY_CHECK`` is 1.
"""

from __future__ import annotations

import os
import time

import httpx
import pytest
from policyengine_simulation_contract.macro_output import SingleYearMacroOutput

COUNTRY = "uk"
REGION = "country/wales"
YEAR = 2026
POLL_INTERVAL_SECONDS = 10
# Both Modal calls, each from a cold start, with a margin.
FINISH_WITHIN_SECONDS = 20 * 60
FINISHED = {"succeeded", "failed"}

pytestmark = pytest.mark.skipif(
    os.environ.get("SIMULATION_STAGING_ECONOMY_CHECK") != "1",
    reason="runs only in the staging economy check",
)


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        msg = f"{name} is required for the staging economy check."
        raise RuntimeError(msg)
    return value


@pytest.fixture(scope="module")
def base_url() -> str:
    """Return the staging stable URL, without a trailing slash."""
    return _required("SIMULATION_ENTRYPOINT_TEST_BASE_URL").rstrip("/")


@pytest.fixture(scope="module")
def headers() -> dict[str, str]:
    """Return the test client's bearer token for the entrypoint's audience."""
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


def _wait_for_run(base_url: str, headers: dict[str, str], report_id: str, run_id: str):
    deadline = time.monotonic() + FINISH_WITHIN_SECONDS
    while True:
        response = httpx.get(
            f"{base_url}/v2/economy-reports/{report_id}", headers=headers, timeout=60
        )
        response.raise_for_status()
        report = response.json()
        run = report["run"]
        if run is not None and run["id"] == run_id and run["status"] in FINISHED:
            return report
        if time.monotonic() > deadline:
            pytest.fail(
                f"report run {run_id} did not finish within "
                f"{FINISH_WITHIN_SECONDS} s; it is {run and run['status']}"
            )
        time.sleep(POLL_INTERVAL_SECONDS)


def test_a_small_economy_report_runs_and_reads_back(
    base_url: str, headers: dict[str, str]
) -> None:
    """Submit, run again with fresh runs, wait, and read the result back."""
    policyengine_version = _required("SIMULATION_STAGING_ECONOMY_POLICYENGINE_VERSION")
    submitted = httpx.post(
        f"{base_url}/v2/economy-reports",
        json={
            "country": COUNTRY,
            "policyengine_version": policyengine_version,
            "year": YEAR,
            "region_code": REGION,
            "baseline": {},
            "reform": {},
        },
        headers=headers,
        timeout=120,
    )
    assert submitted.status_code == 201, submitted.text
    report_id = submitted.json()["id"]

    # The submission can reuse an earlier deployment's run; a forced run is
    # calculated by this deployment's functions.
    rerun = httpx.post(
        f"{base_url}/v2/economy-reports/{report_id}/runs",
        json={
            "policyengine_version": policyengine_version,
            "trigger": "manual",
            "force": True,
        },
        headers=headers,
        timeout=120,
    )
    assert rerun.status_code == 201, rerun.text

    report = _wait_for_run(base_url, headers, report_id, rerun.json()["id"])

    assert report["status"] == "succeeded", report["run"]["error"]
    result = SingleYearMacroOutput.model_validate(report["result"])
    # Current law compared with itself changes nothing.
    assert result.budget.budgetary_impact == 0
    assert result.budget.baseline_net_income > 0
