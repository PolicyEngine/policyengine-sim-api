from __future__ import annotations

import asyncio
import json
import logging
from unittest.mock import Mock

import pytest
from conftest import FakeBackend, make_settings
from fastapi import HTTPException
from policyengine_observability import REQUEST_ID_HEADER
from policyengine_simulation_contract.json_types import JsonObject
from policyengine_simulation_contract.stage12_execution import (
    ComparisonRunLifecycleStatus,
    SimulationRole,
)
from policyengine_simulation_entry import app as app_module
from policyengine_simulation_entry.app import create_app
from policyengine_simulation_entry.backend import (
    BackendResponse,
    BackendTimeout,
    BackendUnavailable,
)
from policyengine_simulation_entry.stage12_backend import (
    TemporaryStage12DispatchFailed,
    TemporaryStage12UnsupportedRequest,
)
from policyengine_simulation_observability.identifiers import OBSERVABILITY_ID_HEADER
from stage12_fixtures import (
    EVALUATION_ID,
    comparison_report,
    comparison_simulation,
    eligible_payload,
)


def response(
    status: int,
    payload: JsonObject,
    *,
    headers: dict[str, str] | None = None,
) -> BackendResponse:
    return BackendResponse(
        status_code=status,
        content=json.dumps(payload).encode(),
        headers={
            "content-type": "application/json",
            "retry-after": "3",
            **(headers or {}),
        },
    )


def automatic_stage12_settings():
    return make_settings(
        stage12_enabled=True,
        stage12_v2_manifest_environment="staging",
        stage12_database_url=(
            "postgresql://policyengine_v2_runtime:secret@"
            "test.pooler.supabase.com:5432/postgres?sslmode=require"
        ),
        stage12_artifact_bucket="policyengine-stage12-staging",
    )


class TemporaryComparisonBackend:
    def __init__(self, *, report=None, simulations=()):
        self.report = report or comparison_report()
        self.simulations = simulations
        self.submissions = []
        self.polls = []

    async def dispatch_after_production(self, **_):
        return None

    async def submit_temporary_report(self, **submission):
        self.submissions.append(submission)
        return self.report

    async def get_temporary_report(self, evaluation_id):
        self.polls.append(evaluation_id)
        if evaluation_id != self.report.evaluation_id:
            raise LookupError("missing report")
        return self.report, self.simulations


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


def test_automatic_comparison_dispatch_preserves_production_response(
    backend,
):
    class ComparisonBackend:
        def __init__(self):
            self.calls = []

        async def dispatch_after_production(self, **call):
            self.calls.append(call)

    comparison = ComparisonBackend()
    payload = {
        "job_id": "fc-123",
        "status": "submitted",
        "poll_url": "/jobs/fc-123",
        "country": "us",
        "version": "1.0.0",
        "resolved_app_name": "production-worker",
        "policyengine_bundle": {"model_version": "1.0.0"},
    }
    backend.responses[("POST", "/simulate/economy/comparison")] = response(202, payload)
    app = create_app(
        settings=automatic_stage12_settings(),
        backend=backend,
        auth_dependency=lambda: None,
        comparison_backend=comparison,
    )

    from fastapi.testclient import TestClient

    observability_id = "00000000-0000-4000-8000-000000000001"
    with TestClient(app) as test_client:
        result = test_client.post(
            "/simulate/economy/comparison",
            json={
                "country": "us",
                "scope": "macro",
                "reform": {},
                "_telemetry": {
                    "submission_claim_id": "api-process-1",
                    "capture_mode": "disabled",
                },
            },
            headers={
                REQUEST_ID_HEADER: "request-1",
                OBSERVABILITY_ID_HEADER: observability_id,
            },
        )

    assert result.status_code == 202
    assert result.json() == payload
    assert result.headers["x-policyengine-simulation-backend"] == "old_gateway"
    assert len(comparison.calls) == 1
    assert comparison.calls[0]["request_id"] == "request-1"
    assert comparison.calls[0]["observability_id"] == observability_id
    assert "observability_id" not in comparison.calls[0]["request_payload"]["telemetry"]
    assert json.loads(comparison.calls[0]["production_response"]) == payload


