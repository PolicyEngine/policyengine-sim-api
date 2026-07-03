"""Tests for the health endpoint."""

from fastapi.testclient import TestClient


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
