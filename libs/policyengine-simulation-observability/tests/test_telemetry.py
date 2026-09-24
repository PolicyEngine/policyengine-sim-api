import pytest
from policyengine_simulation_observability.telemetry import (
    TelemetryEnvelope,
    apply_remote_context,
    remote_context,
    split_internal_payload,
)


def test_split_internal_payload__removes_internal_fields():
    payload = {
        "country": "us",
        "scope": "macro",
        "_metadata": {"submission_claim_id": "proc-123"},
        "_telemetry": {
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
                    "observability_id": "not-a-uuid",
                }
            }
        )
        is None
    )
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


def test_apply_remote_context_sets_the_runtime_diagnostic_identifier():
    class Runtime:
        def __init__(self):
            self.context = {}

        def set_context(self, **values):
            self.context.update(values)

    runtime = Runtime()
    params = {
        "_observability_context": {
            "captured_at": "2026-09-22T00:00:00Z",
            "observability_id": "00000000-0000-4000-8000-000000000001",
        }
    }

    propagated = apply_remote_context(runtime, params)

    assert propagated is not None
    assert runtime.context == {
        "observability_id": "00000000-0000-4000-8000-000000000001"
    }


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


@pytest.mark.parametrize(
    "field", ["observability_id", "run_id", "request_id", "traceparent"]
)
def test_telemetry_discards_noncanonical_context_fields(field):
    telemetry = TelemetryEnvelope.model_validate(
        {field: "identifier", "submission_claim_id": "claim-123"}
    )

    assert telemetry.submission_claim_id == "claim-123"
    assert field not in telemetry.model_dump()


def test_telemetry_maps_legacy_process_id_to_submission_claim_id():
    telemetry = TelemetryEnvelope.model_validate({"process_id": "legacy-claim-123"})

    assert telemetry.submission_claim_id == "legacy-claim-123"
    assert "process_id" not in telemetry.model_dump()


def test_telemetry_prefers_canonical_submission_claim_id_over_legacy_value():
    telemetry = TelemetryEnvelope.model_validate(
        {
            "process_id": "legacy-claim-123",
            "submission_claim_id": "canonical-claim-456",
        }
    )

    assert telemetry.submission_claim_id == "canonical-claim-456"