def test_comparison_dispatch_failure_cannot_change_production_response(backend):
    class FailingComparisonBackend:
        async def dispatch_after_production(self, **_):
            raise RuntimeError("comparison unavailable")

    payload = {
        "job_id": "fc-123",
        "status": "submitted",
        "poll_url": "/jobs/fc-123",
        "country": "us",
        "version": "1.0.0",
        "resolved_app_name": "production-worker",
        "policyengine_bundle": {"model_version": "1.0.0"},
    }
    backend.responses[("POST", "/simulate/economy/comparison")] = response(202, payload)
    app = create_app(
        settings=automatic_stage12_settings(),
        backend=backend,
        auth_dependency=lambda: None,
        comparison_backend=FailingComparisonBackend(),
    )

    from fastapi.testclient import TestClient

    with TestClient(app) as test_client:
        result = test_client.post(
            "/simulate/economy/comparison",
            json={"country": "us", "scope": "macro", "reform": {}},
        )

    assert result.status_code == 202
    assert result.json() == payload


def test_comparison_dispatch_timeout_cannot_change_production_response(
    backend,
    monkeypatch,
):
    class SlowComparisonBackend:
        async def dispatch_after_production(self, **_):
            await asyncio.sleep(1)

    payload = {
        "job_id": "fc-123",
        "status": "submitted",
        "poll_url": "/jobs/fc-123",
        "country": "us",
        "version": "1.0.0",
        "resolved_app_name": "production-worker",
        "policyengine_bundle": {"model_version": "1.0.0"},
    }
    backend.responses[("POST", "/simulate/economy/comparison")] = response(202, payload)
    monkeypatch.setattr(app_module, "STAGE12_MODAL_SUBMISSION_TIMEOUT_SECONDS", 0.001)
    app = create_app(
        settings=automatic_stage12_settings(),
        backend=backend,
        auth_dependency=lambda: None,
        comparison_backend=SlowComparisonBackend(),
    )

    from fastapi.testclient import TestClient

    with TestClient(app) as test_client:
        result = test_client.post(
            "/simulate/economy/comparison",
            json={"country": "us", "scope": "macro", "reform": {}},
        )

    assert result.status_code == 202
    assert result.json() == payload


def test_disabled_automatic_comparison_never_invokes_configured_backend(backend):
    class ComparisonBackend:
        def __init__(self):
            self.calls = []

        async def dispatch_after_production(self, **call):
            self.calls.append(call)

    comparison = ComparisonBackend()
    backend.responses[("POST", "/simulate/economy/comparison")] = response(
        202,
        {"job_id": "fc-123", "status": "submitted"},
    )
    app = create_app(
        settings=make_settings(stage12_enabled=False),
        backend=backend,
        auth_dependency=lambda: None,
        comparison_backend=comparison,
    )

    from fastapi.testclient import TestClient

    with TestClient(app) as test_client:
        result = test_client.post(
            "/simulate/economy/comparison",
            json={"country": "us", "scope": "macro", "reform": {}},
        )

    assert result.status_code == 202
    assert comparison.calls == []


def test_temporary_stage12_submission_returns_polling_identifier(backend):
    comparison = TemporaryComparisonBackend()
    app = create_app(
        settings=automatic_stage12_settings(),
        backend=backend,
        auth_dependency=lambda: None,
        comparison_backend=comparison,
    )

    from fastapi.testclient import TestClient

    with TestClient(app) as test_client:
        result = test_client.post(
            "/internal/stage12/reports",
            json=eligible_payload(),
            headers={REQUEST_ID_HEADER: "manual-request-1"},
        )

    assert result.status_code == 202
    assert result.headers["retry-after"] == "1"
    assert result.json() == {
        "evaluation_id": str(EVALUATION_ID),
        "status": "running",
        "poll_url": f"/internal/stage12/reports/{EVALUATION_ID}",
    }
    assert comparison.submissions[0]["request_id"] == "manual-request-1"
    assert backend.requests == []


def test_temporary_stage12_submission_has_a_hard_acknowledgement_timeout(
    backend,
    monkeypatch,
):
    class BlockingComparisonBackend(TemporaryComparisonBackend):
        async def submit_temporary_report(self, **submission):
            self.submissions.append(submission)
            await asyncio.Event().wait()

    comparison = BlockingComparisonBackend()
    monkeypatch.setattr(app_module, "STAGE12_MODAL_SUBMISSION_TIMEOUT_SECONDS", 0.001)
    app = create_app(
        settings=automatic_stage12_settings(),
        backend=backend,
        auth_dependency=lambda: None,
        comparison_backend=comparison,
    )

    from fastapi.testclient import TestClient

    with TestClient(app) as test_client:
        result = test_client.post(
            "/internal/stage12/reports",
            json=eligible_payload(),
        )

    assert result.status_code == 504
    assert result.json() == {"detail": "Stage 12 submission acknowledgement timed out."}
    assert len(comparison.submissions) == 1


