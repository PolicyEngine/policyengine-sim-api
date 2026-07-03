import json
import time
from http import HTTPStatus

import httpx
import pytest
from pydantic_settings import BaseSettings, SettingsConfigDict

from policyengine_api_simulation_client import AuthenticatedClient, Client
from policyengine_api_simulation_client.api.default import (
    get_budget_window_job_status_budget_window_jobs_batch_job_id_get,
    submit_budget_window_batch_simulate_economy_budget_window_post,
)
from policyengine_api_simulation_client.models import (
    BudgetWindowBatchRequest,
    BudgetWindowBatchStatusResponse,
)


BUDGET_WINDOW_YEARS = ["2026", "2027"]
BUDGET_WINDOW_REFORM = {
    "gov.irs.credits.ctc.refundable.fully_refundable": {"2023-01-01.2100-12-31": True}
}
BUDGET_WINDOW_REGION = "state/ut"
BUDGET_WINDOW_MAX_PARALLEL = 2


class Settings(BaseSettings):
    base_url: str = "http://localhost:8082"
    access_token: str | None = None
    gateway_auth_required: bool = False
    timeout_in_millis: int = 1_500_000  # 25 minutes for full simulations
    poll_interval_seconds: float = 5.0
    us_model_version: str = "1.690.7"
    uk_model_version: str = "2.88.14"

    model_config = SettingsConfigDict(
        env_prefix="simulation_integ_test_",
        env_ignore_empty=True,
    )


settings = Settings()


@pytest.fixture()
def client() -> Client | AuthenticatedClient:
    """Create HTTP client for simulation API."""
    timeout = httpx.Timeout(timeout=settings.timeout_in_millis / 1000)
    if settings.access_token:
        return AuthenticatedClient(
            base_url=settings.base_url, token=settings.access_token, timeout=timeout
        )
    return Client(base_url=settings.base_url, timeout=timeout)


@pytest.fixture()
def us_model_version() -> str:
    """Return the US model version for testing specific version scenarios."""
    return settings.us_model_version


@pytest.fixture()
def uk_model_version() -> str:
    """Return the UK model version for testing specific version scenarios."""
    return settings.uk_model_version


@pytest.fixture()
def poll_interval() -> float:
    """Return poll interval in seconds."""
    return settings.poll_interval_seconds


@pytest.fixture()
def max_wait_seconds() -> float:
    """Return max wait time in seconds."""
    return settings.timeout_in_millis / 1000


def _decode_response_content(content: bytes) -> str:
    try:
        return json.dumps(json.loads(content), sort_keys=True)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return content.decode("utf-8", errors="replace")


def _poll_budget_window_batch(
    *,
    client: Client | AuthenticatedClient,
    batch_job_id: str,
    max_wait_seconds: float,
    poll_interval: float,
) -> BudgetWindowBatchStatusResponse:
    deadline = time.monotonic() + max_wait_seconds
    last_status_code: HTTPStatus | None = None
    last_content = b""

    while time.monotonic() < deadline:
        response = get_budget_window_job_status_budget_window_jobs_batch_job_id_get.sync_detailed(
            batch_job_id=batch_job_id, client=client
        )
        last_status_code = response.status_code
        last_content = response.content

        if response.status_code == HTTPStatus.ACCEPTED:
            time.sleep(poll_interval)
            continue

        if response.status_code == HTTPStatus.OK:
            assert isinstance(response.parsed, BudgetWindowBatchStatusResponse), (
                f"Unexpected response type: {type(response.parsed)}"
            )
            assert response.parsed.status == "complete", (
                f"Unexpected budget-window status: {response.parsed}"
            )
            return response.parsed

        if response.status_code == HTTPStatus.INTERNAL_SERVER_ERROR:
            raise AssertionError(
                "Budget-window batch failed: "
                f"{_decode_response_content(response.content)}"
            )

        raise AssertionError(
            "Unexpected budget-window poll status "
            f"{response.status_code}: {_decode_response_content(response.content)}"
        )

    raise TimeoutError(
        f"Budget-window batch {batch_job_id} did not complete within "
        f"{max_wait_seconds}s; last response was "
        f"{last_status_code}: {_decode_response_content(last_content)}"
    )


@pytest.fixture()
def budget_window_years() -> list[str]:
    """Return the annual rows expected from the staging budget-window smoke run."""
    return list(BUDGET_WINDOW_YEARS)


@pytest.fixture()
def budget_window_request(us_model_version: str) -> BudgetWindowBatchRequest:
    """Build the standard staging budget-window smoke request."""
    return BudgetWindowBatchRequest.from_dict(
        {
            "country": "us",
            "version": us_model_version,
            "region": BUDGET_WINDOW_REGION,
            "scope": "macro",
            "reform": BUDGET_WINDOW_REFORM,
            "start_year": BUDGET_WINDOW_YEARS[0],
            "window_size": len(BUDGET_WINDOW_YEARS),
            "max_parallel": BUDGET_WINDOW_MAX_PARALLEL,
        }
    )


@pytest.fixture()
def decode_response_content():
    """Return a compact formatter for non-OK HTTP response payloads."""
    return _decode_response_content


@pytest.fixture()
def submit_budget_window_batch(client: Client | AuthenticatedClient):
    """Submit a budget-window batch through the generated client."""

    def submit(request: BudgetWindowBatchRequest):
        return submit_budget_window_batch_simulate_economy_budget_window_post.sync_detailed(
            client=client,
            body=request,
        )

    return submit


@pytest.fixture()
def poll_budget_window_batch(
    client: Client | AuthenticatedClient,
    max_wait_seconds: float,
    poll_interval: float,
):
    """Poll a budget-window batch through the generated client."""

    def poll(batch_job_id: str) -> BudgetWindowBatchStatusResponse:
        return _poll_budget_window_batch(
            client=client,
            batch_job_id=batch_job_id,
            max_wait_seconds=max_wait_seconds,
            poll_interval=poll_interval,
        )

    return poll
