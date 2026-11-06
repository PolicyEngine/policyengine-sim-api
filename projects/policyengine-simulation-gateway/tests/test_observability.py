"""Gateway observability identity and request correlation tests."""

import json

import pytest
from fastapi.testclient import TestClient
from policyengine_observability import REQUEST_ID_HEADER
from policyengine_simulation_gateway.testing import create_gateway_app


def _records_written(captured_output: str) -> list[dict[str, object]]:
    """Return the JSON records the runtime wrote to standard output, in order."""
    records: list[dict[str, object]] = []
    for output_line in captured_output.splitlines():
        try:
            record = json.loads(output_line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict):
            records.append(record)
    return records


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


def test_exception_recorded_while_a_route_runs_carries_the_matched_route(
    capsys: pytest.CaptureFixture[str], mock_modal
):
    # The runtime keeps the standard output it finds when it is built, so the
    # app is built after capsys replaces it.
    client = TestClient(create_gateway_app(), raise_server_exceptions=False)
    submit_response = client.post(
        "/simulate/economy/comparison",
        json={"country": "us", "scope": "macro", "reform": {}},
    )
    job_id = submit_response.json()["job_id"]
    mock_modal["function_call"].from_id_errors[job_id] = RuntimeError(
        "Modal control plane unavailable"
    )
    capsys.readouterr()

    response = client.get(f"/jobs/{job_id}")

    assert response.status_code == 500
    routes_by_event_name: dict[object, list[object]] = {}
    for record in _records_written(capsys.readouterr().out):
        routes_by_event_name.setdefault(record["event.name"], []).append(
            record.get("http.route")
        )
    assert routes_by_event_name["exception.recorded"] == ["/jobs/{job_id}"]
    assert routes_by_event_name["request.completed"] == ["/jobs/{job_id}"]
