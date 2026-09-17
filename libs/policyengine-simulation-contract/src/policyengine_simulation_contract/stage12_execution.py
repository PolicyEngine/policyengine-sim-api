"""Versioned Stage 12 report, simulation, and artifact contracts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Annotated, Literal
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    field_validator,
    model_validator,
)

ContractText = Annotated[str, Field(min_length=1, max_length=255)]
Sha256Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
StorageUri = Annotated[
    str,
    Field(min_length=6, max_length=2048, pattern=r"^gs://[^/]+/.+$"),
]
DatasetArtifactUri = Annotated[
    str,
    Field(
        min_length=6,
        max_length=2048,
        pattern=r"^(?:gs|hf)://[^/]+/.+$",
    ),
]
CountryId = Annotated[
    Literal["us", "uk"],
    Field(description="Supported PolicyEngine country ID"),
]


class StrictContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, frozen=True)


class SimulationRole(StrEnum):
    BASELINE = "baseline"
    REFORM = "reform"
    STANDALONE = "standalone"


class ArtifactMediaType(StrEnum):
    JSON = "application/json"
    PARQUET = "application/vnd.apache.parquet"


class SimulationParquetPayloadContract(StrictContractModel):
    """Physical schema rules for a Stage 12 simulation artifact."""

    payload_schema_version: Literal[1] = 1
    media_type: Literal["application/vnd.apache.parquet"] = (
        "application/vnd.apache.parquet"
    )
    compression: Literal["zstd"] = "zstd"
    parquet_version: Literal["2.6"] = "2.6"
    data_page_version: Literal["2.0"] = "2.0"
    entity_column: Literal["__entity__"] = "__entity__"
    row_order_column: Literal["__row_order__"] = "__row_order__"
    identifier_column_template: Literal["{entity}_id"] = "{entity}_id"
    column_order: Literal["system columns, then lexicographic"] = (
        "system columns, then lexicographic"
    )
    row_order: Literal["entity name, then entity identifier"] = (
        "entity name, then entity identifier"
    )
    schema_version_metadata_key: Literal["policyengine.stage12.schema_version"] = (
        "policyengine.stage12.schema_version"
    )
    dtype_metadata_key: Literal["policyengine.stage12.dtypes"] = (
        "policyengine.stage12.dtypes"
    )
    calculation_provenance_metadata_key: Literal[
        "policyengine.stage12.calculation_provenance"
    ] = "policyengine.stage12.calculation_provenance"


SIMULATION_PARQUET_PAYLOAD_CONTRACT = SimulationParquetPayloadContract()


class DatasetArtifactMediaType(StrEnum):
    HDF5 = "application/x-hdf5"
    JSON = "application/json"
    PARQUET = "application/vnd.apache.parquet"


class DatasetProvenance(StrictContractModel):
    identity: ContractText
    uri: Annotated[str, Field(min_length=1, max_length=2048)]
    artifact_revision: ContractText
    data_package_name: ContractText
    data_package_version: ContractText


class BundleProvenance(StrictContractModel):
    policyengine_version: ContractText
    country_package_name: ContractText
    country_package_version: ContractText
    dataset: DatasetProvenance
    bundle_manifest_sha256: Sha256Digest


class ArtifactReference(StrictContractModel):
    uri: StorageUri
    media_type: ArtifactMediaType
    content_sha256: Sha256Digest
    size_bytes: Annotated[int, Field(ge=0)]


class DatasetArtifactReference(StrictContractModel):
    uri: DatasetArtifactUri
    media_type: DatasetArtifactMediaType
    content_sha256: Sha256Digest
    size_bytes: Annotated[int, Field(ge=0)] | None = None


class DatasetPopulationInput(StrictContractModel):
    kind: Literal["dataset"] = "dataset"
    artifact: DatasetArtifactReference


class HouseholdPopulationInput(StrictContractModel):
    kind: Literal["household"] = "household"
    document: dict[str, JsonValue]


PopulationInput = Annotated[
    DatasetPopulationInput | HouseholdPopulationInput,
    Field(discriminator="kind"),
]


class GeographySelection(StrictContractModel):
    country: CountryId
    region: ContractText
    filter_field: ContractText | None = None
    filter_value: ContractText | None = None
    filter_strategy: ContractText | None = None

    @model_validator(mode="after")
    def require_complete_filter(self) -> GeographySelection:
        if (self.filter_field is None) != (self.filter_value is None):
            raise ValueError("filter_field and filter_value must be supplied together")
        if self.filter_strategy is not None and self.filter_field is None:
            raise ValueError("filter_strategy requires filter_field and filter_value")
        return self


class RequestedSimulationOutput(StrictContractModel):
    schema_version: Literal[1] = 1
    variables: Annotated[tuple[ContractText, ...], Field(min_length=1)]

    @field_validator("variables")
    @classmethod
    def require_unique_variables(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("requested variables must be unique")
        return value


class SimulationExecutionInput(StrictContractModel):
    contract_version: Literal[1] = 1
    evaluation_id: UUID
    simulation_execution_id: UUID
    role: SimulationRole
    policy: dict[str, JsonValue]
    population: PopulationInput
    year: Annotated[int, Field(ge=1900, le=2200)]
    geography: GeographySelection
    options: dict[str, JsonValue] = Field(default_factory=dict)
    requested_output: RequestedSimulationOutput
    bundle: BundleProvenance

    @model_validator(mode="before")
    @classmethod
    def reject_combined_policy_input(cls, value: object) -> object:
        if isinstance(value, dict) and ({"baseline", "reform"} & value.keys()):
            raise ValueError(
                "single-simulation input cannot contain baseline or reform fields"
            )
        return value


class RowIdentity(StrictContractModel):
    schema_version: Literal[1] = 1
    identifier_columns: Annotated[tuple[ContractText, ...], Field(min_length=1)]
    row_count: Annotated[int, Field(ge=0)]
    identity_sha256: Sha256Digest

    @field_validator("identifier_columns")
    @classmethod
    def require_unique_columns(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("row identifier columns must be unique")
        return value


class SimulationArtifactDescriptor(StrictContractModel):
    contract_version: Literal[1] = 1
    evaluation_id: UUID
    simulation_execution_id: UUID
    role: SimulationRole
    artifact: ArtifactReference
    output_schema_version: Literal[1] = 1
    row_identity: RowIdentity
    bundle: BundleProvenance
    calculation_provenance: dict[str, JsonValue] | None = None


class ReportAggregate(StrEnum):
    BUDGET = "budget"
    POVERTY = "poverty"
    INEQUALITY = "inequality"
    DISTRIBUTIONAL = "distributional"
    WINNERS_AND_LOSERS = "winners_and_losers"
    GEOGRAPHIC = "geographic"
    PROGRAM_STATISTICS = "program_statistics"


class ReportExecutionInput(StrictContractModel):
    contract_version: Literal[1] = 1
    evaluation_id: UUID
    baseline: SimulationExecutionInput
    reform: SimulationExecutionInput
    requested_aggregates: Annotated[tuple[ReportAggregate, ...], Field(min_length=1)]

    @model_validator(mode="after")
    def require_aligned_simulations(self) -> ReportExecutionInput:
        if self.baseline.role is not SimulationRole.BASELINE:
            raise ValueError("baseline input must use the baseline role")
        if self.reform.role is not SimulationRole.REFORM:
            raise ValueError("reform input must use the reform role")
        if self.baseline.evaluation_id != self.evaluation_id:
            raise ValueError("baseline evaluation_id must match the report")
        if self.reform.evaluation_id != self.evaluation_id:
            raise ValueError("reform evaluation_id must match the report")
        if self.baseline.simulation_execution_id == self.reform.simulation_execution_id:
            raise ValueError("baseline and reform simulation identifiers must differ")
        for field_name in (
            "population",
            "year",
            "geography",
            "options",
            "requested_output",
            "bundle",
        ):
            if getattr(self.baseline, field_name) != getattr(self.reform, field_name):
                raise ValueError(
                    f"baseline and reform {field_name} must match before execution"
                )
        if len(self.requested_aggregates) != len(set(self.requested_aggregates)):
            raise ValueError("requested aggregates must be unique")
        return self


class AggregateReportArtifactDescriptor(StrictContractModel):
    contract_version: Literal[1] = 1
    evaluation_id: UUID
    artifact: ArtifactReference
    aggregate_schema_version: Literal[1] = 1
    baseline_artifact_sha256: Sha256Digest
    reform_artifact_sha256: Sha256Digest
    bundle: BundleProvenance


class AggregateReportArtifactPayload(StrictContractModel):
    """Complete JSON payload stored in one aggregate report artifact."""

    contract_version: Literal[1] = 1
    aggregate_schema_version: Literal[1] = 1
    evaluation_id: UUID
    requested_aggregates: Annotated[tuple[ReportAggregate, ...], Field(min_length=1)]
    bundle: BundleProvenance
    result: dict[str, JsonValue]

    @field_validator("requested_aggregates")
    @classmethod
    def require_unique_aggregates(
        cls,
        value: tuple[ReportAggregate, ...],
    ) -> tuple[ReportAggregate, ...]:
        if len(value) != len(set(value)):
            raise ValueError("requested aggregates must be unique")
        return value


class Stage12InvocationContext(StrictContractModel):
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
    created_at: datetime
    retention_expires_at: datetime

    @model_validator(mode="after")
    def validate_retention(self) -> Stage12InvocationContext:
        _validate_retention(self.created_at, self.retention_expires_at)
        return self


ErrorCode = Annotated[str, Field(min_length=1, max_length=64)]
ErrorSummary = Annotated[str, Field(min_length=1, max_length=512)]
MAX_EVALUATION_RETENTION = timedelta(days=30)


def _validate_retention(created_at: datetime, retention_expires_at: datetime) -> None:
    if created_at.tzinfo is None or retention_expires_at.tzinfo is None:
        raise ValueError("evaluation retention timestamps must include a timezone")
    retention = retention_expires_at - created_at
    if retention <= timedelta(0):
        raise ValueError("evaluation retention must be greater than zero")
    if retention > MAX_EVALUATION_RETENTION:
        raise ValueError("evaluation retention must not exceed 30 days")


class EvaluationLifecycleStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    INCOMPLETE = "incomplete"
    SKIPPED = "skipped"


class EvaluationAggregationStatus(StrEnum):
    NOT_STARTED = "not_started"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class EvaluationReportRecord(StrictContractModel):
    """Complete producer/consumer contract for one temporary parent row."""

    contract_version: Literal[1] = 1
    evaluation_id: UUID
    status: EvaluationLifecycleStatus
    aggregation_status: EvaluationAggregationStatus
    environment: ContractText
    calculation_flow: ContractText
    originating_request_id: ContractText
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
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None
    retention_expires_at: datetime

    @model_validator(mode="after")
    def validate_state(self) -> EvaluationReportRecord:
        _validate_retention(self.created_at, self.retention_expires_at)
        output_fields = (
            self.aggregate_output_uri,
            self.aggregate_output_sha256,
            self.aggregate_schema_version,
        )
        if any(value is not None for value in output_fields) and not all(
            value is not None for value in output_fields
        ):
            raise ValueError("aggregate output fields must be supplied together")
        if self.status is EvaluationLifecycleStatus.SUCCEEDED:
            if self.aggregation_status is not EvaluationAggregationStatus.SUCCEEDED:
                raise ValueError("a successful report requires successful aggregation")
            if not all(value is not None for value in output_fields):
                raise ValueError("a successful report requires an aggregate output")
            if self.completed_at is None:
                raise ValueError("a successful report requires completed_at")
        if (
            self.status
            in {
                EvaluationLifecycleStatus.FAILED,
                EvaluationLifecycleStatus.SKIPPED,
            }
            and self.error_code is None
        ):
            raise ValueError("a failed or skipped report requires an error_code")
        return self


class EvaluationSimulationRecord(StrictContractModel):
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
    status: EvaluationLifecycleStatus
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
    def validate_state(self) -> EvaluationSimulationRecord:
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
            raise ValueError("simulation output fields must be supplied together")
        if self.row_identity_columns is not None:
            if not self.row_identity_columns:
                raise ValueError("row_identity_columns must not be empty")
            if len(self.row_identity_columns) != len(set(self.row_identity_columns)):
                raise ValueError("row_identity_columns must be unique")
        if self.status is EvaluationLifecycleStatus.SUCCEEDED:
            if not all(value is not None for value in output_fields):
                raise ValueError("a successful simulation requires an output")
            if self.completed_at is None:
                raise ValueError("a successful simulation requires completed_at")
        if (
            self.status
            in {
                EvaluationLifecycleStatus.FAILED,
                EvaluationLifecycleStatus.SKIPPED,
            }
            and self.error_code is None
        ):
            raise ValueError("a failed or skipped simulation requires an error_code")
        return self


@dataclass(frozen=True)
class EvaluationReportPersistenceResult:
    record: EvaluationReportRecord
    created: bool


@dataclass(frozen=True)
class EvaluationSimulationPersistenceResult:
    record: EvaluationSimulationRecord
    created: bool
