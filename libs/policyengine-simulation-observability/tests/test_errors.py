"""Tests for gateway error-redaction helpers."""

from __future__ import annotations

import re

from policyengine_simulation_observability import errors as errors_module


CORRELATION_RE = re.compile(r"correlation_id=([0-9a-f]{32})")


def test_log_and_redact_exception_emits_correlation_id(monkeypatch):
    class _FakeLogfire:
        def __init__(self):
            self.calls = []

        def exception(self, *args, **kwargs):
            self.calls.append((args, kwargs))

    recorded_errors = []
    recorded_events = []
    fake_logfire = _FakeLogfire()
    monkeypatch.setattr(
        errors_module,
        "record_error",
        lambda exc, **kwargs: recorded_errors.append((exc, kwargs)),
    )
    monkeypatch.setattr(
        errors_module,
        "record_event",
        lambda event, **kwargs: recorded_events.append((event, kwargs)),
    )
    monkeypatch.setattr(errors_module, "_logfire", fake_logfire)
    monkeypatch.setattr(errors_module, "logfire_is_configured", lambda: True)

    exc = RuntimeError(
        "Signed GCS URL https://storage.googleapis.com/foo?token=SECRET "
        "failed to resolve"
    )
    message = errors_module.log_and_redact_exception(
        exc, scope="test_scope", context={"job_id": "abc"}
    )

    match = CORRELATION_RE.search(message)
    assert match is not None, message

    assert "SECRET" not in message
    assert "token=" not in message
    assert message.startswith("Simulation failed")

    assert recorded_errors == [
        (
            exc,
            {
                "handled": True,
                "status_code": 500,
            },
        )
    ]
    assert len(recorded_events) == 1
    # Correlation id must appear in the server-side structured event.
    event, kwargs = recorded_events[0]
    assert event == "gateway_error_redacted"
    assert kwargs["correlation_id"] == match.group(1)
    assert kwargs["scope"] == "test_scope"
    assert kwargs["job_id"] == "abc"
    assert kwargs["error_type"] == "RuntimeError"
    assert kwargs["logfire_status"] == "legacy_candidate_for_replacement"
    assert kwargs["logfire_replacement_candidate"] == "policyengine-observability"

    assert len(fake_logfire.calls) == 1
    _, logfire_kwargs = fake_logfire.calls[0]
    assert logfire_kwargs["correlation_id"] == match.group(1)
    assert logfire_kwargs["scope"] == "test_scope"
    assert logfire_kwargs["job_id"] == "abc"
    assert logfire_kwargs["logfire_status"] == "legacy_candidate_for_replacement"
    assert logfire_kwargs["logfire_replacement_candidate"] == (
        "policyengine-observability"
    )


def test_log_and_redact_exception_always_logs_to_stdlib(monkeypatch, caplog):
    """The stdlib log line must fire even when every structured sink no-ops.

    record_error/record_event silently do nothing on a disabled runtime and
    Logfire is unconfigured here, so without the unconditional stdlib log the
    correlation id handed to the caller would point at nothing server-side.
    """
    monkeypatch.setattr(errors_module, "record_error", lambda *a, **k: None)
    monkeypatch.setattr(errors_module, "record_event", lambda *a, **k: None)
    monkeypatch.setattr(errors_module, "logfire_is_configured", lambda: False)
    exc = ValueError("secret-parameter-name")
    with caplog.at_level("ERROR", logger="policyengine_simulation_observability.errors"):
        message = errors_module.log_and_redact_exception(exc, scope="fallback")

    match = CORRELATION_RE.search(message)
    assert match is not None, message
    assert "secret-parameter-name" not in message
    assert message.startswith("Simulation failed")

    # Exactly one guaranteed server-side record carrying the correlation id
    # and the full exception (message + stack) for operators.
    stdlib_records = [
        record for record in caplog.records if "fallback" in record.getMessage()
    ]
    assert len(stdlib_records) == 1
    record = stdlib_records[0]
    assert match.group(1) in record.getMessage()
    assert record.exc_info is not None
    assert record.exc_info[1] is exc


def test_log_and_redact_exception_survives_structured_sink_failure(monkeypatch, caplog):
    def _raise(*args, **kwargs):
        raise RuntimeError("observability failed")

    monkeypatch.setattr(errors_module, "record_error", _raise)
    monkeypatch.setattr(errors_module, "logfire_is_configured", lambda: False)
    exc = ValueError("secret-parameter-name")
    with caplog.at_level("ERROR", logger="policyengine_simulation_observability.errors"):
        message = errors_module.log_and_redact_exception(exc, scope="fallback")

    assert "secret-parameter-name" not in message
    assert message.startswith("Simulation failed")
    assert any("fallback" in record.message for record in caplog.records)
