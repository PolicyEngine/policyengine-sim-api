"""Gateway observability identity and request correlation tests."""

from fastapi.testclient import TestClient
from policyengine_observability import REQUEST_ID_HEADER
from policyengine_simulation_gateway.testing import create_gateway_app


def test_gateway_preserves_policyengine_request_id():
    app = create_gateway_app()
    response = TestClient(app).get(
        "/health",
        headers={REQUEST_ID_HEADER: "request-through-entrypoint"},
    )

    assert response.status_code == 200
    assert response.headers[REQUEST_ID_HEADER] == "request-through-entrypoint"


def test_gateway_uses_explicit_api_v1_identity():
    app = create_gateway_app()
    config = app.state.policyengine_observability.config

    assert config.service.name == "policyengine-simulation-gateway"
    assert config.service.namespace == "policyengine.api-v1"
    assert config.service.role == "modal_gateway"
    assert config.deployment.environment == "test"
    assert config.deployment.platform == "local"


def test_gateway_response_survives_unavailable_collector(monkeypatch):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "https://invalid.invalid")
    monkeypatch.setenv("POLICYENGINE_OTEL_GOOGLE_AUDIENCE", "not-a-url")
    app = create_gateway_app()

    response = TestClient(app).get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "healthy"}
