"""Tests for the ping endpoint."""

from fastapi.testclient import TestClient


class TestPingEndpoint:
    """Tests for POST /ping endpoint."""

    def test_ping_increments_value(self, client: TestClient):
        """Given a value, when pinging, then the response has the value plus one."""
        # Given
        request_body = {"value": 10}

        # When
        response = client.post("/ping", json=request_body)

        # Then
        assert response.status_code == 200
        assert response.json() == {"incremented": 11}

    def test_ping_handles_zero(self, client: TestClient):
        """Given value zero, when pinging, then the response contains 1."""
        # Given
        request_body = {"value": 0}

        # When
        response = client.post("/ping", json=request_body)

        # Then
        assert response.status_code == 200
        assert response.json() == {"incremented": 1}

    def test_ping_handles_negative_value(self, client: TestClient):
        """Given a negative value, when pinging, then return it incremented."""
        # Given
        request_body = {"value": -5}

        # When
        response = client.post("/ping", json=request_body)

        # Then
        assert response.status_code == 200
        assert response.json() == {"incremented": -4}

    def test_ping_rejects_missing_value(self, client: TestClient):
        """Given no value field, when pinging, then return a 422 validation error."""
        # Given
        request_body = {}

        # When
        response = client.post("/ping", json=request_body)

        # Then
        assert response.status_code == 422

    def test_ping_rejects_non_integer_value(self, client: TestClient):
        """Given a non-integer value, when pinging, then return a 422 error."""
        # Given
        request_body = {"value": "not_an_integer"}

        # When
        response = client.post("/ping", json=request_body)

        # Then
        assert response.status_code == 422
