"""A comparison run's invocation context, statuses, and its parent and child records."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, model_validator

# Models subclass these bases through the package's path, which ruff.toml
# lists as runtime-evaluated; base.py binds them in the package first.
from policyengine_simulation_contract.stage12_execution import StrictContractModel
from policyengine_simulation_contract.stage12_execution.base import (
    ContractText,
    CountryId,
    ObservabilityId,
    Sha256Digest,
    SimulationRole,
    StorageUri,
)


class ReportResultDelivery(StrEnum):
    """How a report coordinator and its simulations deliver their outcome.

    ``STAGE12_DATABASE``, the default, records the report's and each
    simulation's state in the Stage 12 tables, and the coordinator returns an
    aggregate artifact descriptor. ``RETURN_TO_CALLER`` reads and writes no
    Stage 12 rows: the coordinator returns a ``ReportExecutionResult`` (see
    ``report_execution``) or raises ``ReportExecutionError``.
    """

    STAGE12_DATABASE = "stage12_database"
    RETURN_TO_CALLER = "return_to_caller"


class Stage12InvocationContext(StrictContractModel):
    """Deployment and retention context for one comparison-run invocation.

    Records the originating request, the environments, the worker version and
    callable, the manifest digests and the artifact prefix. Automatic runs also
    name the production function call they are compared with. Artifacts are kept
    for at most 30 days after ``created_at``. ``result_delivery`` selects how the
    coordinator and its simulations deliver their outcome; a report returned to
    its caller cannot be compared with a production job.
    """

    contract_version: Literal[1] = 1
    request_id: ContractText
    # The logical environment labels durable records and artifact paths.
    environment: ContractText
    # Modal uses ``main`` for production, so it cannot be inferred from the
    # logical ``production`` label.
    modal_environment: ContractText
    worker_version: ContractText
    modal_application: ContractText
    simulation_callable: ContractText
    version_manifest_sha256: Sha256Digest
    bundle_manifest_sha256: Sha256Digest
    artifact_prefix: Annotated[str, Field(min_length=1, max_length=2048)]
    # Automatic runs retain the existing Modal function-call identifier so the
    # coordinator can retrieve the production aggregate after Stage 12 finishes.
    # Direct Stage 12-only runs have no associated production call.
    production_function_call_id: ContractText | None = None
    created_at: datetime
    retention_expires_at: datetime
    # Callers that leave it out, such as the Simulation Entrypoint today, keep
    # the Stage 12 database behavior.
    result_delivery: ReportResultDelivery = ReportResultDelivery.STAGE12_DATABASE

    @model_validator(mode="after")
    def validate_retention(self) -> Stage12InvocationContext:
        """Require aware timestamps and a retention window of at most 30 days."""
        _validate_retention(self.created_at, self.retention_expires_at)
        return self

    @model_validator(mode="after")
    def validate_result_delivery(self) -> Stage12InvocationContext:
        """Reject a production comparison for a report returned to its caller.

        The comparison is recorded on the Stage 12 report row, which a
        returned report never writes.
        """
        if (
            self.result_delivery is ReportResultDelivery.RETURN_TO_CALLER
            and self.production_function_call_id is not None
        ):
            msg = (
                "a report returned to its caller cannot request a production comparison"
            )
            raise ValueError(msg)
        return self


ErrorCode = Annotated[str, Field(min_length=1, max_length=64)]
ErrorSummary = Annotated[str, Field(min_length=1, max_length=512)]
MAX_COMPARISON_RUN_ARTIFACT_RETENTION = timedelta(days=30)


def _validate_retention(created_at: datetime, retention_expires_at: datetime) -> None:
    if created_at.tzinfo is None or retention_expires_at.tzinfo is None:
        msg = "comparison retention timestamps must include a timezone"
        raise ValueError(msg)
    retention = retention_expires_at - created_at
    if retention <= timedelta(0):
        msg = "comparison retention must be greater than zero"
        raise ValueError(msg)
    if retention > MAX_COMPARISON_RUN_ARTIFACT_RETENTION:
        msg = "comparison retention must not exceed 30 days"
        raise ValueError(msg)


class ComparisonRunLifecycleStatus(StrEnum):
    """Overall status of a comparison run."""

    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    INCOMPLETE = "incomplete"
    SKIPPED = "skipped"


class ComparisonRunAggregationStatus(StrEnum):
    """Status of aggregating a comparison run's simulations into a report."""

    NOT_STARTED = "not_started"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class ResultComparisonStatus(StrEnum):
    """Outcome of checking a comparison run's aggregate result against production."""

    NOT_REQUESTED = "not_requested"
    PENDING = "pending"
    RUNNING = "running"
    MATCHED = "matched"
    DIFFERENT = "different"
    FAILED = "failed"


