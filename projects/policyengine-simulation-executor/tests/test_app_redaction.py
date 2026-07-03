"""Tests for the Logfire payload redaction helper."""

from __future__ import annotations

from src.modal.logging_redaction import redact_params_for_logging


def test_redact_params_strips_signed_urls_and_reform_bodies():
    """Signed URLs and the reform parameter tree must not reach Logfire."""
    params = {
        "country": "us",
        "scope": "macro",
        "data": "https://storage.googleapis.com/bucket/key?token=SECRET&expiry=123",
        "reform": {"gov.irs.income.bracket[2].rate": {"2024-01-01": 0.45}},
        "baseline": {"gov.irs.income.bracket[2].rate": {"2023-01-01": 0.43}},
        "_telemetry": {"run_id": "run-123", "process_id": "p-1"},
        "_metadata": {"resolved_app_name": "policyengine-simulation-us1-500"},
    }

    redacted = redact_params_for_logging(params)

    # Routing context is preserved.
    assert redacted["country"] == "us"
    assert redacted["scope"] == "macro"
    # Correlation id is preserved, but the rest of the telemetry envelope
    # is not.
    assert redacted["run_id"] == "run-123"
    assert "_telemetry" not in redacted
    assert "_metadata" not in redacted

    # Sensitive fields are stripped entirely.
    assert "data" not in redacted
    assert "reform" not in redacted
    assert "baseline" not in redacted

    # The signed URL value is not present anywhere in the redacted dict.
    assert all("SECRET" not in str(value) for value in redacted.values())


def test_redact_params_tolerates_non_dict_input():
    assert redact_params_for_logging(None) == {}
    assert redact_params_for_logging("string-payload") == {}


def test_redact_params_handles_missing_telemetry():
    params = {"country": "uk", "scope": "macro"}
    assert redact_params_for_logging(params) == params