def test_temporary_stage12_poll_not_found_has_short_retry_hint(backend):
    class MissingComparisonBackend(TemporaryComparisonBackend):
        async def get_temporary_report(self, evaluation_id):
            self.polls.append(evaluation_id)
            raise LookupError("not created")

    comparison = MissingComparisonBackend()
    app = create_app(
        settings=make_settings(),
        backend=backend,
        auth_dependency=lambda: None,
        comparison_backend=comparison,
    )

    from fastapi.testclient import TestClient

    with TestClient(app) as test_client:
        result = test_client.get(f"/internal/stage12/reports/{EVALUATION_ID}")

    assert result.status_code == 404
    assert result.headers["retry-after"] == "1"
    assert result.json() == {"detail": "Stage 12 report was not found."}


def test_temporary_stage12_poll_reads_durable_parent_and_children(backend):
    simulations = (
        comparison_simulation(SimulationRole.BASELINE),
        comparison_simulation(SimulationRole.REFORM),
    )
    comparison = TemporaryComparisonBackend(simulations=simulations)
    app = create_app(
        settings=make_settings(),
        backend=backend,
        auth_dependency=lambda: None,
        comparison_backend=comparison,
    )

    from fastapi.testclient import TestClient

    with TestClient(app) as test_client:
        result = test_client.get(f"/internal/stage12/reports/{EVALUATION_ID}")

    assert result.status_code == 202
    assert result.headers["retry-after"] == "5"
    assert result.json()["report"]["evaluation_id"] == str(EVALUATION_ID)
    assert [item["role"] for item in result.json()["simulations"]] == [
        "baseline",
        "reform",
    ]
    assert comparison.polls == [EVALUATION_ID]
    assert backend.requests == []


def test_temporary_stage12_poll_returns_terminal_metadata_with_200(backend):
    comparison = TemporaryComparisonBackend(
        report=comparison_report(
            status=ComparisonRunLifecycleStatus.SUCCEEDED
        ).model_copy(
            update={"observability_id": "00000000-0000-4000-8000-000000000012"}
        )
    )
    app = create_app(
        settings=make_settings(),
        backend=backend,
        auth_dependency=lambda: None,
        comparison_backend=comparison,
    )

    from fastapi.testclient import TestClient

    with TestClient(app) as test_client:
        result = test_client.get(f"/internal/stage12/reports/{EVALUATION_ID}")

    assert result.status_code == 200
    assert "retry-after" not in result.headers
    assert (
        result.headers[OBSERVABILITY_ID_HEADER]
        == "00000000-0000-4000-8000-000000000012"
    )
    assert (
        result.json()["report"]["observability_id"]
        == "00000000-0000-4000-8000-000000000012"
    )
    assert result.json()["report"]["aggregate_output_uri"] == (
        "gs://stage12-private/report.json"
    )


def test_temporary_stage12_routes_are_excluded_from_openapi(backend):
    app = create_app(
        settings=make_settings(),
        backend=backend,
        auth_dependency=lambda: None,
        comparison_backend=TemporaryComparisonBackend(),
    )

    paths = app.openapi()["paths"]

    assert "/internal/stage12/reports" not in paths
    assert "/internal/stage12/reports/{evaluation_id}" not in paths


def test_temporary_stage12_submission_is_disabled_when_flag_is_zero(backend):
    comparison = TemporaryComparisonBackend()
    app = create_app(
        settings=make_settings(stage12_enabled=False),
        backend=backend,
        auth_dependency=lambda: None,
        comparison_backend=comparison,
    )

    from fastapi.testclient import TestClient

    with TestClient(app) as test_client:
        result = test_client.post(
            "/internal/stage12/reports",
            json=eligible_payload(),
        )

    assert result.status_code == 503
    assert result.json() == {"detail": "Stage 12 execution is disabled."}
    assert comparison.submissions == []


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("post", "/internal/stage12/reports", eligible_payload()),
        ("get", f"/internal/stage12/reports/{EVALUATION_ID}", None),
    ],
)
def test_temporary_stage12_routes_require_existing_authentication(
    backend,
    method,
    path,
    body,
):
    def reject_caller():
        raise HTTPException(status_code=403)

    app = create_app(
        settings=automatic_stage12_settings(),
        backend=backend,
        auth_dependency=reject_caller,
        comparison_backend=TemporaryComparisonBackend(),
    )

    from fastapi.testclient import TestClient

    with TestClient(app) as test_client:
        kwargs = {"json": body} if body is not None else {}
        result = getattr(test_client, method)(path, **kwargs)

    assert result.status_code == 403


