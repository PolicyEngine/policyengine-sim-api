"""Tests for gateway error redaction."""

from __future__ import annotations

import re
from unittest.mock import Mock

from policyengine_simulation_observability import errors as errors_module


CORRELATION_RE = re.compile(r"correlation_id=([0-9a-f]{32})")


def test_log_and_redact_exception_records_runtime_event(caplog):
    runtime = Mock()
    exc = RuntimeError("signed URL containing secret material")

    with caplog.at_level(
        "ERROR", logger="policyengine_simulation_observability.errors"
    ):
        message = errors_module.log_and_redact_exception(
            exc,
            runtime=runtime,
            scope="test_scope",
            context={"job_id": "abc"},
        )

    match = CORRELATION_RE.search(message)
    assert match is not None
    assert "secret material" not in message
    runtime.record_exception.assert_called_once_with(exc, handled=True, status_code=500)
    attributes = runtime.event.call_args.kwargs["attributes"]
    assert attributes == {
        "correlation_id": match.group(1),
        "scope": "test_scope",
        "error_type": "RuntimeError",
        "job_id": "abc",
    }
    assert any(match.group(1) in record.getMessage() for record in caplog.records)


def test_log_and_redact_exception_survives_runtime_failure(caplog):
    runtime = Mock()
    runtime.record_exception.side_effect = RuntimeError("export failed")
    exc = ValueError("private parameter value")

    with caplog.at_level(
        "ERROR", logger="policyengine_simulation_observability.errors"
    ):
        message = errors_module.log_and_redact_exception(
            exc,
            runtime=runtime,
            scope="fallback",
        )

    assert "private parameter value" not in message
    assert message.startswith("Simulation failed")
    assert any("fallback" in record.getMessage() for record in caplog.records)
