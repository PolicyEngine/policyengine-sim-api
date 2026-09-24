"""Versioned Stage 12 report, simulation, and artifact contracts."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from hashlib import sha256
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
# ``evaluation_id`` is the already-deployed physical database and wire field
# for the temporary comparison-run identifier. Stage 14 owns its removal.
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
    bundle: BundleProvenance

    @model_validator(mode="before")
    @classmethod
    def reject_combined_policy_input(cls, value: object) -> object:
        if isinstance(value, dict) and ({"baseline", "reform"} & value.keys()):
            raise ValueError(
                "single-simulation input cannot contain baseline or reform fields"
            )
        return value


class ReportAggregate(StrEnum):
    BUDGET = "budget"
    POVERTY = "poverty"
    INEQUALITY = "inequality"
    DISTRIBUTIONAL = "distributional"
    WINNERS_AND_LOSERS = "winners_and_losers"
    GEOGRAPHIC = "geographic"
    PROGRAM_STATISTICS = "program_statistics"


class ReportOutputRequirements(StrictContractModel):
    """Report features that determine which simulation columns must exist."""

    schema_version: Literal[1] = 1
    aggregates: Annotated[tuple[ReportAggregate, ...], Field(min_length=1)]
    include_cliff_impacts: bool
    labor_supply_response_active: bool

    @field_validator("aggregates")
    @classmethod
    def require_complete_aggregate_profile(
        cls,
        value: tuple[ReportAggregate, ...],
    ) -> tuple[ReportAggregate, ...]:
        if len(value) != len(set(value)):
            raise ValueError("report output aggregates must be unique")
        if value != tuple(ReportAggregate):
            raise ValueError(
                "Stage 12 currently requires the complete aggregate profile"
            )
        return value


class EntityOutputPlan(StrictContractModel):
    """Required materialized columns for one country-model entity."""

    entity: ContractText
    materialized_variables: Annotated[
        tuple[ContractText, ...],
        Field(min_length=1),
    ]
    additional_variables: tuple[ContractText, ...] = ()
    dataset_variables: tuple[ContractText, ...] = ()

    @field_validator(
        "materialized_variables",
        "additional_variables",
        "dataset_variables",
    )
    @classmethod
    def require_canonical_variables(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("output-plan variables must be unique")
        if value != tuple(sorted(value)):
            raise ValueError("output-plan variables must use canonical order")
        return value

    @model_validator(mode="after")
    def require_variable_subsets(self) -> EntityOutputPlan:
        if not set(self.additional_variables).issubset(self.materialized_variables):
            raise ValueError(
                "additional output-plan variables must be a materialized subset"
            )
        if not set(self.dataset_variables).issubset(self.materialized_variables):
            raise ValueError(
                "dataset output-plan variables must be a materialized subset"
            )
        if set(self.additional_variables).intersection(self.dataset_variables):
            raise ValueError(
                "calculated additional variables and dataset variables must be disjoint"
            )
        return self


class Stage12OutputPlan(StrictContractModel):
    """One immutable output schema shared by both report simulations."""

    schema_version: Literal[1] = 1
    country: CountryId
    requirements: ReportOutputRequirements
    entities: Annotated[tuple[EntityOutputPlan, ...], Field(min_length=1)]

    @field_validator("entities")
    @classmethod
    def require_canonical_entities(
        cls,
        value: tuple[EntityOutputPlan, ...],
    ) -> tuple[EntityOutputPlan, ...]:
        names = tuple(entity.entity for entity in value)
        if len(names) != len(set(names)):
            raise ValueError("output-plan entities must be unique")
        if names != tuple(sorted(names)):
            raise ValueError("output-plan entities must use canonical order")
        return value


def stage12_output_plan_sha256(plan: Stage12OutputPlan) -> str:
    """Return the stable digest recorded by each Stage 12 child artifact."""

    payload = json.dumps(
        plan.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return sha256(payload).hexdigest()


class PlannedSimulationExecutionInput(SimulationExecutionInput):
    """Internal coordinator-to-worker input with a resolved output schema."""

    output_plan: Stage12OutputPlan

    @model_validator(mode="after")
    def require_output_plan_country(self) -> PlannedSimulationExecutionInput:
        if self.output_plan.country != self.geography.country:
            raise ValueError("output plan country must match simulation geography")
        return self


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
    output_plan_sha256: Sha256Digest
    row_identity: RowIdentity
    bundle: BundleProvenance
    calculation_provenance: dict[str, JsonValue] | None = None


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


class ResultDifference(StrictContractModel):
    """One exact aggregate-result difference identified by JSON Pointer."""

    path: Annotated[str, Field(max_length=4096)]
    production_present: bool
    stage12_present: bool
    production_value: JsonValue = None
    stage12_value: JsonValue = None
    absolute_delta: float | None = None
    relative_delta: float | None = None

    @model_validator(mode="after")
    def validate_presence(self) -> ResultDifference:
        if not self.production_present and not self.stage12_present:
            raise ValueError(
                "a result difference must be present in at least one result"
            )
        if not self.production_present and self.production_value is not None:
            raise ValueError("an absent production value must be null")
        if not self.stage12_present and self.stage12_value is not None:
            raise ValueError("an absent Stage 12 value must be null")
        # A zero production value has no finite relative delta, so an absolute
        # delta with no relative delta is the only permitted partial pair.
        if (self.absolute_delta is None) != (self.relative_delta is None) and (
            self.absolute_delta is None or self.relative_delta is not None
        ):
            raise ValueError("numeric deltas must be supplied together")
        return self


class ResultComparisonArtifactPayload(StrictContractModel):
    """Private comparison receipt without duplicate complete result objects."""

    schema_version: Literal[1] = 1
    evaluation_id: UUID
    production_job_id: ContractText
    compared_at: datetime
    status: Literal["matched", "different"]
    production_result_sha256: Sha256Digest
    stage12_result_sha256: Sha256Digest
    difference_count: Annotated[int, Field(ge=0)]
    differences: tuple[ResultDifference, ...]

    @model_validator(mode="after")
    def validate_differences(self) -> ResultComparisonArtifactPayload:
        if self.compared_at.tzinfo is None:
            raise ValueError("comparison timestamp must include a timezone")
        if self.difference_count != len(self.differences):
            raise ValueError("difference_count must equal the number of differences")
        expected_status = "matched" if not self.differences else "different"
        if self.status != expected_status:
            raise ValueError("comparison status does not match its differences")
        return self


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
    # Automatic runs retain the existing Modal function-call identifier so the
    # coordinator can retrieve the production aggregate after Stage 12 finishes.
    # Direct Stage 12-only runs have no associated production call.
    production_function_call_id: ContractText | None = None
    created_at: datetime
    retention_expires_at: datetime

    @model_validator(mode="after")
    def validate_retention(self) -> Stage12InvocationContext:
        _validate_retention(self.created_at, self.retention_expires_at)
        return self


ErrorCode = Annotated[str, Field(min_length=1, max_length=64)]
ErrorSummary = Annotated[str, Field(min_length=1, max_length=512)]
MAX_COMPARISON_RUN_ARTIFACT_RETENTION = timedelta(days=30)


def _validate_retention(created_at: datetime, retention_expires_at: datetime) -> None:
    if created_at.tzinfo is None or retention_expires_at.tzinfo is None:
        raise ValueError("comparison retention timestamps must include a timezone")
    retention = retention_expires_at - created_at
    if retention <= timedelta(0):
        raise ValueError("comparison retention must be greater than zero")
    if retention > MAX_COMPARISON_RUN_ARTIFACT_RETENTION:
        raise ValueError("comparison retention must not exceed 30 days")


class ComparisonRunLifecycleStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    INCOMPLETE = "incomplete"
    SKIPPED = "skipped"


class ComparisonRunAggregationStatus(StrEnum):
    NOT_STARTED = "not_started"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class ResultComparisonStatus(StrEnum):
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
        if self.status is ComparisonRunLifecycleStatus.SUCCEEDED:
            if self.aggregation_status is not ComparisonRunAggregationStatus.SUCCEEDED:
                raise ValueError("a successful report requires successful aggregation")
            if not all(value is not None for value in output_fields):
                raise ValueError("a successful report requires an aggregate output")
            if self.completed_at is None:
                raise ValueError("a successful report requires completed_at")
        if (
            self.status
            in {
                ComparisonRunLifecycleStatus.FAILED,
                ComparisonRunLifecycleStatus.SKIPPED,
            }
            and self.error_code is None
        ):
            raise ValueError("a failed or skipped report requires an error_code")
        comparison_output_fields = (
            self.comparison_output_uri,
            self.comparison_output_sha256,
            self.comparison_schema_version,
        )
        if self.comparison_status in {
            ResultComparisonStatus.NOT_REQUESTED,
            ResultComparisonStatus.PENDING,
            ResultComparisonStatus.RUNNING,
        }:
            if any(value is not None for value in comparison_output_fields):
                raise ValueError(
                    "an incomplete comparison cannot reference an output artifact"
                )
            if self.comparison_completed_at is not None:
                raise ValueError("an incomplete comparison cannot have completed_at")
            if self.comparison_error_code is not None:
                raise ValueError("an incomplete comparison cannot have an error_code")
            if self.comparison_error_summary is not None:
                raise ValueError(
                    "an incomplete comparison cannot have an error_summary"
                )
        elif self.comparison_status in {
            ResultComparisonStatus.MATCHED,
            ResultComparisonStatus.DIFFERENT,
        }:
            if not all(value is not None for value in comparison_output_fields):
                raise ValueError("a completed comparison requires an output artifact")
            if self.comparison_completed_at is None:
                raise ValueError("a completed comparison requires completed_at")
            if self.comparison_error_code is not None:
                raise ValueError("a completed comparison cannot have an error_code")
            if self.comparison_error_summary is not None:
                raise ValueError("a completed comparison cannot have an error_summary")
        else:
            if any(value is not None for value in comparison_output_fields):
                raise ValueError(
                    "a failed comparison cannot reference an output artifact"
                )
            if self.comparison_completed_at is None:
                raise ValueError("a failed comparison requires completed_at")
            if self.comparison_error_code is None:
                raise ValueError("a failed comparison requires an error_code")
        return self


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
        if self.status is ComparisonRunLifecycleStatus.SUCCEEDED:
            if not all(value is not None for value in output_fields):
                raise ValueError("a successful simulation requires an output")
            if self.completed_at is None:
                raise ValueError("a successful simulation requires completed_at")
        if (
            self.status
            in {
                ComparisonRunLifecycleStatus.FAILED,
                ComparisonRunLifecycleStatus.SKIPPED,
            }
            and self.error_code is None
        ):
            raise ValueError("a failed or skipped simulation requires an error_code")
        return self


@dataclass(frozen=True)
class ComparisonReportPersistenceResult:
    record: ComparisonReportRecord
    created: bool


@dataclass(frozen=True)
class ComparisonSimulationPersistenceResult:
    record: ComparisonSimulationRecord
    created: bool
