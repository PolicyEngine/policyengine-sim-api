from __future__ import annotations

import json
import logging

import pytest
from policyengine_observability import REQUEST_ID_HEADER, current_context
from policyengine_simulation_contract.json_types import JsonObject

from policyengine_simulation_entry import app as app_module
from policyengine_simulation_entry.app import create_app
from policyengine_simulation_entry.backend import (
    BackendResponse,
    BackendTimeout,
    BackendUnavailable,
)

from conftest import FakeBackend, make_settings


def response(status: int, payload: JsonObject) -> BackendResponse:
    return BackendResponse(
        status_code=status,
        content=json.dumps(payload).encode(),
        headers={"content-type": "application/json", "retry-after": "3"},
    )


def test_health_is_local_and_compatible(client, backend):
    result = client.get("/health")

    assert result.status_code == 200
    assert result.json() == {"status": "healthy"}
    assert (
        result.headers["x-policyengine-simulation-revision"]
        == "simulation-entry-test-revision"
    )
    assert backend.requests == []


def test_readiness_tracks_backend(client, backend):
    assert client.get("/ready").json() == {"status": "ready"}

    backend.is_ready = False
    result = client.get("/ready")

    assert result.status_code == 503
    assert result.json() == {"status": "not_ready"}


def test_comparison_submission_preserves_upstream_response(client, backend):
    payload = {
        "job_id": "fc-123",
        "status": "submitted",
        "poll_url": "/jobs/fc-123",
        "country": "us",
        "version": "1.0.0",
        "resolved_app_name": "worker",
        "policyengine_bundle": {"model_version": "1.0.0"},
    }
    backend.responses[("POST", "/simulate/economy/comparison")] = response(
        202,
        payload,
    )

    result = client.post(
        "/simulate/economy/comparison",
        json={"country": "us", "scope": "macro", "reform": {}},
    )

    assert result.status_code == 202
    assert result.json() == payload
    assert result.headers["x-policyengine-simulation-backend"] == "old_gateway"
    assert backend.requests[-1].path == "/simulate/economy/comparison"


def test_job_status_preserves_id_and_status(client, backend):
    backend.responses[("GET", "/jobs/fc-123")] = response(
        202,
        {"status": "running", "run_id": "run-1"},
    )

    result = client.get("/jobs/fc-123")

    assert result.status_code == 202
    assert result.json() == {"status": "running", "run_id": "run-1"}
    assert backend.requests[-1].path == "/jobs/fc-123"


def test_job_status_records_structured_backend_telemetry(
    client,
    backend,
    monkeypatch,
):
    events = []
    monkeypatch.setattr(
        app_module,
        "record_event",
        lambda name, **attributes: events.append((name, attributes)),
    )
    backend.responses[("GET", "/jobs/fc-123")] = response(
        202,
        {"status": "running", "job_id": "must-not-be-recorded"},
    )

    client.get("/jobs/fc-123")

    name, attributes = events[-1]
    assert name == "simulation_entry_backend_response"
    assert attributes["job_state"] == "running"
    assert attributes["status_code"] == 202
    assert attributes["route"] == "/jobs/{job_id}"
    assert attributes["job_id"] == "fc-123"


def test_request_log_templates_route_and_keeps_structured_job_id(
    client,
    backend,
    caplog,
):
    backend.responses[("GET", "/jobs/fc-123")] = BackendResponse(
        202,
        b'{"job_id":"fc-123","status":"running"}',
        {"content-type": "application/json"},
    )

    with caplog.at_level(logging.INFO):
        result = client.get(
            "/jobs/fc-123",
            headers={"Authorization": "Bearer caller"},
        )

    assert result.status_code == 202
    request_record = next(
        record
        for record in caplog.records
        if record.getMessage() == "simulation_entry_request"
    )
    assert request_record.path == "/jobs/{job_id}"
    assert request_record.job_id == "fc-123"


def test_budget_window_routes_use_original_batch_id(client, backend):
    backend.responses[("POST", "/simulate/economy/budget-window")] = response(
        202,
        {
            "batch_job_id": "batch-123",
            "status": "submitted",
            "poll_url": "/budget-window-jobs/batch-123",
        },
    )
    backend.responses[("GET", "/budget-window-jobs/batch-123")] = response(
        200,
        {"status": "complete", "completed_years": ["2026"]},
    )

    submitted = client.post(
        "/simulate/economy/budget-window",
        json={
            "country": "us",
            "region": "us",
            "scope": "macro",
            "reform": {},
            "start_year": "2026",
            "window_size": 1,
        },
    )
    polled = client.get("/budget-window-jobs/batch-123")

    assert submitted.status_code == 202
    assert submitted.json()["batch_job_id"] == "batch-123"
    assert polled.status_code == 200
    assert backend.requests[-1].path == "/budget-window-jobs/batch-123"


