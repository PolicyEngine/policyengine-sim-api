"""Qualify v2 budget windows on staging against the old gateway's: an operator step.

An operator runs this once against staging after the v2 budget-window routes
are deployed there; no workflow runs it. It needs a reform saved in the
staging Public API database, given twice: its policy ID for the v2 report,
which reads the policy itself, and its parameter values for the old gateway,
which takes them in the request. Both windows cost Modal time for every year,
which is why this is not run on every staging deployment.

It checks:

- **totals:** the same reform, region and years submitted to the v2 route
  (``/v2/budget-window-reports``) and to the old gateway's
  (``/simulate/economy/budget-window``) give each year's budget figures and
  the totals within the tolerance;
- **recovery of queued years:** the window has more years than sim
  calculates at once, so its last years start only when earlier years'
  calls report their ends or the safety check passes, without the submitter;
- **recovery of results:** running the report again unforced, so that its
  years reuse the simulation runs that exist, reads back the same figures.

Run it from ``projects/policyengine-simulation-entry`` with
``SIMULATION_STAGING_BUDGET_WINDOW_QUALIFICATION=1`` and:

- ``SIMULATION_ENTRYPOINT_TEST_BASE_URL``: the staging stable URL;
- ``SIMULATION_ENTRYPOINT_TEST_AUTH_ISSUER``, ``_AUDIENCE``, ``_CLIENT_ID``
  and ``_CLIENT_SECRET``: the test client's, as the staging checks use;
- ``SIMULATION_QUALIFICATION_POLICYENGINE_VERSION``: the deployed
  PolicyEngine.py version, which a staging catalog version must exist for;
- ``SIMULATION_QUALIFICATION_MODEL_VERSION``: the country package version
  of that release, which the old gateway routes by;
- ``SIMULATION_QUALIFICATION_REFORM_POLICY_ID`` and
  ``SIMULATION_QUALIFICATION_REFORM``: the reform's v2 policy ID and its
  parameter values as JSON (``{"gov....": {"2026-01-01.2100-12-31": 1}}``);
- optionally ``SIMULATION_QUALIFICATION_COUNTRY`` (``us``),
  ``_REGION`` (``state/ut``), ``_START_YEAR`` (2026), ``_WINDOW_SIZE`` (7),
  ``_RELATIVE_TOLERANCE`` (0.001), ``_ABSOLUTE_TOLERANCE`` (1000, in the
  country's currency) and ``_TIMEOUT_MINUTES`` (120).

**Recovery after the caller stops** is an operator action beside it: while
the v2 window runs (its run reads with queued years), promote a new
entrypoint revision, or roll back to the previous one and forward again,
with the deploy and rollback workflows. The window must still finish within
the timeout: its later years are then started by the new revision's call
reports and by the safety check, not by the instance that took the
submission.

For example:

    uv run --frozen pytest \
        authenticated_tests/test_staging_budget_window_qualification.py -v

The deployed-auth runs collect this directory too; the test runs only where
``SIMULATION_STAGING_BUDGET_WINDOW_QUALIFICATION`` is 1.
"""

from __future__ import annotations

import json
import math
import os
import time
from typing import TYPE_CHECKING, Any

import httpx
import pytest
from policyengine_simulation_contract.economy_runs import (
    BUDGET_WINDOW_MAX_CONCURRENT_YEARS,
)

POLL_INTERVAL_SECONDS = 30
FINISHED = {"succeeded", "failed"}
OLD_GATEWAY_FINISHED = {"complete", "failed"}
# Sim calculates at most this many years of a window at once; a longer
# window needs recovery of its queued years.
MAX_CONCURRENT_YEARS = BUDGET_WINDOW_MAX_CONCURRENT_YEARS
BUDGET_FIELDS = (
    "taxRevenueImpact",
    "federalTaxRevenueImpact",
    "stateTaxRevenueImpact",
    "benefitSpendingImpact",
    "budgetaryImpact",
)

if TYPE_CHECKING:
    from collections.abc import Callable

pytestmark = pytest.mark.skipif(
    os.environ.get("SIMULATION_STAGING_BUDGET_WINDOW_QUALIFICATION") != "1",
    reason="an operator step: runs only when it is asked for",
)


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        msg = f"{name} is required for the budget-window qualification."
        raise RuntimeError(msg)
    return value


def _optional(name: str, default: str) -> str:
    return os.environ.get(name, "").strip() or default


def old_gateway_status(response: httpx.Response) -> dict[str, Any]:
    """Return an old-gateway batch's status from its status route's answer.

    The route answers 500 with the batch's status and error when the batch
    failed, or 400 when the failure carries the caller's errors; any other
    error answer is raised.
    """
    if response.status_code in {
        httpx.codes.BAD_REQUEST,
        httpx.codes.INTERNAL_SERVER_ERROR,
    }:
        try:
            body = response.json()
        except ValueError:
            body = None
        if isinstance(body, dict) and body.get("status") == "failed":
            return body
    response.raise_for_status()
    return response.json()


def figures_differ(
    v2_value: float, old_value: float, *, relative: float, absolute: float
) -> bool:
    """Whether two budget figures differ by more than both tolerances allow."""
    return not math.isclose(v2_value, old_value, rel_tol=relative, abs_tol=absolute)