class ComparisonReportRecord(StrictContractModel):
    """Complete producer/consumer contract for one temporary parent row."""

    contract_version: Literal[1] = 1
    evaluation_id: UUID
    status: ComparisonRunLifecycleStatus
    aggregation_status: ComparisonRunAggregationStatus
    environment: ContractText
    calculation_flow: ContractText
    originating_request_id: ContractText
    observability_id: ObservabilityId | None = None
    production_identity: ContractText
    incumbent_execution_id: ContractText | None = None
    worker_version: ContractText
    modal_application: ContractText
    report_coordinator_callable: ContractText
    version_manifest_sha256: Sha256Digest
    policyengine_version: ContractText
    country_package_name: ContractText
    country_package_version: ContractText
    country: CountryId
    dataset_identity: ContractText
    dataset_uri: Annotated[str, Field(min_length=1, max_length=2048)]
    data_package_name: ContractText
    data_package_version: ContractText
    data_artifact_revision: ContractText
    coordinator_invocation_id: ContractText | None = None
    error_code: ErrorCode | None = None
    error_summary: ErrorSummary | None = None
    aggregate_output_uri: StorageUri | None = None
    aggregate_output_sha256: Sha256Digest | None = None
    aggregate_schema_version: Annotated[int, Field(ge=1)] | None = None
    comparison_status: ResultComparisonStatus = ResultComparisonStatus.NOT_REQUESTED
    comparison_output_uri: StorageUri | None = None
    comparison_output_sha256: Sha256Digest | None = None
    comparison_schema_version: Annotated[int, Field(ge=1)] | None = None
    comparison_completed_at: datetime | None = None
    comparison_error_code: ErrorCode | None = None
    comparison_error_summary: ErrorSummary | None = None
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None
    retention_expires_at: datetime

    @model_validator(mode="after")
    def validate_state(self) -> ComparisonReportRecord:
        """Check that the row's fields are consistent with its lifecycle states.

        Aggregate and comparison output fields come all-or-nothing, a success
        needs its outputs and ``completed_at``, failures and skips need an
        ``error_code``, and the retention window is at most 30 days.
        """
        _validate_retention(self.created_at, self.retention_expires_at)
        self._validate_report_state()
        self._validate_comparison_state()
        return self

    def _validate_report_state(self) -> None:
        """Check the aggregate output fields against the report's status."""
        output_fields = (
            self.aggregate_output_uri,
            self.aggregate_output_sha256,
            self.aggregate_schema_version,
        )
        if any(value is not None for value in output_fields) and not all(
            value is not None for value in output_fields
        ):
            msg = "aggregate output fields must be supplied together"
            raise ValueError(msg)
        if self.status is ComparisonRunLifecycleStatus.SUCCEEDED:
            if self.aggregation_status is not ComparisonRunAggregationStatus.SUCCEEDED:
                msg = "a successful report requires successful aggregation"
                raise ValueError(msg)
            if not all(value is not None for value in output_fields):
                msg = "a successful report requires an aggregate output"
                raise ValueError(msg)
            if self.completed_at is None:
                msg = "a successful report requires completed_at"
                raise ValueError(msg)
        if (
            self.status
            in {
                ComparisonRunLifecycleStatus.FAILED,
                ComparisonRunLifecycleStatus.SKIPPED,
            }
            and self.error_code is None
        ):
            msg = "a failed or skipped report requires an error_code"
            raise ValueError(msg)

    def _validate_comparison_state(self) -> None:
        """Check the result comparison fields against the comparison's status."""
        if self.comparison_status in {
            ResultComparisonStatus.NOT_REQUESTED,
            ResultComparisonStatus.PENDING,
            ResultComparisonStatus.RUNNING,
        }:
            self._validate_incomplete_comparison()
        elif self.comparison_status in {
            ResultComparisonStatus.MATCHED,
            ResultComparisonStatus.DIFFERENT,
        }:
            self._validate_completed_comparison()
        else:
            self._validate_failed_comparison()

    @property
    def _comparison_output_fields(self) -> tuple[object, ...]:
        return (
            self.comparison_output_uri,
            self.comparison_output_sha256,
            self.comparison_schema_version,
        )

    def _validate_incomplete_comparison(self) -> None:
        """Reject outputs, completion and errors on a comparison still to finish."""
        if any(value is not None for value in self._comparison_output_fields):
            msg = "an incomplete comparison cannot reference an output artifact"
            raise ValueError(msg)
        if self.comparison_completed_at is not None:
            msg = "an incomplete comparison cannot have completed_at"
            raise ValueError(msg)
        if self.comparison_error_code is not None:
            msg = "an incomplete comparison cannot have an error_code"
            raise ValueError(msg)
        if self.comparison_error_summary is not None:
            msg = "an incomplete comparison cannot have an error_summary"
            raise ValueError(msg)

    def _validate_completed_comparison(self) -> None:
        """Require an output and completion, and no error, on a finished comparison."""
        if not all(value is not None for value in self._comparison_output_fields):
            msg = "a completed comparison requires an output artifact"
            raise ValueError(msg)
        if self.comparison_completed_at is None:
            msg = "a completed comparison requires completed_at"
            raise ValueError(msg)
        if self.comparison_error_code is not None:
            msg = "a completed comparison cannot have an error_code"
            raise ValueError(msg)
        if self.comparison_error_summary is not None:
            msg = "a completed comparison cannot have an error_summary"
            raise ValueError(msg)

    def _validate_failed_comparison(self) -> None:
        """Require completion and an error code, but no output, on a failure."""
        if any(value is not None for value in self._comparison_output_fields):
            msg = "a failed comparison cannot reference an output artifact"
            raise ValueError(msg)
        if self.comparison_completed_at is None:
            msg = "a failed comparison requires completed_at"
            raise ValueError(msg)
        if self.comparison_error_code is None:
            msg = "a failed comparison requires an error_code"
            raise ValueError(msg)


