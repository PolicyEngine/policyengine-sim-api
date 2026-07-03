from policyengine_simulation_observability.telemetry import (
    TelemetryEnvelope,
    split_internal_payload,
)


def test_split_internal_payload__removes_internal_fields():
    payload = {
        "country": "us",
        "scope": "macro",
        "_metadata": {"process_id": "proc-123"},
        "_telemetry": {
            "run_id": "run-123",
            "process_id": "proc-123",
            "capture_mode": "disabled",
        },
    }

    simulation_params, telemetry, metadata = split_internal_payload(payload)

    assert "_metadata" not in simulation_params
    assert "_telemetry" not in simulation_params
    assert simulation_params == {"country": "us", "scope": "macro"}
    assert telemetry == TelemetryEnvelope(
        run_id="run-123",
        process_id="proc-123",
        capture_mode="disabled",
    )
    assert metadata == {"process_id": "proc-123"}


def test_split_internal_payload__tolerates_missing_internal_fields():
    payload = {"country": "us", "scope": "macro"}

    simulation_params, telemetry, metadata = split_internal_payload(payload)

    assert simulation_params == payload
    assert telemetry is None
    assert metadata is None
