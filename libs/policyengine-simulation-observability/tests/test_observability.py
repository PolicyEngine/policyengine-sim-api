import inspect

from fastapi import FastAPI
from fastapi.testclient import TestClient
from policyengine_observability import REQUEST_ID_HEADER
from policyengine_observability import (
    GoogleCloudLogDestination,
    StdoutLogDestination,
)

from policyengine_simulation_observability.observability import (
    build_runtime,
    init_process_observability,
    init_simulation_observability,
    modal_image_environment,
)


def test_modal_runtime_has_explicit_identity_and_remote_logging(monkeypatch):
    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")
    monkeypatch.setenv("OTEL_TRACES_SAMPLER_ARG", "0.01")
    monkeypatch.setenv("OBSERVABILITY_SERVICE_NAMESPACE", "example.stack")
    monkeypatch.setenv("OBSERVABILITY_TRACE_PROJECT_ID", "trace-project")
    monkeypatch.setenv("OBSERVABILITY_LOGGING_PROJECT_ID", "log-project")
    monkeypatch.setenv("OBSERVABILITY_LOG_NAME", "simulation-modal")
    runtime = build_runtime(
        service_name="policyengine-simulation-py5-2-0",
        service_role="simulation_worker",
        platform="modal",
        environment="main",
    )
    try:
        assert runtime.config.service.name == "policyengine-simulation-py5-2-0"
        assert runtime.config.service.namespace == "example.stack"
        assert runtime.config.service.role == "simulation_worker"
        assert runtime.config.deployment.environment == "main"
        assert runtime.config.deployment.platform == "modal"
        assert runtime.config.otel.sampling_ratio == 1.0
        stdout, remote = runtime.config.logging.destinations
        assert isinstance(stdout, StdoutLogDestination)
        assert isinstance(remote, GoogleCloudLogDestination)
        assert remote.project_id == "log-project"
        assert remote.log_name == "simulation-modal"
    finally:
        runtime.shutdown()


def test_cloud_run_runtime_uses_stdout_without_direct_remote_logging(monkeypatch):
    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")
    monkeypatch.delenv("OBSERVABILITY_LOGGING_PROJECT_ID", raising=False)
    monkeypatch.delenv("OBSERVABILITY_LOG_NAME", raising=False)
    runtime = init_process_observability(
        service_name="policyengine-simulation-entry-prod",
        service_role="simulation_entry",
        platform="google_cloud_run",
        environment="prod",
    )
    try:
        assert runtime.config.logging.capture_standard_library is True
        assert len(runtime.config.logging.destinations) == 1
        assert isinstance(runtime.config.logging.destinations[0], StdoutLogDestination)
    finally:
        runtime.shutdown()


def test_modal_image_environment_only_copies_explicit_destinations(monkeypatch):
    for name in (
        "OBSERVABILITY_LOGGING_PROJECT_ID",
        "OBSERVABILITY_LOG_NAME",
        "OTEL_EXPORTER_OTLP_ENDPOINT",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("OBSERVABILITY_LOGGING_PROJECT_ID", "another-project")
    monkeypatch.setenv("OBSERVABILITY_LOG_NAME", "another-log")

    environment = modal_image_environment()

    assert environment["OBSERVABILITY_LOGGING_PROJECT_ID"] == "another-project"
    assert environment["OBSERVABILITY_LOG_NAME"] == "another-log"
    assert "OTEL_EXPORTER_OTLP_ENDPOINT" not in environment
    assert environment["OTEL_EXPORTER_OTLP_PROTOCOL"] == "grpc"
    assert environment["OTEL_TRACES_SAMPLER_ARG"] == "1.0"


def test_fastapi_adapter_preserves_response_and_request_id(monkeypatch):
    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")
    app = FastAPI()

    @app.get("/health")
    def health():
        return {"status": "healthy"}

    runtime = init_simulation_observability(
        app,
        service_name="policyengine-simulation-entry-prod",
        service_role="simulation_entry",
        platform="google_cloud_run",
        environment="prod",
    )
    try:
        response = TestClient(app).get(
            "/health",
            headers={REQUEST_ID_HEADER: "request-123"},
        )
        assert response.status_code == 200
        assert response.json() == {"status": "healthy"}
        assert response.headers[REQUEST_ID_HEADER] == "request-123"
        assert app.state.policyengine_observability is runtime
    finally:
        runtime.shutdown()


def test_observability_setup_failure_does_not_change_fastapi_response(
    monkeypatch,
):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "https://invalid.invalid")
    monkeypatch.setenv("POLICYENGINE_OTEL_GOOGLE_AUDIENCE", "not-a-url")
    app = FastAPI()

    @app.get("/result")
    def result():
        return {"result": 42}

    runtime = init_simulation_observability(
        app,
        service_name="policyengine-simulation-entry-prod",
        service_role="simulation_entry",
        platform="google_cloud_run",
        environment="prod",
    )
    try:
        response = TestClient(app).get("/result")
        assert response.status_code == 200
        assert response.json() == {"result": 42}
    finally:
        runtime.shutdown()


def test_service_identity_arguments_are_required_and_keyword_only():
    parameters = inspect.signature(init_simulation_observability).parameters
    for name in ("service_name", "service_role", "platform", "environment"):
        assert parameters[name].kind is inspect.Parameter.KEYWORD_ONLY
        assert parameters[name].default is inspect.Parameter.empty
