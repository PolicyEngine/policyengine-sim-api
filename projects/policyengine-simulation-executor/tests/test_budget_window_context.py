"""Tests for budget-window batch context helpers."""

from src.modal.budget_window_context import (
    build_batch_context,
    build_child_simulation_request,
)
from src.modal.gateway.models import BudgetWindowBatchRequest, PolicyEngineBundle


def _build_parent_payload():
    request = BudgetWindowBatchRequest(
        country="us",
        region="us",
        start_year="2026",
        window_size=3,
        max_parallel=2,
        scope="macro",
        reform={},
        _telemetry={
            "run_id": "batch-run-123",
            "process_id": "proc-123",
            "capture_mode": "disabled",
        },
    )
    payload = request.model_dump(mode="json", exclude={"telemetry"})
    payload["version"] = "1.500.0"
    payload["_telemetry"] = request.telemetry.model_dump(mode="json")
    payload["_metadata"] = {
        "resolved_version": "1.500.0",
        "resolved_app_name": "policyengine-simulation-py4-10-0",
        "policyengine_bundle": PolicyEngineBundle(model_version="1.500.0").model_dump(
            mode="json"
        ),
    }
    return request, payload


def test_build_batch_context_extracts_request_and_metadata():
    _, payload = _build_parent_payload()

    context = build_batch_context(payload, batch_job_id="parent-123")

    assert context.batch_job_id == "parent-123"
    assert context.request.country == "us"
    assert context.request.region == "us"
    assert context.request.start_year == "2026"
    assert context.request.window_size == 3
    assert context.request.max_parallel == 2
    assert context.request.telemetry is not None
    assert context.request.telemetry.run_id == "batch-run-123"
    assert context.resolved_version == "1.500.0"
    assert context.resolved_app_name == "policyengine-simulation-py4-10-0"
    assert context.bundle == PolicyEngineBundle(model_version="1.500.0")


def test_build_child_simulation_request_removes_batch_only_fields():
    _, payload = _build_parent_payload()
    context = build_batch_context(payload, batch_job_id="parent-123")

    child_request = build_child_simulation_request(
        context,
        simulation_year="2028",
    )

    assert child_request.simulation_year == "2028"
    assert child_request.payload["time_period"] == "2028"
    assert child_request.payload["region"] == "us"
    assert child_request.payload["scope"] == "macro"
    assert "_telemetry" in child_request.payload
    assert "version" not in child_request.payload
    assert "start_year" not in child_request.payload
    assert "window_size" not in child_request.payload
    assert "max_parallel" not in child_request.payload
    assert "target" not in child_request.payload
    assert "_metadata" not in child_request.payload
