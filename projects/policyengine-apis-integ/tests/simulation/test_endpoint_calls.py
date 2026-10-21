"""endpoint_calls sends what the generated sync_detailed sends.

Each request is captured by an httpx mock transport. With an
AuthenticatedClient, the endpoint_calls function and the endpoint's own
sync_detailed must send the same request. With a plain Client, the request is
the same except that it carries no Authorization header.
"""

from collections.abc import Callable

import httpx
import pytest
from policyengine_api_simulation_client import AuthenticatedClient, Client
from policyengine_api_simulation_client.api.default import (
    get_budget_window_job_status_budget_window_jobs_batch_job_id_get,
    get_job_status_jobs_job_id_get,
    submit_budget_window_batch_simulate_economy_budget_window_post,
    submit_simulation_simulate_economy_comparison_post,
)
from policyengine_api_simulation_client.models import (
    BudgetWindowBatchRequest,
    SimulationRequest,
)

from . import endpoint_calls

BASE_URL = "http://simulation.test"
TOKEN = "integration-token"


def _recording_transport(requests: list[httpx.Request]) -> httpx.MockTransport:
    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(500, json={"detail": "recorded"})

    return httpx.MockTransport(respond)


def _sent(request: httpx.Request) -> tuple[str, str, dict[str, str], bytes]:
    """Return the method, URL, headers and body a request puts on the wire."""
    return (request.method, str(request.url), dict(request.headers), request.content)


def _authenticated_client(requests: list[httpx.Request]) -> AuthenticatedClient:
    return AuthenticatedClient(
        base_url=BASE_URL,
        token=TOKEN,
        httpx_args={"transport": _recording_transport(requests)},
    )


def _plain_client(requests: list[httpx.Request]) -> Client:
    return Client(
        base_url=BASE_URL, httpx_args={"transport": _recording_transport(requests)}
    )


SIMULATION_REQUEST = SimulationRequest(country="us", scope="macro")
BUDGET_WINDOW_REQUEST = BudgetWindowBatchRequest(
    country="us",
    region="us",
    start_year="2026",
    window_size=2,
    max_parallel=1,
    scope="macro",
)

# Each endpoint: its endpoint_calls function and its sync_detailed, called with
# the same arguments.
ENDPOINT_CALLS: dict[
    str,
    tuple[
        Callable[[Client | AuthenticatedClient], object],
        Callable[[AuthenticatedClient], object],
    ],
] = {
    "submit_simulation": (
        lambda client: endpoint_calls.submit_simulation(client, SIMULATION_REQUEST),
        lambda client: submit_simulation_simulate_economy_comparison_post.sync_detailed(
            client=client, body=SIMULATION_REQUEST
        ),
    ),
    "job_status": (
        lambda client: endpoint_calls.job_status(client, "fc-job"),
        lambda client: get_job_status_jobs_job_id_get.sync_detailed(
            job_id="fc-job", client=client
        ),
    ),
    "submit_budget_window": (
        lambda client: endpoint_calls.submit_budget_window(
            client, BUDGET_WINDOW_REQUEST
        ),
        lambda client: (
            submit_budget_window_batch_simulate_economy_budget_window_post.sync_detailed(
                client=client, body=BUDGET_WINDOW_REQUEST
            )
        ),
    ),
    "budget_window_status": (
        lambda client: endpoint_calls.budget_window_status(client, "fc-batch"),
        lambda client: (
            get_budget_window_job_status_budget_window_jobs_batch_job_id_get.sync_detailed(
                batch_job_id="fc-batch", client=client
            )
        ),
    ),
}


@pytest.mark.parametrize("endpoint", sorted(ENDPOINT_CALLS))
def test_an_authenticated_call_sends_what_sync_detailed_sends(endpoint: str):
    support_call, generated_call = ENDPOINT_CALLS[endpoint]
    support_requests: list[httpx.Request] = []
    generated_requests: list[httpx.Request] = []

    support_call(_authenticated_client(support_requests))
    generated_call(_authenticated_client(generated_requests))

    assert [_sent(request) for request in support_requests] == [
        _sent(request) for request in generated_requests
    ]
    assert support_requests[0].headers["authorization"] == f"Bearer {TOKEN}"


@pytest.mark.parametrize("endpoint", sorted(ENDPOINT_CALLS))
def test_a_plain_call_sends_the_same_request_without_authorization(endpoint: str):
    support_call, generated_call = ENDPOINT_CALLS[endpoint]
    plain_requests: list[httpx.Request] = []
    generated_requests: list[httpx.Request] = []

    support_call(_plain_client(plain_requests))
    generated_call(_authenticated_client(generated_requests))

    method, url, headers, body = _sent(generated_requests[0])
    del headers["authorization"]
    assert [_sent(request) for request in plain_requests] == [
        (method, url, headers, body)
    ]
