"""Tests for the redacted, bounded causes persisted on Stage 12 failures."""

from __future__ import annotations

import pytest
from policyengine_simulation_contract.spm import SPMComparisonProvenance
from pydantic import BaseModel, ValidationError
from sqlalchemy.exc import OperationalError

from policyengine_simulation_executor.stage12_runtime.failure_causes import (
    MAX_FAILURE_SUMMARY_LENGTH,
    Stage12FailureCause,
    StageProgress,
    failure_summary,
    record_failure_cause,
)

DATABASE_PASSWORD = "pooler-password-value"
DATABASE_URL = (
    f"postgresql://postgres.project:{DATABASE_PASSWORD}"
    "@pooler.example.com:5432/postgres"
)
SESSION_LIMIT_MESSAGE = (
    'connection to server at "pooler.example.com" (10.0.0.1), port 5432 failed: '
    "FATAL: (EMAXCONNSESSION) max clients reached in session mode - "
    "max clients are limited to pool_size: 15"
)


class DriverOperationalError(Exception):
    """Stands in for the database driver's own connection error."""


class RecordingRuntime:
    def __init__(self) -> None:
        self.recorded_errors: list[tuple[Exception, bool]] = []

    def record_exception(self, error, *, handled, status_code=None) -> None:
        self.recorded_errors.append((error, handled))


def _raised(error: Exception) -> Exception:
    try:
        raise error
    except Exception as caught:  # noqa: BLE001
        return caught


def test_summary_names_type_stage_role_and_message() -> None:
    progress = StageProgress(role="reform")

    with pytest.raises(ValueError) as caught, progress.stage("stage12_calculation"):
        raise ValueError("Variable 'country' not found in household data")

    assert failure_summary(caught.value, progress=progress) == (
        "ValueError in stage12_calculation (reform): "
        "Variable 'country' not found in household data"
    )


def test_summary_names_the_last_finished_stage_for_a_failure_between_stages() -> None:
    progress = StageProgress(role="baseline")
    with progress.stage("stage12_dataset_load"):
        pass

    summary = failure_summary(KeyError("region"), progress=progress)

    assert summary == "KeyError after stage12_dataset_load (baseline): 'region'"


def test_outer_stage_does_not_hide_the_finished_inner_stage() -> None:
    progress = StageProgress()
    with (
        progress.stage("stage12_credential_setup"),
        progress.stage("stage12_dataset_load"),
    ):
        pass

    assert progress.stage_name == "stage12_dataset_load"
    assert progress.stage_finished


def test_database_url_credentials_are_redacted_from_the_summary() -> None:
    error = RuntimeError(f"could not connect using {DATABASE_URL}")

    summary = failure_summary(error)

    assert DATABASE_PASSWORD not in summary
    assert "postgres.project" not in summary
    assert "postgresql://[REDACTED]@pooler.example.com:5432/postgres" in summary


def test_connection_string_password_assignment_is_redacted() -> None:
    error = RuntimeError(
        f"invalid dsn: host=pooler.example.com user=postgres password={DATABASE_PASSWORD}"
    )

    summary = failure_summary(error)

    assert DATABASE_PASSWORD not in summary
    assert "password=[REDACTED]" in summary


def test_secret_environment_values_are_redacted(monkeypatch) -> None:
    monkeypatch.setenv("STAGE12_DATABASE_URL", DATABASE_URL)
    monkeypatch.setenv("HUGGING_FACE_TOKEN", "private-dataset-read-credential")
    error = RuntimeError(
        f"authentication failed for {DATABASE_PASSWORD} "
        "using private-dataset-read-credential"
    )

    summary = failure_summary(error)

    assert DATABASE_PASSWORD not in summary
    assert "private-dataset-read-credential" not in summary
    assert summary == (
        "RuntimeError: authentication failed for [REDACTED] using [REDACTED]"
    )


