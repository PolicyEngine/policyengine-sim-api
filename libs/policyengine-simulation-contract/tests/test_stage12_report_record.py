"""Characterization tests for the comparison report record's state validation."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from policyengine_simulation_contract.stage12_execution import ComparisonReportRecord
from pydantic import ValidationError

NOW = datetime(2026, 9, 22, tzinfo=UTC)
AGGREGATE_OUTPUT = {
    "aggregate_output_uri": "gs://bucket/aggregate.json",
    "aggregate_output_sha256": "c" * 64,
    "aggregate_schema_version": 1,
}
COMPARISON_OUTPUT = {
    "comparison_output_uri": "gs://bucket/comparison.json",
    "comparison_output_sha256": "d" * 64,
    "comparison_schema_version": 1,
}
SUCCEEDED_REPORT = {
    "status": "succeeded",
    "aggregation_status": "succeeded",
    **AGGREGATE_OUTPUT,
    "completed_at": NOW,
}


def _record_fields(**overrides: Any) -> dict[str, Any]:
    return {
        "evaluation_id": "00000000-0000-0000-0000-000000000001",
        "status": "pending",
        "aggregation_status": "not_started",
        "environment": "staging",
        "calculation_flow": "economy",
        "originating_request_id": "request-1",
        "production_identity": "job-1",
        "worker_version": "5.2.0",
        "modal_application": "policyengine-simulation-v2-py5-2-0",
        "report_coordinator_callable": "coordinate_report",
        "version_manifest_sha256": "a" * 64,
        "policyengine_version": "5.2.0",
        "country_package_name": "policyengine-us",
        "country_package_version": "1.764.6",
        "country": "us",
        "dataset_identity": "populace_us_2024",
        "dataset_uri": "hf://policyengine/data/populace_us_2024.h5@revision",
        "data_package_name": "populace-data",
        "data_package_version": "0.1.0",
        "data_artifact_revision": "revision",
        "created_at": NOW,
        "updated_at": NOW,
        "retention_expires_at": NOW + timedelta(days=30),
        **overrides,
    }


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({}, id="pending"),
        pytest.param(SUCCEEDED_REPORT, id="succeeded"),
        pytest.param({"status": "failed", "error_code": "E"}, id="failed"),
        pytest.param({"status": "skipped", "error_code": "E"}, id="skipped"),
        pytest.param({"status": "running", **AGGREGATE_OUTPUT}, id="running-output"),
        pytest.param({"comparison_status": "pending"}, id="comparison-pending"),
        pytest.param({"comparison_status": "running"}, id="comparison-running"),
        pytest.param(
            {
                "comparison_status": "matched",
                **COMPARISON_OUTPUT,
                "comparison_completed_at": NOW,
            },
            id="comparison-matched",
        ),
        pytest.param(
            {
                "comparison_status": "different",
                **COMPARISON_OUTPUT,
                "comparison_completed_at": NOW,
            },
            id="comparison-different",
        ),
        pytest.param(
            {
                "comparison_status": "failed",
                "comparison_completed_at": NOW,
                "comparison_error_code": "E",
                "comparison_error_summary": "summary",
            },
            id="comparison-failed",
        ),
    ],
)
def test_consistent_report_states_validate(overrides):
    fields = _record_fields(**overrides)

    record = ComparisonReportRecord.model_validate(fields)

    assert record.status == fields["status"]
    assert record.comparison_status == fields.get("comparison_status", "not_requested")


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        pytest.param(
            {"created_at": NOW.replace(tzinfo=None)},
            "comparison retention timestamps must include a timezone",
            id="naive-created-at",
        ),
        pytest.param(
            {"retention_expires_at": NOW},
            "comparison retention must be greater than zero",
            id="zero-retention",
        ),
        pytest.param(
            {"retention_expires_at": NOW + timedelta(days=30, seconds=1)},
            "comparison retention must not exceed 30 days",
            id="long-retention",
        ),
        pytest.param(
            {"aggregate_output_uri": "gs://bucket/aggregate.json"},
            "aggregate output fields must be supplied together",
            id="partial-aggregate-output",
        ),
        pytest.param(
            {**SUCCEEDED_REPORT, "aggregation_status": "running"},
            "a successful report requires successful aggregation",
            id="succeeded-without-aggregation",
        ),
        pytest.param(
            {
                **SUCCEEDED_REPORT,
                "aggregate_output_uri": None,
                "aggregate_output_sha256": None,
                "aggregate_schema_version": None,
            },
            "a successful report requires an aggregate output",
            id="succeeded-without-output",
        ),
        pytest.param(
            {**SUCCEEDED_REPORT, "completed_at": None},
            "a successful report requires completed_at",
            id="succeeded-without-completed-at",
        ),
        pytest.param(
            {"status": "failed"},
            "a failed or skipped report requires an error_code",
            id="failed-without-error-code",
        ),
        pytest.param(
            {"status": "skipped"},
            "a failed or skipped report requires an error_code",
            id="skipped-without-error-code",
        ),
        pytest.param(
            {"comparison_output_uri": "gs://bucket/comparison.json"},
            "an incomplete comparison cannot reference an output artifact",
            id="not-requested-with-output",
        ),
        pytest.param(
            {"comparison_status": "running", "comparison_completed_at": NOW},
            "an incomplete comparison cannot have completed_at",
            id="running-with-completed-at",
        ),
        pytest.param(
            {"comparison_status": "pending", "comparison_error_code": "E"},
            "an incomplete comparison cannot have an error_code",
            id="pending-with-error-code",
        ),
        pytest.param(
            {"comparison_error_summary": "summary"},
            "an incomplete comparison cannot have an error_summary",
            id="not-requested-with-error-summary",
        ),
        pytest.param(
            {"comparison_status": "matched", "comparison_completed_at": NOW},
            "a completed comparison requires an output artifact",
            id="matched-without-output",
        ),
        pytest.param(
            {"comparison_status": "different", **COMPARISON_OUTPUT},
            "a completed comparison requires completed_at",
            id="different-without-completed-at",
        ),
        pytest.param(
            {
                "comparison_status": "matched",
                **COMPARISON_OUTPUT,
                "comparison_completed_at": NOW,
                "comparison_error_code": "E",
            },
            "a completed comparison cannot have an error_code",
            id="matched-with-error-code",
        ),
        pytest.param(
            {
                "comparison_status": "matched",
                **COMPARISON_OUTPUT,
                "comparison_completed_at": NOW,
                "comparison_error_summary": "summary",
            },
            "a completed comparison cannot have an error_summary",
            id="matched-with-error-summary",
        ),
        pytest.param(
            {
                "comparison_status": "failed",
                "comparison_output_sha256": "d" * 64,
                "comparison_completed_at": NOW,
                "comparison_error_code": "E",
            },
            "a failed comparison cannot reference an output artifact",
            id="failed-comparison-with-output",
        ),
        pytest.param(
            {"comparison_status": "failed", "comparison_error_code": "E"},
            "a failed comparison requires completed_at",
            id="failed-comparison-without-completed-at",
        ),
        pytest.param(
            {"comparison_status": "failed", "comparison_completed_at": NOW},
            "a failed comparison requires an error_code",
            id="failed-comparison-without-error-code",
        ),
        pytest.param(
            {
                "status": "failed",
                "aggregate_schema_version": 1,
                "comparison_status": "matched",
            },
            "aggregate output fields must be supplied together",
            id="aggregate-checked-before-status",
        ),
        pytest.param(
            {"status": "skipped", "comparison_status": "failed"},
            "a failed or skipped report requires an error_code",
            id="report-checked-before-comparison",
        ),
        pytest.param(
            {
                "comparison_status": "pending",
                **COMPARISON_OUTPUT,
                "comparison_completed_at": NOW,
                "comparison_error_code": "E",
            },
            "an incomplete comparison cannot reference an output artifact",
            id="comparison-output-checked-first",
        ),
    ],
)
def test_inconsistent_report_states_are_rejected(overrides, message):
    with pytest.raises(ValidationError) as error:
        ComparisonReportRecord.model_validate(_record_fields(**overrides))

    assert error.value.error_count() == 1
    assert re.fullmatch(
        f"Value error, {re.escape(message)}", error.value.errors()[0]["msg"]
    )
