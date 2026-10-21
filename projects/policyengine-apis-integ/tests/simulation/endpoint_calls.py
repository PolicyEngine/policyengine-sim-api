"""Call the generated client's authenticated endpoints with either client.

The generated ``sync_detailed`` of an endpoint that declares bearer
authentication accepts only an ``AuthenticatedClient``. Against local services,
which run without authentication, these tests use a plain ``Client`` and send no
``Authorization`` header. Each function here sends exactly what the endpoint's
``sync_detailed`` sends: the endpoint module's request keywords
(``_get_kwargs``), through the given client's own httpx client, with the
response parsed by the module's ``_build_response``. Only the client type it
accepts differs.
"""

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


def submit_simulation(client: Client | AuthenticatedClient, body: SimulationRequest):
    """POST /simulate/economy/comparison."""
    endpoint = submit_simulation_simulate_economy_comparison_post
    request = endpoint._get_kwargs(body=body)
    response = client.get_httpx_client().request(**request)
    return endpoint._build_response(client=client, response=response)


def job_status(client: Client | AuthenticatedClient, job_id: str):
    """GET /jobs/{job_id}."""
    endpoint = get_job_status_jobs_job_id_get
    request = endpoint._get_kwargs(job_id=job_id)
    response = client.get_httpx_client().request(**request)
    return endpoint._build_response(client=client, response=response)


def submit_budget_window(
    client: Client | AuthenticatedClient, body: BudgetWindowBatchRequest
):
    """POST /simulate/economy/budget-window."""
    endpoint = submit_budget_window_batch_simulate_economy_budget_window_post
    request = endpoint._get_kwargs(body=body)
    response = client.get_httpx_client().request(**request)
    return endpoint._build_response(client=client, response=response)


def budget_window_status(client: Client | AuthenticatedClient, batch_job_id: str):
    """GET /budget-window-jobs/{batch_job_id}."""
    endpoint = get_budget_window_job_status_budget_window_jobs_batch_job_id_get
    request = endpoint._get_kwargs(batch_job_id=batch_job_id)
    response = client.get_httpx_client().request(**request)
    return endpoint._build_response(client=client, response=response)