def window_differences(
    v2_result: dict[str, Any],
    old_result: dict[str, Any],
    *,
    relative: float,
    absolute: float,
) -> list[str]:
    """Return each year's and each total's figure that differs between the paths.

    Both results are in the budget-window format (``BudgetWindowResult``);
    a year one path has and the other lacks is a difference too.
    """
    v2_years = {impact["year"]: impact for impact in v2_result["annualImpacts"]}
    old_years = {impact["year"]: impact for impact in old_result["annualImpacts"]}
    differences = [
        f"{year}: only one path has it"
        for year in sorted(set(v2_years) ^ set(old_years))
    ]
    rows = [
        *(
            (year, v2_years[year], old_years[year])
            for year in sorted(v2_years)
            if year in old_years
        ),
        ("Total", v2_result["totals"], old_result["totals"]),
    ]
    differences.extend(
        f"{year} {field}: v2 {v2_row[field]} against the old gateway's {old_row[field]}"
        for year, v2_row, old_row in rows
        for field in BUDGET_FIELDS
        if figures_differ(
            v2_row[field], old_row[field], relative=relative, absolute=absolute
        )
    )
    return differences


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


def _wait(
    read: Callable[[], dict[str, Any]], finished_statuses: set[str], deadline: float
) -> dict[str, Any]:
    while True:
        body = read()
        if body["status"] in finished_statuses:
            return body
        if time.monotonic() > deadline:
            pytest.fail(f"still {body['status']} at the deadline")
        time.sleep(POLL_INTERVAL_SECONDS)


def test_v2_budget_windows_match_the_old_gateways_and_recover(
    base_url: str, headers: dict[str, str]
) -> None:
    """Submit both windows, compare them, and run the v2 one again."""
    country = _optional("SIMULATION_QUALIFICATION_COUNTRY", "us")
    region = _optional("SIMULATION_QUALIFICATION_REGION", "state/ut")
    start_year = int(_optional("SIMULATION_QUALIFICATION_START_YEAR", "2026"))
    window_size = int(_optional("SIMULATION_QUALIFICATION_WINDOW_SIZE", "7"))
    relative = float(_optional("SIMULATION_QUALIFICATION_RELATIVE_TOLERANCE", "0.001"))
    absolute = float(_optional("SIMULATION_QUALIFICATION_ABSOLUTE_TOLERANCE", "1000"))
    deadline = time.monotonic() + 60 * float(
        _optional("SIMULATION_QUALIFICATION_TIMEOUT_MINUTES", "120")
    )
    policyengine_version = _required("SIMULATION_QUALIFICATION_POLICYENGINE_VERSION")
    assert window_size > MAX_CONCURRENT_YEARS, (
        "the window must have queued years to qualify their recovery"
    )

    submitted = httpx.post(
        f"{base_url}/v2/budget-window-reports",
        json={
            "country": country,
            "policyengine_version": policyengine_version,
            "start_year": start_year,
            "window_size": window_size,
            "region_code": region,
            "baseline": {},
            "reform": {
                "policy_id": _required("SIMULATION_QUALIFICATION_REFORM_POLICY_ID")
            },
        },
        headers=headers,
        timeout=300,
    )
    assert submitted.status_code == 201, submitted.text
    report_id = submitted.json()["id"]
    old_submitted = httpx.post(
        f"{base_url}/simulate/economy/budget-window",
        json={
            "country": country,
            "version": _required("SIMULATION_QUALIFICATION_MODEL_VERSION"),
            "region": region,
            "scope": "macro",
            "reform": json.loads(_required("SIMULATION_QUALIFICATION_REFORM")),
            "start_year": str(start_year),
            "window_size": window_size,
        },
        headers=headers,
        timeout=300,
    )
    assert old_submitted.status_code == 200, old_submitted.text
    batch_job_id = old_submitted.json()["batch_job_id"]

    def read_v2() -> dict[str, Any]:
        response = httpx.get(
            f"{base_url}/v2/budget-window-reports/{report_id}",
            headers=headers,
            timeout=120,
        )
        response.raise_for_status()
        return response.json()

    def read_old() -> dict[str, Any]:
        return old_gateway_status(
            httpx.get(
                f"{base_url}/budget-window-jobs/{batch_job_id}",
                headers=headers,
                timeout=120,
            )
        )

    v2_report = _wait(read_v2, FINISHED, deadline)
    old_status = _wait(read_old, OLD_GATEWAY_FINISHED, deadline)

    assert v2_report["status"] == "succeeded", v2_report["run"]["error"]
    assert old_status["status"] == "complete", old_status.get("error")
    assert len(v2_report["run"]["years"]) == window_size
    assert (
        window_differences(
            v2_report["result"],
            old_status["result"],
            relative=relative,
            absolute=absolute,
        )
        == []
    )

    rerun = httpx.post(
        f"{base_url}/v2/budget-window-reports/{report_id}/runs",
        json={"policyengine_version": policyengine_version, "trigger": "manual"},
        headers=headers,
        timeout=300,
    )
    assert rerun.status_code == 201, rerun.text
    rerun_report = _wait(read_v2, FINISHED, deadline)
    assert rerun_report["run"]["id"] == rerun.json()["id"]
    assert rerun_report["status"] == "succeeded", rerun_report["run"]["error"]
    assert rerun_report["result"] == v2_report["result"]