class ComparisonSimulationRecord(StrictContractModel):
    """Complete producer/consumer contract for one temporary child row."""

    contract_version: Literal[1] = 1
    simulation_execution_id: UUID
    evaluation_id: UUID
    role: SimulationRole
    input_sha256: Sha256Digest
    worker_version: ContractText
    modal_application: ContractText
    simulation_callable: ContractText
    version_manifest_sha256: Sha256Digest
    modal_invocation_id: ContractText | None = None
    status: ComparisonRunLifecycleStatus
    error_code: ErrorCode | None = None
    error_summary: ErrorSummary | None = None
    output_uri: StorageUri | None = None
    output_sha256: Sha256Digest | None = None
    output_schema_version: Annotated[int, Field(ge=1)] | None = None
    row_identity_columns: tuple[ContractText, ...] | None = None
    row_count: Annotated[int, Field(ge=0)] | None = None
    row_identity_sha256: Sha256Digest | None = None
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None
    retention_expires_at: datetime

    @model_validator(mode="after")
    def validate_state(self) -> ComparisonSimulationRecord:
        """Check that the row's fields are consistent with its lifecycle status.

        Output and row-identity fields come all-or-nothing, a success needs
        them and ``completed_at``, failures and skips need an ``error_code``,
        and the retention window is at most 30 days.
        """
        _validate_retention(self.created_at, self.retention_expires_at)
        output_fields = (
            self.output_uri,
            self.output_sha256,
            self.output_schema_version,
            self.row_identity_columns,
            self.row_count,
            self.row_identity_sha256,
        )
        if any(value is not None for value in output_fields) and not all(
            value is not None for value in output_fields
        ):
            msg = "simulation output fields must be supplied together"
            raise ValueError(msg)
        if self.row_identity_columns is not None:
            if not self.row_identity_columns:
                msg = "row_identity_columns must not be empty"
                raise ValueError(msg)
            if len(self.row_identity_columns) != len(set(self.row_identity_columns)):
                msg = "row_identity_columns must be unique"
                raise ValueError(msg)
        if self.status is ComparisonRunLifecycleStatus.SUCCEEDED:
            if not all(value is not None for value in output_fields):
                msg = "a successful simulation requires an output"
                raise ValueError(msg)
            if self.completed_at is None:
                msg = "a successful simulation requires completed_at"
                raise ValueError(msg)
        if (
            self.status
            in {
                ComparisonRunLifecycleStatus.FAILED,
                ComparisonRunLifecycleStatus.SKIPPED,
            }
            and self.error_code is None
        ):
            msg = "a failed or skipped simulation requires an error_code"
            raise ValueError(msg)
        return self


@dataclass(frozen=True)
class ComparisonReportPersistenceResult:
    """The stored parent row after an idempotent create.

    ``created`` is False when an identical row already existed and was reused.
    """

    record: ComparisonReportRecord
    created: bool


@dataclass(frozen=True)
class ComparisonSimulationPersistenceResult:
    """The stored child row after an idempotent create.

    ``created`` is False when an identical row already existed and was reused.
    """

    record: ComparisonSimulationRecord
    created: bool
