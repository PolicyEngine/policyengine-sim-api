"""Characterization tests for entry app branches the route tests leave uncovered."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import override
from unittest.mock import Mock

import pytest
from conftest import FakeBackend, make_settings
from fastapi.testclient import TestClient
from policyengine_observability import REQUEST_ID_HEADER
from policyengine_simulation_entry import app as app_module
from policyengine_simulation_entry.app import create_app
from policyengine_simulation_entry.backend import BackendResponse
from policyengine_simulation_observability.identifiers import OBSERVABILITY_ID_HEADER
from stage12_fixtures import EVALUATION_ID, eligible_payload

STAGE12_SETTINGS = {
    "stage12_enabled": True,
    "stage12_v2_manifest_environment": "staging",
    "stage12_database_url": (
        "postgresql://policyengine_v2_runtime:secret@"
        "test.pooler.supabase.com:5432/postgres?sslmode=require"
    ),
    "stage12_artifact_bucket": "policyengine-stage12-staging",
}
ENTRY_OBSERVABILITY_ID = "00000000-0000-4000-8000-000000000001"
BACKEND_OBSERVABILITY_ID = "00000000-0000-4000-8000-000000000099"
BUDGET_WINDOW_BODY = {
    "country": "us",
    "region": "us",
    "scope": "macro",
    "reform": {},
    "start_year": "2026",
    "window_size": 1,
}


def _backend_response(
    content: bytes, headers: dict[str, str] | None = None
) -> BackendResponse:
    return BackendResponse(
        status_code=200,
        content=content,
        headers={"content-type": "application/json", **(headers or {})},
    )


def _event_attributes(event: Mock, name: str) -> list[dict]:
    return [
        call.kwargs["attributes"]
        for call in event.call_args_list
        if call.args == (name,)
    ]


@pytest.fixture
def recorded_events(client, monkeypatch) -> Mock:
    runtime = client.app.state.policyengine_observability
    event = Mock(wraps=runtime.event)
    monkeypatch.setattr(runtime, "event", event)
    return event


@dataclass(frozen=True)
class ForwardedCase:
    method: str
    path: str
    route: str
    content: bytes
    expected: dict[str, object]
    body: dict | None = None


@pytest.mark.parametrize(
    "case",
    [
        pytest.param(
            ForwardedCase(
                "POST",
                "/simulate/economy/comparison",
                "/simulate/economy/comparison",
                b'{"status": "submitted", "job_id": "fc-1"}',
                {"job_state": "submitted", "job_id": "fc-1"},
                body={"country": "us", "scope": "macro", "reform": {}},
            ),
            id="submission-records-job-id",
        ),
        pytest.param(
            ForwardedCase(
                "POST",
                "/simulate/economy/budget-window",
                "/simulate/economy/budget-window",
                b'{"status": "submitted", "batch_job_id": "batch-1"}',
                {"job_state": "submitted", "batch_job_id": "batch-1"},
                body=BUDGET_WINDOW_BODY,
            ),
            id="batch-records-batch-id",
        ),
        pytest.param(
            ForwardedCase(
                "POST",
                "/simulate/economy/comparison",
                "/simulate/economy/comparison",
                b'{"job_id": "fc-1"}',
                {"job_id": "fc-1"},
                body={"country": "us", "scope": "macro", "reform": {}},
            ),
            id="submission-without-status",
        ),
        pytest.param(
            ForwardedCase("GET", "/jobs/fc-1", "/jobs/{job_id}", b"not json", {}),
            id="unparsed-poll",
        ),
        pytest.param(
            ForwardedCase(
                "GET",
                "/budget-window-jobs/batch-1",
                "/budget-window-jobs/{batch_job_id}",
                b'{"status": "running", "batch_job_id": "ignored"}',
                {"job_state": "running"},
            ),
            id="batch-poll-records-path-id",
        ),
        pytest.param(
            ForwardedCase("GET", "/versions", "/versions", b"[]", {}),
            id="unparsed-versions",
        ),
    ],
)
def test_backend_response_event_records_every_attribute(
    recorded_events, client, backend, case
):
    backend.responses[(case.method, case.path)] = _backend_response(case.content)

    result = client.request(
        case.method,
        case.path,
        json=case.body,
        headers={REQUEST_ID_HEADER: "request-1"},
    )

    assert result.status_code == 200
    [attributes] = _event_attributes(
        recorded_events, "simulation_entry_backend_response"
    )
    elapsed_ms = attributes.pop("elapsed_ms")
    assert isinstance(elapsed_ms, float)
    assert elapsed_ms >= 0
    identifiers = {
        key: case.path.rsplit("/", 1)[1]
        for key in ("job_id", "batch_job_id")
        if f"{{{key}}}" in case.route
    }
    assert list(attributes.items()) == list(
        {
            "request_id": "request-1",
            "route": case.route,
            "method": case.method,
            "status_code": 200,
            **identifiers,
            **case.expected,
        }.items()
    )


def test_invalid_backend_observability_header_is_dropped(client, backend):
    backend.responses[("GET", "/versions")] = _backend_response(
        b"{}", {OBSERVABILITY_ID_HEADER: "not-an-identifier"}
    )

    result = client.get("/versions")

    assert result.status_code == 200
    assert OBSERVABILITY_ID_HEADER not in result.headers


def test_non_workflow_route_adopts_the_backend_observability_id(client, backend):
    backend.responses[("GET", "/versions")] = _backend_response(
        b"{}", {OBSERVABILITY_ID_HEADER: BACKEND_OBSERVABILITY_ID}
    )

    result = client.get("/versions")

    assert result.headers[OBSERVABILITY_ID_HEADER] == BACKEND_OBSERVABILITY_ID


def test_telemetry_failures_never_change_responses(client, backend, monkeypatch):
    runtime = client.app.state.policyengine_observability

    def failing_set_context(**_attributes):
        msg = "context unavailable"
        raise RuntimeError(msg)

    def failing_mismatch_event(name, **_kwargs):
        if name == "simulation_entry_observability_id_mismatch":
            msg = "event unavailable"
            raise RuntimeError(msg)

    monkeypatch.setattr(runtime, "set_context", failing_set_context)
    monkeypatch.setattr(runtime, "event", failing_mismatch_event)
    backend.responses[("POST", "/simulate/economy/comparison")] = _backend_response(
        b'{"status": "submitted", "job_id": "fc-1"}',
        {OBSERVABILITY_ID_HEADER: BACKEND_OBSERVABILITY_ID},
    )

    result = client.post(
        "/simulate/economy/comparison",
        json={"country": "us", "scope": "macro", "reform": {}},
        headers={OBSERVABILITY_ID_HEADER: ENTRY_OBSERVABILITY_ID},
    )

    assert result.status_code == 200
    assert result.json() == {"status": "submitted", "job_id": "fc-1"}
    assert result.headers[OBSERVABILITY_ID_HEADER] == ENTRY_OBSERVABILITY_ID


def test_observability_id_mismatch_is_reported_without_identifiers(
    recorded_events, client, backend
):
    backend.responses[("POST", "/simulate/economy/comparison")] = _backend_response(
        b"{}", {OBSERVABILITY_ID_HEADER: BACKEND_OBSERVABILITY_ID}
    )

    client.post(
        "/simulate/economy/comparison",
        json={"country": "us", "scope": "macro", "reform": {}},
        headers={OBSERVABILITY_ID_HEADER: ENTRY_OBSERVABILITY_ID},
    )

    assert _event_attributes(
        recorded_events, "simulation_entry_observability_id_mismatch"
    ) == [{"backend": "old_gateway", "reason": "downstream_identifier_differed"}]


def test_unhandled_failures_log_the_batch_identifier(caplog):
    class FailingBackend(FakeBackend):
        @override
        async def request(self, *args, **kwargs):
            msg = "unexpected backend failure"
            raise RuntimeError(msg)

    app = create_app(
        settings=make_settings(),
        backend=FailingBackend(),
        auth_dependency=lambda: None,
    )

    with caplog.at_level(logging.INFO), TestClient(app) as client:
        result = client.get(
            "/budget-window-jobs/batch-1", headers={REQUEST_ID_HEADER: "request-500"}
        )

    assert result.status_code == 500
    record = next(
        item
        for item in caplog.records
        if item.getMessage() == "simulation_entry_unhandled_request"
    )
    assert record.request_id == "request-500"
    assert record.method == "GET"
    assert record.path == "/budget-window-jobs/{batch_job_id}"
    assert record.batch_job_id == "batch-1"
    assert not hasattr(record, "job_id")


def test_unhandled_status_failures_log_the_evaluation_identifier(caplog):
    class MalformedComparison:
        async def get_temporary_report(self, _evaluation_id):
            return object(), ()

    with (
        caplog.at_level(logging.INFO),
        _stage12_client(MalformedComparison()) as client,
    ):
        result = client.get(
            f"/internal/stage12/reports/{EVALUATION_ID}",
            headers={REQUEST_ID_HEADER: "request-500"},
        )

    assert result.status_code == 500
    assert result.text == "Internal Server Error"
    record = next(
        item
        for item in caplog.records
        if item.getMessage() == "simulation_entry_unhandled_request"
    )
    assert record.path == "/internal/stage12/reports/{evaluation_id}"
    assert record.evaluation_id == str(EVALUATION_ID)


def _stage12_client(comparison, **settings_overrides) -> TestClient:
    return TestClient(
        create_app(
            settings=make_settings(**{**STAGE12_SETTINGS, **settings_overrides}),
            backend=FakeBackend(),
            auth_dependency=lambda: None,
            comparison_backend=comparison,
        )
    )


def test_configured_stage12_backend_is_built_from_settings(monkeypatch):
    built = []

    class UnavailableComparisonBackend:
        @classmethod
        def from_settings(cls, settings, *, runtime):
            built.append((settings.stage12_artifact_bucket, runtime))

    monkeypatch.setattr(
        app_module, "Stage12ComparisonBackend", UnavailableComparisonBackend
    )

    with _stage12_client(None) as client:
        submission = client.post("/internal/stage12/reports", json=eligible_payload())
        status = client.get(f"/internal/stage12/reports/{EVALUATION_ID}")

    assert [bucket for bucket, _runtime in built] == ["policyengine-stage12-staging"]
    assert built[0][1] is client.app.state.policyengine_observability
    for result in (submission, status):
        assert result.status_code == 503
        assert result.json() == {"detail": "Stage 12 direct execution is unavailable."}
        assert result.headers["retry-after"] == "10"


def test_unconfigured_stage12_status_is_unavailable():
    app = create_app(
        settings=make_settings(),
        backend=FakeBackend(),
        auth_dependency=lambda: None,
    )

    with TestClient(app) as client:
        result = client.get(f"/internal/stage12/reports/{EVALUATION_ID}")

    assert result.status_code == 503
    assert result.json() == {"detail": "Stage 12 direct execution is unavailable."}
    assert result.headers["retry-after"] == "10"


class SecretBearingError(RuntimeError):
    pass


class FailingComparisonBackend:
    async def submit_temporary_report(self, **_submission):
        msg = "secret request details"
        raise SecretBearingError(msg)

    async def get_temporary_report(self, _evaluation_id):
        msg = "secret report details"
        raise SecretBearingError(msg)


def test_unexpected_stage12_submission_failure_logs_only_the_error_type(caplog):
    with (
        caplog.at_level(logging.INFO),
        _stage12_client(FailingComparisonBackend()) as client,
    ):
        result = client.post(
            "/internal/stage12/reports",
            json=eligible_payload(),
            headers={REQUEST_ID_HEADER: "request-1"},
        )

    assert result.status_code == 503
    assert result.json() == {"detail": "Stage 12 direct execution is unavailable."}
    assert result.headers["retry-after"] == "10"
    [record] = [
        item
        for item in caplog.records
        if item.getMessage() == "stage12_direct_submission_failed"
    ]
    assert record.name == "policyengine_simulation_entry.app"
    assert record.levelno == logging.ERROR
    assert record.exc_info is None
    assert record.request_id == "request-1"
    assert record.error_type == "SecretBearingError"
    assert "secret" not in caplog.text


def test_unexpected_stage12_status_failure_logs_only_the_error_type(caplog):
    with (
        caplog.at_level(logging.INFO),
        _stage12_client(FailingComparisonBackend()) as client,
    ):
        result = client.get(
            f"/internal/stage12/reports/{EVALUATION_ID}",
            headers={REQUEST_ID_HEADER: "request-1"},
        )

    assert result.status_code == 503
    assert result.json() == {"detail": "Stage 12 report status is unavailable."}
    assert result.headers["retry-after"] == "10"
    [record] = [
        item
        for item in caplog.records
        if item.getMessage() == "stage12_direct_status_failed"
    ]
    assert record.name == "policyengine_simulation_entry.app"
    assert record.levelno == logging.ERROR
    assert record.exc_info is None
    assert record.request_id == "request-1"
    assert record.evaluation_id == str(EVALUATION_ID)
    assert record.error_type == "SecretBearingError"
    assert "secret" not in caplog.text


@pytest.mark.parametrize("status_code", [200, 202, 400, 500])
def test_automatic_dispatch_runs_only_for_accepted_submissions(status_code):
    class RecordingComparison:
        def __init__(self):
            self.calls = []

        async def dispatch_after_production(self, **call):
            self.calls.append(call)

    comparison = RecordingComparison()
    backend = FakeBackend()
    backend.responses[("POST", "/simulate/economy/comparison")] = BackendResponse(
        status_code=status_code,
        content=b'{"job_id": "fc-1"}',
        headers={"content-type": "application/json"},
    )
    app = create_app(
        settings=make_settings(**STAGE12_SETTINGS),
        backend=backend,
        auth_dependency=lambda: None,
        comparison_backend=comparison,
    )

    with TestClient(app) as client:
        result = client.post(
            "/simulate/economy/comparison",
            json={"country": "us", "scope": "macro", "reform": {}},
        )

    assert result.status_code == status_code
    assert len(comparison.calls) == (1 if status_code in {200, 202} else 0)
