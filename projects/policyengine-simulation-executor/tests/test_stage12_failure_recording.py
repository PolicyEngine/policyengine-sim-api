"""Tests for reporting Stage 12 failures without an observability runtime."""

from __future__ import annotations

import logging

from policyengine_simulation_executor.stage12_runtime import failure_recording
from policyengine_simulation_executor.stage12_runtime.failure_causes import (
    StageProgress,
)
from policyengine_simulation_executor.stage12_runtime.failure_recording import (
    persist_failure_record,
    report_original_failure,
)
from policyengine_simulation_executor.stage12_runtime.failures import (
    ERROR_SUMMARY_MAX_LENGTH,
    redacted_failure_detail,
)


def test_original_failure_is_logged_when_no_observability_runtime_is_available(
    caplog,
) -> None:
    calculation_error = ValueError("country column missing from household data")

    with caplog.at_level(logging.ERROR, logger=failure_recording.__name__):
        report_original_failure(
            calculation_error,
            runtime=None,
            failure_scope="simulation_execution",
            attributes={"simulation_role": "baseline"},
        )

    [record] = caplog.records
    assert record.levelno == logging.ERROR
    assert record.getMessage() == "Stage 12 execution failed (simulation_execution)"
    assert record.exc_info[1] is calculation_error


def test_unwritable_failure_record_is_logged_when_no_runtime_is_available(
    caplog,
) -> None:
    persistence_error = ConnectionError("database unavailable")

    def fail_to_write_record() -> None:
        raise persistence_error

    with caplog.at_level(logging.ERROR, logger=failure_recording.__name__):
        persist_failure_record(
            fail_to_write_record,
            runtime=None,
            failure_scope="report_coordination",
        )

    [record] = caplog.records
    assert record.levelno == logging.ERROR
    assert record.getMessage() == (
        "Stage 12 failure record could not be persisted (report_coordination)"
    )
    assert record.exc_info[1] is persistence_error


def test_written_failure_record_returns_what_the_write_returned() -> None:
    worker_cause = "ValueError in stage12_calculation (baseline): country missing"

    def keep_worker_cause() -> str:
        return worker_cause

    kept_cause = persist_failure_record(
        keep_worker_cause,
        runtime=None,
        failure_scope="child_wait",
    )

    assert kept_cause == worker_cause


def test_a_long_unexpected_failure_keeps_its_correlation_id() -> None:
    progress = StageProgress(role="baseline")
    with progress.stage("stage12_calculation"):
        pass

    detail = redacted_failure_detail(
        RuntimeError("household data rejected: " + "row " * 400),
        default_code="simulation_execution_failed",
        progress=progress,
    )

    assert detail.correlation_id is not None
    suffix = f" (correlation_id={detail.correlation_id})"
    assert len(detail.error_summary) == ERROR_SUMMARY_MAX_LENGTH
    assert detail.error_summary.endswith(f"...{suffix}")
    assert detail.error_summary.startswith(
        "RuntimeError after stage12_calculation (baseline): household data rejected"
    )
