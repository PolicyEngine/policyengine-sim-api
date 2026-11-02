"""Field types, the strict contract base, and one simulation's execution input."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

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