def test_versions_and_ping_are_public_proxy_routes(client, backend):
    backend.responses[("GET", "/versions/us")] = response(
        200,
        {"latest": "1.2.3"},
    )
    backend.responses[("POST", "/ping")] = response(200, {"incremented": 2})

    assert client.get("/versions/us").json() == {"latest": "1.2.3"}
    assert client.post("/ping", json={"value": 1}).json() == {"incremented": 2}


def test_request_id_is_propagated_and_returned(client, backend):
    result = client.get("/versions", headers={"X-Request-ID": "request-123"})

    assert result.headers["x-request-id"] == "request-123"
    assert backend.requests[-1].request_id == "request-123"


def test_generated_request_id_is_shared_with_observability():
    class CapturingBackend(FakeBackend):
        observability_request_id: str | None = None

        async def request(self, *args, **kwargs):
            context = current_context()
            self.observability_request_id = (
                context.request_id if context is not None else None
            )
            return await super().request(*args, **kwargs)

    backend = CapturingBackend()
    app = create_app(
        settings=make_settings(),
        backend=backend,
        auth_dependency=lambda: None,
    )

    from fastapi.testclient import TestClient

    with TestClient(app) as client:
        result = client.get("/versions")

    request_id = result.headers["x-request-id"]
    assert request_id
    assert result.headers[REQUEST_ID_HEADER] == request_id
    assert backend.requests[-1].request_id == request_id
    assert backend.observability_request_id == request_id


def test_request_validation_matches_shared_contract(client):
    result = client.post(
        "/simulate/economy/comparison",
        json={"country": "us", "unknown": True},
    )

    assert result.status_code == 422


@pytest.mark.parametrize(
    ("error", "expected_status", "expected_detail"),
    [
        (BackendUnavailable("unavailable"), 503, "Simulation backend is unavailable."),
        (BackendTimeout("timed out"), 504, "Simulation backend timed out."),
    ],
)
def test_backend_failures_are_sanitized(error, expected_status, expected_detail):
    class FailingBackend(FakeBackend):
        async def request(self, *args, **kwargs):
            raise error

    app = create_app(
        settings=make_settings(),
        backend=FailingBackend(),
        auth_dependency=lambda: None,
    )

    from fastapi.testclient import TestClient

    with TestClient(app) as client:
        result = client.get("/jobs/job-1")

    assert result.status_code == expected_status
    assert result.json() == {"detail": expected_detail}
    assert result.headers["retry-after"] == "10"
    assert result.headers["x-policyengine-simulation-backend"] == "old_gateway"


def test_unexpected_failure_preserves_correlation_headers_and_request_log(caplog):
    class FailingBackend(FakeBackend):
        async def request(self, *args, **kwargs):
            raise RuntimeError("unexpected backend failure")

    app = create_app(
        settings=make_settings(),
        backend=FailingBackend(),
        auth_dependency=lambda: None,
    )

    from fastapi.testclient import TestClient

    with caplog.at_level(logging.INFO), TestClient(app) as client:
        result = client.get(
            "/jobs/job-1",
            headers={"X-Request-ID": "request-500"},
        )

    assert result.status_code == 500
    assert result.text == "Internal Server Error"
    assert result.headers["x-request-id"] == "request-500"
    assert (
        result.headers["x-policyengine-simulation-revision"]
        == "simulation-entry-test-revision"
    )
    request_record = next(
        record
        for record in caplog.records
        if record.getMessage() == "simulation_entry_request"
    )
    assert request_record.status_code == 500
    assert request_record.path == "/jobs/{job_id}"
    assert request_record.job_id == "job-1"


@pytest.mark.parametrize("status_code", [400, 404, 409, 500])
def test_upstream_error_status_and_body_are_preserved(client, backend, status_code):
    backend.responses[("GET", "/jobs/job-1")] = response(
        status_code,
        {"detail": "upstream response"},
    )

    result = client.get("/jobs/job-1")

    assert result.status_code == status_code
    assert result.json() == {"detail": "upstream response"}
