"""Tests for the health endpoint."""

from fastapi.testclient import TestClient
from policyengine_simulation_observability.identifiers import OBSERVABILITY_ID_HEADER


class TestHealthEndpoint:
    """Tests for GET /health endpoint."""

    def test_health_returns_healthy_status(self, client: TestClient):
        """
        Given a running gateway service
        When the health endpoint is called
        Then the response indicates healthy status.
        """
        # When
        response = client.get("/health")

        # Then
        assert response.status_code == 200
        assert response.json() == {"status": "healthy"}
        assert OBSERVABILITY_ID_HEADER not in response.headers

    def test_health_does_not_adopt_an_incoming_workflow_identifier(
        self,
        client: TestClient,
    ):
        response = client.get(
            "/health",
            headers={OBSERVABILITY_ID_HEADER: "00000000-0000-4000-8000-000000000001"},
        )

        assert response.status_code == 200
        assert OBSERVABILITY_ID_HEADER not in response.headers

    def test_health_is_idempotent(self, client: TestClient):
        """
        Given a running gateway service
        When the health endpoint is called multiple times
        Then each response is identical.
        """
        # When
        response1 = client.get("/health")
        response2 = client.get("/health")

        # Then
        assert response1.status_code == 200
        assert response2.status_code == 200
        assert response1.json() == response2.json()
