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

from policyengine_simulation_contract.spm import (
    SPMComparisonProvenance,
    SPMProvenance,
)
from policyengine_simulation_contract.uk_geography import (
    UKLocalAuthorityBoundaryVersion as UKLocalAuthorityBoundaryVersion,  # noqa: PLC0414  # explicit re-export: callers import it from stage12_execution
)
from policyengine_simulation_contract.uk_geography import (
    UKLocalAuthorityMetadata as UKLocalAuthorityMetadata,  # noqa: PLC0414  # explicit re-export: callers import it from stage12_execution
)

ContractText = Annotated[str, Field(min_length=1, max_length=255)]
ObservabilityId = Annotated[str, Field(min_length=1, max_length=36)]
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
    """Base for Stage 12 contracts; rejects unknown fields and non-finite numbers."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, frozen=True)


class SimulationRole(StrEnum):
    """Role of a simulation: baseline or reform of a comparison, or a standalone run."""

    BASELINE = "baseline"
    REFORM = "reform"
    STANDALONE = "standalone"


class ArtifactMediaType(StrEnum):
    """Media type of a Stage 12 output artifact."""

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
    uk_local_authority_metadata_key: Literal[
        "policyengine.stage12.uk_local_authority_metadata"
    ] = "policyengine.stage12.uk_local_authority_metadata"


SIMULATION_PARQUET_PAYLOAD_CONTRACT = SimulationParquetPayloadContract()


class DatasetArtifactMediaType(StrEnum):
    """Media type of an input dataset artifact."""

    HDF5 = "application/x-hdf5"
    JSON = "application/json"
    PARQUET = "application/vnd.apache.parquet"


class DatasetProvenance(StrictContractModel):
    """The dataset a simulation used: identity, URI, revision and data package."""

    identity: ContractText
    uri: Annotated[str, Field(min_length=1, max_length=2048)]
    artifact_revision: ContractText
    data_package_name: ContractText
    data_package_version: ContractText


class BundleProvenance(StrictContractModel):
    """The pinned software and data a simulation ran with.

    Covers the PolicyEngine.py version, the country model package, the dataset,
    and the SHA-256 of the bundle manifest they came from.
    """

    policyengine_version: ContractText
    country_package_name: ContractText
    country_package_version: ContractText
    dataset: DatasetProvenance
    bundle_manifest_sha256: Sha256Digest


class ArtifactReference(StrictContractModel):
    """An artifact in Cloud Storage, with its media type, SHA-256 and size in bytes."""

    uri: StorageUri
    media_type: ArtifactMediaType
    content_sha256: Sha256Digest
    size_bytes: Annotated[int, Field(ge=0)]


class DatasetArtifactReference(StrictContractModel):
    """A dataset artifact in Cloud Storage (``gs://``) or on Hugging Face (``hf://``).

    ``size_bytes`` may be omitted when the size is not known.
    """

    uri: DatasetArtifactUri
    media_type: DatasetArtifactMediaType
    content_sha256: Sha256Digest
    size_bytes: Annotated[int, Field(ge=0)] | None = None


class DatasetPopulationInput(StrictContractModel):
    """Population input that simulates the households in a dataset artifact."""

    kind: Literal["dataset"] = "dataset"
    artifact: DatasetArtifactReference


class HouseholdPopulationInput(StrictContractModel):
    """Population input that simulates one household from a situation document."""

    kind: Literal["household"] = "household"
    document: dict[str, JsonValue]


PopulationInput = Annotated[
    DatasetPopulationInput | HouseholdPopulationInput,
    Field(discriminator="kind"),
]


class GeographySelection(StrictContractModel):
    """Country and region to simulate, optionally filtered on one dataset field.

    ``filter_field`` and ``filter_value`` must be given together, and
    ``filter_strategy`` requires both.
    """

    country: CountryId
    region: ContractText
    filter_field: ContractText | None = None
    filter_value: ContractText | None = None
    filter_strategy: ContractText | None = None

    @model_validator(mode="after")
    def require_complete_filter(self) -> GeographySelection:
        """Require the filter field and value together, and both for a strategy."""
        if (self.filter_field is None) != (self.filter_value is None):
            msg = "filter_field and filter_value must be supplied together"
            raise ValueError(msg)
        if self.filter_strategy is not None and self.filter_field is None:
            msg = "filter_strategy requires filter_field and filter_value"
            raise ValueError(msg)
        return self


class SimulationExecutionInput(StrictContractModel):
    """Everything needed to run one simulation of a comparison run.

    ``evaluation_id`` identifies the comparison run and
    ``simulation_execution_id`` this simulation within it. The input fixes the
    policy, population, year, geography, options and pinned bundle; a baseline
    and reform pair is described by ``ReportExecutionInput`` instead.
    """

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
        """Reject report-shaped input that carries ``baseline`` or ``reform`` keys.

        One execution input describes one simulation; the pair belongs in
        ``ReportExecutionInput``.
        """
        if isinstance(value, dict) and ({"baseline", "reform"} & value.keys()):
            msg = "single-simulation input cannot contain baseline or reform fields"
            raise ValueError(msg)
        return value


class ReportAggregate(StrEnum):
    """A family of aggregate results that a comparison report computes."""

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
        """Require every ``ReportAggregate``, once each and in declaration order."""
        if len(value) != len(set(value)):
            msg = "report output aggregates must be unique"
            raise ValueError(msg)
        if value != tuple(ReportAggregate):
            msg = "Stage 12 currently requires the complete aggregate profile"
            raise ValueError(msg)
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
        """Require variable names to be unique and sorted, so plans hash stably."""
        if len(value) != len(set(value)):
            msg = "output-plan variables must be unique"
            raise ValueError(msg)
        if value != tuple(sorted(value)):
            msg = "output-plan variables must use canonical order"
            raise ValueError(msg)
        return value

    @model_validator(mode="after")
    def require_variable_subsets(self) -> EntityOutputPlan:
        """Require the additional and dataset variables not to overlap.

        Both must also be subsets of ``materialized_variables``.
        """
        if not set(self.additional_variables).issubset(self.materialized_variables):
            msg = "additional output-plan variables must be a materialized subset"
            raise ValueError(msg)
        if not set(self.dataset_variables).issubset(self.materialized_variables):
            msg = "dataset output-plan variables must be a materialized subset"
            raise ValueError(msg)
        if set(self.additional_variables).intersection(self.dataset_variables):
            msg = (
                "calculated additional variables and dataset variables must be disjoint"
            )
            raise ValueError(msg)
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
        """Require entity plans to be unique and sorted by entity name."""
        names = tuple(entity.entity for entity in value)
        if len(names) != len(set(names)):
            msg = "output-plan entities must be unique"
            raise ValueError(msg)
        if names != tuple(sorted(names)):
            msg = "output-plan entities must use canonical order"
            raise ValueError(msg)
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
        """Reject an output plan built for a different country than the geography."""
        if self.output_plan.country != self.geography.country:
            msg = "output plan country must match simulation geography"
            raise ValueError(msg)
        return self


class RowIdentity(StrictContractModel):
    """Fingerprint of the rows in a simulation artifact.

    Names the identifier columns and gives the row count and a SHA-256 over the
    row identifiers, so artifacts can be checked to cover the same rows.
    """

    schema_version: Literal[1] = 1
    identifier_columns: Annotated[tuple[ContractText, ...], Field(min_length=1)]
    row_count: Annotated[int, Field(ge=0)]
    identity_sha256: Sha256Digest

    @field_validator("identifier_columns")
    @classmethod
    def require_unique_columns(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        """Reject repeated identifier column names."""
        if len(value) != len(set(value)):
            msg = "row identifier columns must be unique"
            raise ValueError(msg)
        return value


class SimulationArtifactDescriptor(StrictContractModel):
    """The stored output of one simulation in a comparison run.

    Points to the artifact and records the digest of the output plan it follows,
    its row identity, its bundle provenance and, for SPM calculations, the
    calculation receipt.
    """

    contract_version: Literal[1] = 1
    evaluation_id: UUID
    simulation_execution_id: UUID
    role: SimulationRole
    artifact: ArtifactReference
    output_schema_version: Literal[1] = 1
    output_plan_sha256: Sha256Digest
    row_identity: RowIdentity
    bundle: BundleProvenance
    calculation_provenance: SPMProvenance | None = None


class ReportExecutionInput(StrictContractModel):
    """A baseline and reform simulation pair to run and aggregate into one report.

    Both simulations must belong to this report, carry the baseline and reform
    roles and distinct execution IDs, and differ only in their policy.
    """

    contract_version: Literal[1] = 1
    evaluation_id: UUID
    baseline: SimulationExecutionInput
    reform: SimulationExecutionInput
    requested_aggregates: Annotated[tuple[ReportAggregate, ...], Field(min_length=1)]

    @model_validator(mode="after")
    def require_aligned_simulations(self) -> ReportExecutionInput:
        """Check the two simulations form one comparable report.

        Roles must be baseline and reform, both must belong to this report with
        distinct execution ids, and everything but the policy must match.
        """
        if self.baseline.role is not SimulationRole.BASELINE:
            msg = "baseline input must use the baseline role"
            raise ValueError(msg)
        if self.reform.role is not SimulationRole.REFORM:
            msg = "reform input must use the reform role"
            raise ValueError(msg)
        if self.baseline.evaluation_id != self.evaluation_id:
            msg = "baseline evaluation_id must match the report"
            raise ValueError(msg)
        if self.reform.evaluation_id != self.evaluation_id:
            msg = "reform evaluation_id must match the report"
            raise ValueError(msg)
        if self.baseline.simulation_execution_id == self.reform.simulation_execution_id:
            msg = "baseline and reform simulation identifiers must differ"
            raise ValueError(msg)
        for field_name in (
            "population",
            "year",
            "geography",
            "options",
            "bundle",
        ):
            if getattr(self.baseline, field_name) != getattr(self.reform, field_name):
                msg = f"baseline and reform {field_name} must match before execution"
                raise ValueError(msg)
        if len(self.requested_aggregates) != len(set(self.requested_aggregates)):
            msg = "requested aggregates must be unique"
            raise ValueError(msg)
        return self


class AggregateReportArtifactDescriptor(StrictContractModel):
    """The stored aggregate results of a comparison run.

    Records the digests of the baseline and reform artifacts the results were
    computed from, and the bundle provenance.
    """

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
        """Reject repeated aggregate names."""
        if len(value) != len(set(value)):
            msg = "requested aggregates must be unique"
            raise ValueError(msg)
        return value

    @model_validator(mode="after")
    def validate_spm_provenance(self) -> AggregateReportArtifactPayload:
        """Reject duplicate or noncanonical SPM data in aggregate artifacts."""
        if "spm_config" in self.result:
            msg = "aggregate result must not include legacy spm_config"
            raise ValueError(msg)
        # Reports without US SPM calculations serialize this optional field
        # as null. Validate a supplied record, not the absence of one.
        provenance = self.result.get("spm_provenance")
        if provenance is not None:
            SPMComparisonProvenance.model_validate(provenance)
        return self


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
        """Check presence flags against values, and that deltas come as a pair.

        An absent side must be null, and a relative delta needs an absolute one;
        only an absolute delta may stand alone (a zero production value).
        """
        if not self.production_present and not self.stage12_present:
            msg = "a result difference must be present in at least one result"
            raise ValueError(msg)
        if not self.production_present and self.production_value is not None:
            msg = "an absent production value must be null"
            raise ValueError(msg)
        if not self.stage12_present and self.stage12_value is not None:
            msg = "an absent Stage 12 value must be null"
            raise ValueError(msg)
        # A zero production value has no finite relative delta, so an absolute
        # delta with no relative delta is the only permitted partial pair.
        if (self.absolute_delta is None) != (self.relative_delta is None) and (
            self.absolute_delta is None or self.relative_delta is not None
        ):
            msg = "numeric deltas must be supplied together"
            raise ValueError(msg)
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
        """Require a timezone-aware timestamp and a count and status that match."""
        if self.compared_at.tzinfo is None:
            msg = "comparison timestamp must include a timezone"
            raise ValueError(msg)
        if self.difference_count != len(self.differences):
            msg = "difference_count must equal the number of differences"
            raise ValueError(msg)
        expected_status = "matched" if not self.differences else "different"
        if self.status != expected_status:
            msg = "comparison status does not match its differences"
            raise ValueError(msg)
        return self


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