def test_temporary_stage12_poll_returns_404_for_unknown_report(backend):
    app = create_app(
        settings=make_settings(),
        backend=backend,
        auth_dependency=lambda: None,
        comparison_backend=TemporaryComparisonBackend(),
    )

    from fastapi.testclient import TestClient

    with TestClient(app) as test_client:
        result = test_client.get(
            "/internal/stage12/reports/00000000-0000-0000-0000-000000000099"
        )

    assert result.status_code == 404
    assert result.json() == {"detail": "Stage 12 report was not found."}


@pytest.mark.parametrize(
    ("error", "status_code", "expected"),
    [
        (
            TemporaryStage12UnsupportedRequest("unsupported_cliff_calculation"),
            422,
            {"reason": "unsupported_cliff_calculation"},
        ),
        (
            TemporaryStage12DispatchFailed(
                comparison_report(status=ComparisonRunLifecycleStatus.FAILED)
            ),
            502,
            {"evaluation_id": str(EVALUATION_ID), "status": "failed"},
        ),
    ],
)
def test_temporary_stage12_submission_returns_bounded_failures(
    backend,
    error,
    status_code,
    expected,
):
    class FailingTemporaryBackend(TemporaryComparisonBackend):
        async def submit_temporary_report(self, **_):
            raise error

    app = create_app(
        settings=automatic_stage12_settings(),
        backend=backend,
        auth_dependency=lambda: None,
        comparison_backend=FailingTemporaryBackend(),
    )

    from fastapi.testclient import TestClient

    with TestClient(app) as test_client:
        result = test_client.post(
            "/internal/stage12/reports",
            json=eligible_payload(),
        )

    assert result.status_code == status_code
    for field, value in expected.items():
        assert result.json()[field] == value


def test_job_status_preserves_id_and_status(client, backend):
    observability_id = "00000000-0000-4000-8000-000000000001"
    backend.responses[("GET", "/jobs/fc-123")] = response(
        202,
        {"status": "running"},
        headers={OBSERVABILITY_ID_HEADER: observability_id},
    )

    result = client.get("/jobs/fc-123")

    assert result.status_code == 202
    assert result.json() == {"status": "running"}
    assert result.headers[OBSERVABILITY_ID_HEADER] == observability_id
    assert backend.requests[-1].path == "/jobs/fc-123"


