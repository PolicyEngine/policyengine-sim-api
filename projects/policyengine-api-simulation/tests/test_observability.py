import json
import os

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from policyengine_observability import (
    REQUEST_ID_HEADER,
    ObservabilityRuntime,
    set_observability_runtime,
)
from policyengine_observability.runtime import REQUEST_LOGGER

from policyengine_api_simulation.observability import (
    LOG_DESTINATIONS,
    SERVICE_NAME,
    configure_process_observability,
    init_simulation_observability,
)


OBSERVABILITY_ENV_KEYS = (
    "OBSERVABILITY_PLATFORM",
    "OBSERVABILITY_SERVICE_ROLE",
    "OBSERVABILITY_RUNTIME_ROLE",
    "OBSERVABILITY_MODAL_APP_NAME",
    "OBSERVABILITY_MODAL_FUNCTION_NAME",
    "OBSERVABILITY_LOG_DESTINATIONS",
    "OTEL_ENABLED",
    "GOOGLE_CLOUD_PROJECT",
    "MODAL_ENVIRONMENT",
)


@pytest.fixture(autouse=True)
def reset_observability_runtime():
    saved_env = {key: os.environ.get(key) for key in OBSERVABILITY_ENV_KEYS}
    for key in OBSERVABILITY_ENV_KEYS:
        os.environ.pop(key, None)
    yield
    set_observability_runtime(ObservabilityRuntime.disabled())
    for key, value in saved_env.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


def test_configure_process_observability_sets_modal_metadata():
    configure_process_observability(
        platform="modal",
        service_role="simulation_worker",
        runtime_role="worker",
        modal_app_name="policyengine-simulation-py4-19-1",
        modal_function_name="run_simulation",
    )

    assert os.environ["OBSERVABILITY_PLATFORM"] == "modal"
    assert os.environ["OBSERVABILITY_SERVICE_ROLE"] == "simulation_worker"
    assert os.environ["OBSERVABILITY_RUNTIME_ROLE"] == "worker"
    assert (
        os.environ["OBSERVABILITY_MODAL_APP_NAME"] == "policyengine-simulation-py4-19-1"
    )
    assert os.environ["OBSERVABILITY_MODAL_FUNCTION_NAME"] == "run_simulation"


def test_configure_process_observability_overwrites_stale_metadata():
    os.environ["OBSERVABILITY_SERVICE_ROLE"] = "old_role"
    os.environ["OBSERVABILITY_MODAL_FUNCTION_NAME"] = "old_function"

    configure_process_observability(
        platform="modal",
        service_role="budget_window_worker",
        modal_app_name="policyengine-simulation-py4-19-1",
        modal_function_name="run_budget_window_batch",
    )

    assert os.environ["OBSERVABILITY_SERVICE_ROLE"] == "budget_window_worker"
    assert os.environ["OBSERVABILITY_RUNTIME_ROLE"] == "budget_window_worker"
    assert os.environ["OBSERVABILITY_MODAL_FUNCTION_NAME"] == "run_budget_window_batch"


def test_init_simulation_observability_forces_stdout_and_disables_exports():
    os.environ["OTEL_ENABLED"] = "true"
    os.environ["OBSERVABILITY_LOG_DESTINATIONS"] = "google_cloud_logging"
    os.environ["GOOGLE_CLOUD_PROJECT"] = "policyengine-prod"
    os.environ["MODAL_ENVIRONMENT"] = "staging"

    app = FastAPI()
    runtime = init_simulation_observability(app, service_role="modal_gateway")

    assert app.state.policyengine_observability is runtime
    assert runtime.config.service_name == SERVICE_NAME
    assert runtime.config.service_role == "modal_gateway"
    assert runtime.config.environment == "staging"
    assert runtime.config.log_destinations == LOG_DESTINATIONS
    assert runtime.config.otel_enabled is False
    assert runtime.config.google_cloud_project is None


def test_fastapi_observability_emits_structured_request_log(monkeypatch):
    app = FastAPI()

    @app.get("/health")
    def health():
        return {"status": "healthy"}

    init_simulation_observability(app, service_role="api")

    records = []
    monkeypatch.setattr(
        REQUEST_LOGGER,
        "info",
        lambda message: records.append(json.loads(message)),
    )

    response = TestClient(app).get(
        "/health",
        headers={REQUEST_ID_HEADER: "request-123"},
    )

    assert response.status_code == 200
    assert len(records) == 1
    record = records[0]
    assert record["schema_version"] == "policyengine.observability.request.v1"
    assert record["event"] == "http_request_completed"
    assert record["service_name"] == SERVICE_NAME
    assert record["service_role"] == "api"
    assert record["request_id"] == "request-123"
    assert record["method"] == "GET"
    assert record["route"] == "/health"
    assert record["status_code"] == 200
    assert record["logfire_status"] == "legacy_candidate_for_replacement"
    assert record["logfire_replacement_candidate"] == "policyengine-observability"
