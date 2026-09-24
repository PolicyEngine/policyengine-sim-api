import pytest
from pydantic import ValidationError

from policyengine_simulation_observability.telemetry import (
    TelemetryEnvelope,
    remote_context,
    split_internal_payload,
)


def test_split_internal_payload__removes_internal_fields():
    payload = {
        "country": "us",
        "scope": "macro",
        "_metadata": {"submission_claim_id": "proc-123"},
        "_telemetry": {
            "observability_id": "run-123",
            "submission_claim_id": "proc-123",
            "capture_mode": "disabled",
        },
        "_observability_context": {
            "traceparent": "00-11111111111111111111111111111111-2222222222222222-01",
            "captured_at": "2026-09-22T00:00:00Z",
            "request_id": "request-123",
        },
    }

    simulation_params, telemetry, metadata = split_internal_payload(payload)

    assert "_metadata" not in simulation_params
    assert "_telemetry" not in simulation_params
    assert "_observability_context" not in simulation_params
    assert simulation_params == {"country": "us", "scope": "macro"}
    assert telemetry == TelemetryEnvelope(
        observability_id="run-123",
        submission_claim_id="proc-123",
        capture_mode="disabled",
    )
    assert metadata == {"submission_claim_id": "proc-123"}


def test_remote_context_validates_and_serializes_allowlisted_fields():
    context = remote_context(
        {
            "_observability_context": {
                "traceparent": "00-11111111111111111111111111111111-2222222222222222-01",
                "captured_at": "2026-09-22T00:00:00Z",
                "request_id": "request-123",
                "job_id": "job-123",
            }
        }
    )

    assert context is not None
    assert context["request_id"] == "request-123"
    assert context["job_id"] == "job-123"
    assert context["captured_at"] == "2026-09-22 00:00:00+00:00"


def test_remote_context_rejects_malformed_or_extra_values():
    assert remote_context({"_observability_context": {"captured_at": "bad"}}) is None
    assert (
        remote_context(
            {
                "_observability_context": {
                    "captured_at": "2026-09-22T00:00:00Z",
                    "household": {"people": {}},
                }
            }
        )
        is None
    )


def test_split_internal_payload__tolerates_missing_internal_fields():
    payload = {"country": "us", "scope": "macro"}

    simulation_params, telemetry, metadata = split_internal_payload(payload)

    assert simulation_params == payload
    assert telemetry is None
    assert metadata is None


def test_split_internal_payload__drops_malformed_telemetry_without_failing_work():
    payload = {
        "country": "us",
        "scope": "macro",
        "_telemetry": {"capture_mode": "not-supported"},
    }

    simulation_params, telemetry, metadata = split_internal_payload(payload)

    assert simulation_params == {"country": "us", "scope": "macro"}
    assert telemetry is None
    assert metadata is None


def test_split_internal_payload__normalizes_current_production_api_telemetry():
    payload = {
        "country": "us",
        "scope": "macro",
        "_telemetry": {
            "run_id": "00000000-0000-4000-8000-000000000001",
            "process_id": "job-123",
            "request_id": "request-123",
            "traceparent": (
                "00-11111111111111111111111111111111-2222222222222222-01"
            ),
            "capture_mode": "disabled",
        },
    }

    simulation_params, telemetry, metadata = split_internal_payload(payload)

    assert simulation_params == {"country": "us", "scope": "macro"}
    assert telemetry == TelemetryEnvelope(
        observability_id="00000000-0000-4000-8000-000000000001",
        submission_claim_id="job-123",
        capture_mode="disabled",
    )
    assert metadata is None


def test_telemetry_rejects_conflicting_previous_and_current_identifiers():
    with pytest.raises(ValidationError, match="run_id and observability_id must match"):
        TelemetryEnvelope.model_validate(
            {
                "run_id": "00000000-0000-4000-8000-000000000001",
                "observability_id": "00000000-0000-4000-8000-000000000002",
            }
        )