def test_polling_and_budget_window_routes_never_dispatch_stage12_comparison(backend):
    class ComparisonBackend:
        def __init__(self):
            self.calls = []

        async def dispatch_after_production(self, **call):
            self.calls.append(call)

    comparison = ComparisonBackend()
    backend.responses[("GET", "/jobs/fc-123")] = response(
        200,
        {"status": "complete", "result": {"budget": 10}},
    )
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
    app = create_app(
        settings=automatic_stage12_settings(),
        backend=backend,
        auth_dependency=lambda: None,
        comparison_backend=comparison,
    )

    from fastapi.testclient import TestClient

    with TestClient(app) as test_client:
        job = test_client.get("/jobs/fc-123")
        batch = test_client.post(
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
        batch_poll = test_client.get("/budget-window-jobs/batch-123")

    assert job.json() == {"status": "complete", "result": {"budget": 10}}
    assert batch.json() == {
        "batch_job_id": "batch-123",
        "status": "submitted",
        "poll_url": "/budget-window-jobs/batch-123",
    }
    assert batch_poll.json() == {
        "status": "complete",
        "completed_years": ["2026"],
    }
    assert comparison.calls == []


def test_job_status_records_structured_backend_telemetry(
    client,
    backend,
    monkeypatch,
):
    runtime = client.app.state.policyengine_observability
    event = Mock(wraps=runtime.event)
    monkeypatch.setattr(runtime, "event", event)
    backend.responses[("GET", "/jobs/fc-123")] = response(
        202,
        {"status": "running", "job_id": "must-not-be-recorded"},
    )

    client.get("/jobs/fc-123")

    call = next(
        item
        for item in event.call_args_list
        if item.args == ("simulation_entry_backend_response",)
    )
    name = call.args[0]
    attributes = call.kwargs["attributes"]
    assert name == "simulation_entry_backend_response"
    assert attributes["job_state"] == "running"
    assert attributes["status_code"] == 202
    assert attributes["route"] == "/jobs/{job_id}"
    assert attributes["job_id"] == "fc-123"


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


def test_request_id_is_propagated_logged_and_returned(
    client,
    backend,
):
    result = client.get(
        "/versions",
        headers={REQUEST_ID_HEADER: "request-123"},
    )

    assert result.headers[REQUEST_ID_HEADER] == "request-123"
    assert "x-request-id" not in result.headers
    assert backend.requests[-1].request_id == "request-123"


def test_request_identifiers_are_attached_to_the_active_observability_context(
    backend,
):
    app = create_app(
        settings=make_settings(),
        backend=backend,
        auth_dependency=lambda: None,
    )
    runtime = app.state.policyengine_observability

    @app.get("/_test/runtime-context")
    def runtime_context():
        return runtime.capture_context()

    from fastapi.testclient import TestClient

    observability_id = "00000000-0000-4000-8000-000000000001"
    with TestClient(app) as test_client:
        result = test_client.get(
            "/_test/runtime-context",
            headers={
                REQUEST_ID_HEADER: "request-123",
                OBSERVABILITY_ID_HEADER: observability_id,
            },
        )

    assert result.status_code == 200
    assert result.json()["request_id"] == "request-123"
    assert result.json()["observability_id"] == observability_id


def test_x_request_id_is_not_an_alias(client, backend):
    result = client.get(
        "/versions",
        headers={"X-Request-ID": "must-not-be-used"},
    )

    request_id = result.headers[REQUEST_ID_HEADER]
    assert request_id
    assert request_id != "must-not-be-used"
    assert backend.requests[-1].request_id == request_id
    assert "x-request-id" not in result.headers


def test_entrypoint_uses_its_own_service_name(backend):
    app = create_app(
        settings=make_settings(),
        backend=backend,
        auth_dependency=lambda: None,
    )

    assert (
        app.state.policyengine_observability.config.service.name
        == "policyengine-simulation-entry"
    )


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
        result = client.get(
            "/jobs/job-1",
            headers={REQUEST_ID_HEADER: "request-backend-failure"},
        )

    assert result.status_code == expected_status
    assert result.json() == {"detail": expected_detail}
    assert result.headers["retry-after"] == "10"
    assert result.headers["x-policyengine-simulation-backend"] == "old_gateway"
    assert result.headers[REQUEST_ID_HEADER] == "request-backend-failure"
    assert "x-request-id" not in result.headers


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
            headers={REQUEST_ID_HEADER: "request-500"},
        )

    assert result.status_code == 500
    assert result.text == "Internal Server Error"
    assert result.headers[REQUEST_ID_HEADER] == "request-500"
    assert "x-request-id" not in result.headers
    assert (
        result.headers["x-policyengine-simulation-revision"]
        == "simulation-entry-test-revision"
    )
    request_record = next(
        record
        for record in caplog.records
        if record.getMessage() == "simulation_entry_unhandled_request"
    )
    assert request_record.path == "/jobs/{job_id}"
    assert request_record.job_id == "job-1"


@pytest.mark.parametrize("status_code", [400, 404, 409, 500, 502])
def test_upstream_error_status_and_body_are_preserved(client, backend, status_code):
    backend.responses[("GET", "/jobs/job-1")] = response(
        status_code,
        {"detail": "upstream response"},
    )

    result = client.get(
        "/jobs/job-1",
        headers={REQUEST_ID_HEADER: "request-upstream-error"},
    )

    assert result.status_code == status_code
    assert result.json() == {"detail": "upstream response"}
    assert result.headers[REQUEST_ID_HEADER] == "request-upstream-error"
    assert "x-request-id" not in result.headers


@pytest.mark.parametrize(
    "path,extra",
    [
        ("/simulate/economy/comparison", {}),
        (
            "/simulate/economy/budget-window",
            {"region": "us", "start_year": "2026", "window_size": 2},
        ),
    ],
)
@pytest.mark.parametrize("date_fields", [{"as_of": None}, {}])
def test_submission_preserves_spm_field_presence(
    client, backend, path, extra, date_fields
):
    selection = {"geography_kind": "national", **date_fields}

    result = client.post(path, json={"country": "us", "spm": selection, **extra})

    assert result.status_code == 200
    forwarded = backend.requests[-1]
    assert forwarded.method == "POST"
    assert forwarded.path == path
    # Explicit null clears the downstream bundle cutoff; an omitted date inherits it.
    # Other omitted fields must remain absent so bundle defaults can fill them.
    assert forwarded.json_body["spm"] == selection
