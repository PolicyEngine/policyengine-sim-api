"""Tests for the redacted, bounded causes persisted on Stage 12 failures."""

from __future__ import annotations

import time

import pytest
from policyengine_simulation_contract.spm import SPMComparisonProvenance
from policyengine_simulation_contract.stage12_execution import ErrorSummary
from policyengine_simulation_executor.stage12_runtime import failure_causes
from policyengine_simulation_executor.stage12_runtime.failure_causes import (
    MAX_FAILURE_SUMMARY_LENGTH,
    MAX_REDACTED_MESSAGE_LENGTH,
    Stage12FailureCause,
    StageProgress,
    failure_summary,
    record_failure_cause,
    redact_failure_message,
)
from pydantic import BaseModel, TypeAdapter, ValidationError
from sqlalchemy.exc import IntegrityError, OperationalError

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
# Shapes that made the earlier patterns backtrack quadratically.
PATHOLOGICAL_MESSAGE_UNITS = ("a-", "a.", "eyJ-", "a://", "s://a@")
# Redacting this many pathological characters took several seconds before the
# patterns were made linear.
UNCAPPED_PATHOLOGICAL_LENGTH = 20_000
MAX_PATHOLOGICAL_SECONDS = 1.0


class DriverOperationalError(Exception):
    """Stands in for the database driver's own connection error."""


class DriverIntegrityError(Exception):
    """Stands in for the database driver's constraint violation."""


class RecordingRuntime:
    def __init__(self) -> None:
        self.recorded_errors: list[tuple[Exception, bool]] = []

    def record_exception(self, error, *, handled, status_code=None) -> None:  # noqa: ARG002  # mirrors ObservabilityRuntime.record_exception, which callers pass status_code= by keyword
        self.recorded_errors.append((error, handled))


def _raised(error: Exception) -> Exception:
    with pytest.raises(type(error)) as caught:
        raise error
    return caught.value


def test_summary_names_type_stage_role_and_message() -> None:
    progress = StageProgress(role="reform")
    msg = "Variable 'country' not found in household data"

    with (
        pytest.raises(ValueError, match=msg) as caught,
        progress.stage("stage12_calculation"),
    ):
        raise ValueError(msg)

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
        "invalid dsn: host=pooler.example.com user=postgres "
        f"password={DATABASE_PASSWORD}"
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
            (
                "download https://storage.example.com/data.h5"
                "?X-Goog-Signature=signed123 failed"
            ),
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


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        (
            'request body {"password": "json-password", "user": "runtime"}',
            'request body {"password": [REDACTED], "user": "runtime"}',
        ),
        (
            'request body {"password":"escaped \\"quote\\" password"}',
            'request body {"password":[REDACTED]}',
        ),
        ("options {'token': 'dict-token'}", "options {'token': [REDACTED]}"),
        ("options {'api_key': 'dict-api-key'}", "options {'api_key': [REDACTED]}"),
        (
            "connect(password='two word password') failed",
            "connect(password=[REDACTED]) failed",
        ),
    ],
)
def test_quoted_secret_keys_are_redacted(message, expected) -> None:
    assert redact_failure_message(message) == expected


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("Authorization: Basic YWxpY2U6c2VjcmV0", "Authorization: [REDACTED]"),
        ("Authorization: Token opaque-api-token", "Authorization: [REDACTED]"),
        (
            'Authorization: Digest username="runtime", response="6629fae4"',
            "Authorization: [REDACTED]",
        ),
        ("Authorization: opaque-header-value", "Authorization: [REDACTED]"),
        (
            "Proxy-Authorization: Basic YWxpY2U6c2VjcmV0",
            "Proxy-Authorization: [REDACTED]",
        ),
        (
            "headers {'Authorization': 'Basic YWxpY2U6c2VjcmV0', 'Accept': 'json'}",
            "headers {'Authorization': [REDACTED], 'Accept': 'json'}",
        ),
        ("Cookie: session=opaque-session", "Cookie: [REDACTED]"),
    ],
)
def test_authorization_header_values_are_redacted(message, expected) -> None:
    assert redact_failure_message(message) == expected


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        (
            "GET /bucket/data.h5?X-Goog-Algorithm=GOOG4&X-Goog-Signature=abc failed",
            "GET /bucket/data.h5?[REDACTED] failed",
        ),
        (
            "read bucket/data.h5?X-Amz-Signature=abc rejected",
            "read bucket/data.h5?[REDACTED] rejected",
        ),
        (
            "blob container/data.h5?sv=2024-01-01&sig=abc expired",
            "blob container/data.h5?[REDACTED] expired",
        ),
        (
            "canonical query X-Goog-Signature=abc mismatch",
            "canonical query X-Goog-Signature=[REDACTED] mismatch",
        ),
        ("request sig=abc rejected", "request sig=[REDACTED] rejected"),
    ],
)
def test_signed_query_parameters_without_a_scheme_are_redacted(
    message, expected
) -> None:
    assert redact_failure_message(message) == expected


def test_url_password_containing_an_at_sign_is_redacted_whole() -> None:
    message = "could not connect to postgresql://runtime:pass@word@db.example/postgres"

    assert redact_failure_message(message) == (
        "could not connect to postgresql://[REDACTED]@db.example/postgres"
    )