@pytest.mark.parametrize(
    ("message", "secret"),
    [
        ("Authorization: Bearer abc.def-ghi", "abc.def-ghi"),
        (
            "download https://storage.example.com/data.h5?X-Goog-Signature=signed123 failed",
            "signed123",
        ),
        ("token rejected: hf_abcdefghijklmnopqrstuvwxyz0123", "hf_abcdefghij"),
        ("api_key=very-private-key rejected", "very-private-key"),
    ],
)
def test_tokens_and_signed_urls_are_redacted(message, secret) -> None:
    summary = failure_summary(RuntimeError(message))

    assert secret not in summary
    assert "[REDACTED]" in summary


def test_validation_input_values_are_not_persisted() -> None:
    class HouseholdIncome(BaseModel):
        employment_income: float

    with pytest.raises(ValidationError) as caught:
        HouseholdIncome.model_validate({"employment_income": "household-value-123"})

    summary = failure_summary(caught.value)

    assert "household-value-123" not in summary
    assert summary.startswith("ValidationError: 1 validation error for HouseholdIncome")
    assert "employment_income: Input should be a valid number" in summary


def test_null_spm_provenance_validation_failure_is_specific() -> None:
    progress = StageProgress()
    with (
        pytest.raises(ValidationError) as caught,
        progress.stage("stage12_aggregate_artifact_write"),
    ):
        SPMComparisonProvenance.model_validate(None)

    summary = failure_summary(caught.value, progress=progress)

    assert summary == (
        "ValidationError in stage12_aggregate_artifact_write: "
        "1 validation error for SPMComparisonProvenance: "
        "Input should be a valid dictionary or instance "
        "of SPMComparisonProvenance [model_type]"
    )


def test_stringified_validation_input_values_are_redacted() -> None:
    error = RuntimeError(
        "1 validation error for Simulation\n  Value error [type=value_error, "
        "input_value={'income': 52000.0}, input_type=dict]"
    )

    summary = failure_summary(error)

    assert "52000" not in summary
    assert "input_value=[REDACTED], input_type=dict" in summary


def test_database_errors_report_the_driver_cause_without_parameters() -> None:
    error = OperationalError(
        "INSERT INTO stage12_eval_reports (error_summary) VALUES (%(summary)s)",
        {"summary": "stored-value-that-must-not-leak"},
        DriverOperationalError(SESSION_LIMIT_MESSAGE),
    )

    summary = failure_summary(
        error, progress=_progress_in("stage12_child_state_create")
    )

    assert "stored-value-that-must-not-leak" not in summary
    assert "Background on this error" not in summary
    assert summary == (
        "OperationalError in stage12_child_state_create: "
        f"DriverOperationalError: {SESSION_LIMIT_MESSAGE}"
    )


def test_summary_is_bounded_to_the_persisted_column_length() -> None:
    summary = failure_summary(
        ValueError("x" * 2_000), progress=StageProgress(role="reform")
    )

    assert len(summary) == MAX_FAILURE_SUMMARY_LENGTH
    assert summary.endswith("...")


def test_unprintable_errors_still_produce_a_summary() -> None:
    class UnprintableError(Exception):
        def __str__(self) -> str:
            raise RuntimeError("cannot render")

    assert failure_summary(UnprintableError()) == "UnprintableError"


def test_recorded_cause_is_redacted_and_keeps_the_original_stack() -> None:
    runtime = RecordingRuntime()
    error = _raised(RuntimeError(f"could not connect using {DATABASE_URL}"))
    summary = failure_summary(error)

    record_failure_cause(runtime, error, summary)

    [(recorded, handled)] = runtime.recorded_errors
    assert isinstance(recorded, Stage12FailureCause)
    assert handled
    assert str(recorded) == summary
    assert DATABASE_PASSWORD not in str(recorded)
    assert recorded.__traceback__ is error.__traceback__


def test_recording_failures_never_escape() -> None:
    class FailingRuntime:
        def record_exception(self, *_args, **_kwargs) -> None:
            raise RuntimeError("exporter unavailable")

    record_failure_cause(FailingRuntime(), ValueError("cause"), "ValueError: cause")
    record_failure_cause(None, ValueError("cause"), "ValueError: cause")


def _progress_in(stage_name: str) -> StageProgress:
    progress = StageProgress()
    with pytest.raises(RuntimeError), progress.stage(stage_name):
        raise RuntimeError("stage failed")
    return progress
