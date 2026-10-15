import backoff
import httpx
from policyengine_api_simulation_client import AuthenticatedClient, Client
from policyengine_api_simulation_client.api.default import ping_ping_post
from policyengine_api_simulation_client.errors import UnexpectedStatus
from policyengine_api_simulation_client.models import PingRequest, PingResponse


@backoff.on_exception(
    backoff.expo,
    (httpx.HTTPStatusError, UnexpectedStatus),
    max_tries=5,
    giveup=lambda e: getattr(e, "response", {}).get("status_code", 0) != 503,
)
def test_ping(client: Client | AuthenticatedClient):
    """
    Given a running simulation API
    When a ping request is sent
    Then the response contains the incremented value.
    """
    response = ping_ping_post.sync(client=client, body=PingRequest(value=12))
    assert isinstance(response, PingResponse)
    assert response.incremented == 13