def test_secret_environment_value_in_an_assignment_is_redacted_once(
    monkeypatch,
) -> None:
    monkeypatch.setenv("STAGE12_DATABASE_URL", DATABASE_URL)

    summary = failure_summary(RuntimeError(f"password={DATABASE_PASSWORD} rejected"))

    assert summary == "RuntimeError: password=[REDACTED] rejected"


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


@pytest.mark.parametrize(
    ("driver_message", "expected_message"),
    [
        (
            (
                'duplicate key value violates unique constraint "reports_pkey"\n'
                "DETAIL:  Key (evaluation_id)=(stored-identifier) already exists."
            ),
            (
                'duplicate key value violates unique constraint "reports_pkey" '
                "DETAIL: Key (evaluation_id)=[REDACTED]"
            ),
        ),
        (
            (
                'duplicate key value violates unique constraint "users_email_key"\n'
                "DETAIL:  Key (lower(email))=(stored@example.com) already exists."
            ),
            (
                'duplicate key value violates unique constraint "users_email_key" '
                "DETAIL: Key (lower(email))=[REDACTED]"
            ),
        ),
        (
            (
                'null value in column "status" violates not-null constraint\n'
                "DETAIL:  Failing row contains "
                "(stored-identifier, stored\nvalue, null)."
            ),
            (
                'null value in column "status" violates not-null constraint '
                "DETAIL: Failing row contains [REDACTED]"
            ),
        ),
    ],
)
def test_constraint_violations_keep_the_cause_without_row_values(
    driver_message, expected_message
) -> None:
    error = IntegrityError(
        "INSERT INTO stage12_eval_reports (evaluation_id) VALUES (%(id)s)",
        {"id": "stored-identifier"},
        DriverIntegrityError(driver_message),
    )

    summary = failure_summary(error)

    assert "stored" not in summary
    assert summary == f"IntegrityError: DriverIntegrityError: {expected_message}"


@pytest.mark.parametrize(
    ("secret_construct", "kept_length", "secret_fragment"),
    [
        (
            "postgresql://runtime:cutpassword@db.example/postgres",
            30,
            "cutpass",
        ),
        ("{'password': 'open sesame word'}", 28, "sesame"),
        ("[parameters: {'summary': 'stored row value'}]", 36, "stored"),
    ],
)
def test_a_secret_cut_by_the_redaction_limit_is_not_exposed(
    secret_construct, kept_length, secret_fragment
) -> None:
    padding_length = MAX_REDACTED_MESSAGE_LENGTH - kept_length
    padding = ("padding " * padding_length)[:padding_length]
    message = f"{padding}{secret_construct} and more"
    kept_text = message[:MAX_REDACTED_MESSAGE_LENGTH]
    assert secret_fragment in kept_text

    redacted = redact_failure_message(message)

    assert secret_fragment not in redacted
    assert redacted.endswith("...")


@pytest.mark.parametrize("message_unit", PATHOLOGICAL_MESSAGE_UNITS)
def test_long_pathological_messages_are_summarised_quickly(message_unit) -> None:
    message = message_unit * (1_000_000 // len(message_unit))

    started = time.perf_counter()
    summary = failure_summary(RuntimeError(message))
    elapsed = time.perf_counter() - started

    assert elapsed < MAX_PATHOLOGICAL_SECONDS
    assert len(summary) <= MAX_FAILURE_SUMMARY_LENGTH


@pytest.mark.parametrize("message_unit", PATHOLOGICAL_MESSAGE_UNITS)
def test_redaction_patterns_are_linear_without_the_length_cap(
    monkeypatch, message_unit
) -> None:
    monkeypatch.setattr(
        failure_causes, "MAX_REDACTED_MESSAGE_LENGTH", UNCAPPED_PATHOLOGICAL_LENGTH
    )
    message = message_unit * (UNCAPPED_PATHOLOGICAL_LENGTH // len(message_unit))

    started = time.perf_counter()
    redact_failure_message(message)
    elapsed = time.perf_counter() - started

    assert elapsed < MAX_PATHOLOGICAL_SECONDS


def test_summary_is_bounded_to_the_persisted_column_length() -> None:
    summary = failure_summary(
        ValueError("x" * 2_000), progress=StageProgress(role="reform")
    )

    assert len(summary) == MAX_FAILURE_SUMMARY_LENGTH
    assert summary.endswith("...")
    assert TypeAdapter(ErrorSummary).validate_python(summary) == summary


def test_unprintable_errors_still_produce_a_summary() -> None:
    class UnprintableError(Exception):
        def __str__(self) -> str:
            msg = "cannot render"
            raise RuntimeError(msg)

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
            msg = "exporter unavailable"
            raise RuntimeError(msg)

    record_failure_cause(FailingRuntime(), ValueError("cause"), "ValueError: cause")
    record_failure_cause(None, ValueError("cause"), "ValueError: cause")


def _progress_in(stage_name: str) -> StageProgress:
    progress = StageProgress()
    msg = "stage failed"
    with pytest.raises(RuntimeError), progress.stage(stage_name):
        raise RuntimeError(msg)
    return progress
