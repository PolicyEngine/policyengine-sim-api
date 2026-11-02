"""The stored simulation, report and comparison artifacts of a comparison run."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, JsonValue, field_validator, model_validator

from policyengine_simulation_contract.spm import SPMComparisonProvenance, SPMProvenance

# Models subclass these bases through the package's path, which ruff.toml
# lists as runtime-evaluated; base.py binds them in the package first.
from policyengine_simulation_contract.stage12_execution import StrictContractModel
from policyengine_simulation_contract.stage12_execution.base import (
    ArtifactReference,
    BundleProvenance,
    ContractText,
    Sha256Digest,
    SimulationRole,
)
from policyengine_simulation_contract.stage12_execution.report_inputs import (
    ReportAggregate,
)


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
