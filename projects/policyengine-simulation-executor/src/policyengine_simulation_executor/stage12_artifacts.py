"""Canonical private artifact encoding and paths for Stage 12 comparison runs."""

from __future__ import annotations

import json
from dataclasses import dataclass
from hashlib import sha256
from io import BytesIO
from typing import TYPE_CHECKING, Any

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
from policyengine_simulation_contract.spm import SPMProvenance
from policyengine_simulation_contract.stage12_execution import (
    SIMULATION_PARQUET_PAYLOAD_CONTRACT,
    AggregateReportArtifactPayload,
    ArtifactMediaType,
    ArtifactReference,
    PlannedSimulationExecutionInput,
    ResultComparisonArtifactPayload,
    RowIdentity,
    SimulationArtifactDescriptor,
    stage12_output_plan_sha256,
)
from policyengine_simulation_contract.uk_geography import UKLocalAuthorityMetadata

from policyengine_simulation_executor.artifact_store import ArtifactStore

if TYPE_CHECKING:
    from collections.abc import Mapping
    from datetime import datetime
    from uuid import UUID

SIMULATION_MEDIA_TYPE = ArtifactMediaType.PARQUET.value
REPORT_MEDIA_TYPE = ArtifactMediaType.JSON.value
PARQUET_CONTRACT = SIMULATION_PARQUET_PAYLOAD_CONTRACT


def canonical_json_bytes(value: object) -> bytes:
    """Encode ``value`` as compact, key-sorted UTF-8 JSON so equal values hash equally.

    NaN and infinity raise ``ValueError`` rather than producing non-standard JSON.
    """
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _require_environment_segment(environment: str) -> None:
    """Raise ``ValueError`` for an environment that could escape its key prefix."""
    if not environment or "/" in environment or ".." in environment:
        msg = "invalid Stage 12 artifact environment"
        raise ValueError(msg)


def comparison_run_prefix(
    *,
    environment: str,
    created_at: datetime,
    evaluation_id: UUID,
) -> str:
    """Return ``stage-12-runs/<environment>/<YYYY>/<MM>/<evaluation_id>`` for one run.

    Raises ``ValueError`` for an environment that could escape the prefix or a
    naive ``created_at``.
    """
    _require_environment_segment(environment)
    if created_at.tzinfo is None:
        msg = "Stage 12 artifact timestamp must include a timezone"
        raise ValueError(msg)
    return (
        f"stage-12-runs/{environment}/{created_at:%Y}/{created_at:%m}/{evaluation_id}"
    )


def _content_named(name: str, content_sha256: str | None) -> str:
    """Return ``name``, followed by ``-<content_sha256>`` when a digest is given."""
    return name if content_sha256 is None else f"{name}-{content_sha256}"


def input_path(*, prefix: str, role: str, content_sha256: str | None = None) -> str:
    """Return the key of the planned execution input JSON for one simulation role.

    With ``content_sha256``, the key also names the content's SHA-256.
    """
    return f"{prefix}/inputs/{_content_named(role, content_sha256)}.json"


def simulation_path(
    *, prefix: str, role: str, content_sha256: str | None = None
) -> str:
    """Return the key of the Parquet output for one simulation role.

    With ``content_sha256``, the key also names the content's SHA-256.
    """
    return f"{prefix}/simulations/{_content_named(role, content_sha256)}.parquet"


def simulation_run_output_path(
    *,
    environment: str,
    country: str,
    simulation_run_id: UUID,
    content_sha256: str,
) -> str:
    """Return the key of a report run's simulation output file in its own bucket.

    The key is
    ``simulation-runs/<environment>/<country>/<simulation_run_id>/output-<content_sha256>.parquet``,
    so a call Modal restarts can store other bytes beside its earlier
    attempt's. Raises ``ValueError`` for an environment that could escape the
    prefix.
    """
    _require_environment_segment(environment)
    return (
        f"simulation-runs/{environment}/{country}/{simulation_run_id}/"
        f"output-{content_sha256}.parquet"
    )


def aggregate_path(*, prefix: str, content_sha256: str | None = None) -> str:
    """Return the key of the run's aggregate report JSON.

    With ``content_sha256``, the key also names the content's SHA-256.
    """
    return f"{prefix}/reports/{_content_named('aggregate', content_sha256)}.json"


def comparison_path(*, prefix: str) -> str:
    """Return the key of the run's result-comparison report JSON."""
    return f"{prefix}/reports/comparison.json"


def _frame_payload(
    frames: Mapping[str, pd.DataFrame],
) -> tuple[pd.DataFrame, dict[str, dict[str, str]], dict[str, list[str]]]:
    parts = []
    dtypes: dict[str, dict[str, str]] = {}
    identifier_values: dict[str, list[str]] = {}
    for entity in sorted(frames):
        source = frames[entity].copy()
        source = source.reindex(sorted(source.columns), axis=1)
        identifier = PARQUET_CONTRACT.identifier_column_template.format(entity=entity)
        if identifier not in source.columns:
            msg = f"simulation output entity {entity!r} has no {identifier}"
            raise ValueError(msg)
        if source[identifier].duplicated().any():
            msg = f"simulation output entity {entity!r} has duplicate IDs"
            raise ValueError(msg)
        source = source.sort_values(identifier, kind="stable").reset_index(drop=True)
        dtypes[entity] = {column: str(dtype) for column, dtype in source.dtypes.items()}
        identifier_values[entity] = [
            str(value) for value in source[identifier].tolist()
        ]
        source.insert(0, PARQUET_CONTRACT.row_order_column, range(len(source)))
        source.insert(0, PARQUET_CONTRACT.entity_column, entity)
        parts.append(source)
    if not parts:
        msg = "simulation output contains no entity tables"
        raise ValueError(msg)
    combined = pd.concat(parts, ignore_index=True, sort=False)
    columns = [
        PARQUET_CONTRACT.entity_column,
        PARQUET_CONTRACT.row_order_column,
        *sorted(
            column
            for column in combined.columns
            if column
            not in {
                PARQUET_CONTRACT.entity_column,
                PARQUET_CONTRACT.row_order_column,
            }
        ),
    ]
    return combined.reindex(columns=columns), dtypes, identifier_values


def serialize_simulation_frames(
    frames: Mapping[str, pd.DataFrame],
    *,
    calculation_provenance: SPMProvenance | None = None,
    uk_local_authority_metadata: UKLocalAuthorityMetadata | None = None,
) -> tuple[bytes, RowIdentity]:
    """Encode entity frames as one deterministic Parquet payload plus its row identity.

    Rows are ordered by each entity's ``<entity>_id`` column, which must exist
    and be unique (``ValueError`` otherwise). Source dtypes and any SPM or UK
    local authority metadata travel in the Parquet schema metadata.
    """
    combined, dtypes, identifier_values = _frame_payload(frames)
    identity_payload = canonical_json_bytes(identifier_values)
    row_identity = RowIdentity(
        identifier_columns=tuple(
            f"{entity}.{entity}_id" for entity in sorted(identifier_values)
        ),
        row_count=sum(len(values) for values in identifier_values.values()),
        identity_sha256=sha256(identity_payload).hexdigest(),
    )
    table = pa.Table.from_pandas(combined, preserve_index=False)
    metadata = dict(table.schema.metadata or {})
    metadata[PARQUET_CONTRACT.schema_version_metadata_key.encode()] = str(
        PARQUET_CONTRACT.payload_schema_version
    ).encode()
    metadata[PARQUET_CONTRACT.dtype_metadata_key.encode()] = canonical_json_bytes(
        dtypes
    )
    if calculation_provenance is not None:
        metadata[PARQUET_CONTRACT.calculation_provenance_metadata_key.encode()] = (
            canonical_json_bytes(
                calculation_provenance.model_dump(mode="json", by_alias=True)
            )
        )
    if uk_local_authority_metadata is not None:
        metadata[PARQUET_CONTRACT.uk_local_authority_metadata_key.encode()] = (
            canonical_json_bytes(uk_local_authority_metadata.model_dump(mode="json"))
        )
    table = table.replace_schema_metadata(metadata)
    buffer = BytesIO()
    pq.write_table(
        table,
        buffer,
        compression=PARQUET_CONTRACT.compression,
        version=PARQUET_CONTRACT.parquet_version,
        data_page_version=PARQUET_CONTRACT.data_page_version,
        write_statistics=False,
        use_dictionary=False,
        row_group_size=65_536,
    )
    return buffer.getvalue(), row_identity


def deserialize_simulation_frames(payload: bytes) -> dict[str, pd.DataFrame]:
    """Decode a simulation payload back into per-entity frames with their source dtypes.

    Raises ``ValueError`` for an unsupported schema version or missing dtype
    metadata.
    """
    table = pq.read_table(BytesIO(payload))
    metadata = table.schema.metadata or {}
    if (
        metadata.get(PARQUET_CONTRACT.schema_version_metadata_key.encode())
        != str(PARQUET_CONTRACT.payload_schema_version).encode()
    ):
        msg = "unsupported Stage 12 simulation artifact schema"
        raise ValueError(msg)
    raw_dtypes = metadata.get(PARQUET_CONTRACT.dtype_metadata_key.encode())
    if raw_dtypes is None:
        msg = "Stage 12 simulation artifact has no dtype metadata"
        raise ValueError(msg)
    dtypes = json.loads(raw_dtypes)
    if not isinstance(dtypes, dict):
        msg = "Stage 12 simulation artifact dtype metadata is invalid"
        raise TypeError(msg)
    combined = table.to_pandas()
    frames: dict[str, pd.DataFrame] = {}
    for entity in sorted(combined[PARQUET_CONTRACT.entity_column].unique()):
        entity_name = str(entity)
        entity_dtypes = dtypes.get(entity_name)
        if not isinstance(entity_dtypes, dict):
            msg = f"Stage 12 simulation artifact has no dtype schema for {entity_name}"
            raise ValueError(msg)  # noqa: TRY004  # documented ValueError for missing dtype metadata; the coordinator persists the type name as error_summary
        frame = combined.loc[combined[PARQUET_CONTRACT.entity_column] == entity].copy()
        frame = frame.sort_values(PARQUET_CONTRACT.row_order_column, kind="stable")
        frame = frame.drop(
            columns=[
                PARQUET_CONTRACT.entity_column,
                PARQUET_CONTRACT.row_order_column,
            ]
        )
        # The dtype metadata is the authoritative per-entity schema. Inferring
        # ownership by dropping all-null columns loses legitimate planned
        # outputs whose values happen to be null for every row.
        frame = frame.reindex(columns=sorted(entity_dtypes)).reset_index(drop=True)
        for column, dtype in entity_dtypes.items():
            if column in frame.columns and str(frame[column].dtype) != dtype:
                frame[column] = frame[column].astype(dtype)
        frames[entity_name] = frame
    return frames


def _parquet_schema_metadata(payload: bytes) -> Mapping[bytes, bytes]:
    """Read artifact metadata without materializing its simulation rows."""
    parquet_file = pq.ParquetFile(BytesIO(payload))
    return parquet_file.schema_arrow.metadata or {}


def deserialize_calculation_provenance(
    payload: bytes,
) -> SPMProvenance | None:
    """Read a simulation artifact's SPM calculation receipt, or None if absent."""
    raw = _parquet_schema_metadata(payload).get(
        PARQUET_CONTRACT.calculation_provenance_metadata_key.encode()
    )
    if raw is None:
        return None
    return SPMProvenance.model_validate_json(raw)


def deserialize_uk_local_authority_metadata(
    payload: bytes,
) -> UKLocalAuthorityMetadata | None:
    """Read typed UK authority metadata from a Stage 12 simulation artifact."""
    raw = _parquet_schema_metadata(payload).get(
        PARQUET_CONTRACT.uk_local_authority_metadata_key.encode()
    )
    if raw is None:
        return None
    return UKLocalAuthorityMetadata.model_validate_json(raw)


class Stage12ArtifactStore:
    """Write-once storage for one private bucket's Stage 12 run artifacts.

    A write to a path already holding identical bytes succeeds; different
    bytes raise ``RuntimeError``. A ``content_addressed`` store names each
    input, simulation output and aggregate's SHA-256 in its key, so a call
    Modal restarts can store other bytes beside its earlier attempt's, and
    identical bytes reuse the earlier object. A store given
    ``simulation_outputs`` also writes a report run's simulation output files
    to that bucket, and reads from both buckets.
    """

    def __init__(
        self,
        bucket_name: str,
        *,
        store: ArtifactStore | None = None,
        content_addressed: bool = False,
        simulation_outputs: SimulationOutputBucket | None = None,
    ) -> None:
        """Bind to ``bucket_name``, which must be non-empty (there is no default).

        ``simulation_outputs`` must name another bucket, so a report run's
        output files are never kept with the artifacts deleted after 30 days.
        """
        if not bucket_name:
            msg = "Stage 12 requires an explicitly configured private bucket"
            raise ValueError(msg)
        if simulation_outputs is not None and (
            simulation_outputs.bucket_name == bucket_name
        ):
            msg = (
                "the simulation output bucket must differ from the Stage 12 "
                "artifact bucket"
            )
            raise ValueError(msg)
        self.bucket_name = bucket_name
        self._store = store or ArtifactStore(bucket_name)
        self._content_addressed = content_addressed
        self._simulation_outputs = simulation_outputs

    def _key_digest(self, payload: bytes) -> str | None:
        """Return the SHA-256 a content-addressed key names, else None."""
        return sha256(payload).hexdigest() if self._content_addressed else None

    def _write_immutable(
        self,
        path: str,
        payload: bytes,
        *,
        content_type: str,
    ) -> ArtifactReference:
        return _write_once(
            self._store,
            f"gs://{self.bucket_name}/",
            path,
            payload,
            content_type=content_type,
        )

    def write_input(
        self,
        *,
        prefix: str,
        simulation: PlannedSimulationExecutionInput,
    ) -> ArtifactReference:
        """Store a simulation's planned execution input as canonical JSON."""
        payload = canonical_json_bytes(simulation.model_dump(mode="json"))
        return self._write_immutable(
            input_path(
                prefix=prefix,
                role=simulation.role.value,
                content_sha256=self._key_digest(payload),
            ),
            payload,
            content_type=REPORT_MEDIA_TYPE,
        )

    def write_simulation(
        self,
        *,
        prefix: str,
        simulation: PlannedSimulationExecutionInput,
        frames: Mapping[str, pd.DataFrame],
        calculation_provenance: SPMProvenance | None = None,
        uk_local_authority_metadata: UKLocalAuthorityMetadata | None = None,
    ) -> SimulationArtifactDescriptor:
        """Store a simulation's output frames and describe the artifact for reports."""
        payload, row_identity = serialize_simulation_frames(
            frames,
            calculation_provenance=calculation_provenance,
            uk_local_authority_metadata=uk_local_authority_metadata,
        )
        artifact = self._write_immutable(
            simulation_path(
                prefix=prefix,
                role=simulation.role.value,
                content_sha256=self._key_digest(payload),
            ),
            payload,
            content_type=SIMULATION_MEDIA_TYPE,
        )
        return _simulation_descriptor(
            simulation, artifact, row_identity, calculation_provenance
        )

    def write_simulation_run_output(
        self,
        location: SimulationRunOutputLocation,
        *,
        simulation: PlannedSimulationExecutionInput,
        frames: Mapping[str, pd.DataFrame],
        calculation_provenance: SPMProvenance | None = None,
        uk_local_authority_metadata: UKLocalAuthorityMetadata | None = None,
    ) -> SimulationArtifactDescriptor:
        """Store a report run's simulation output file in the simulation output bucket.

        Its key names the file's SHA-256; see ``simulation_run_output_path``.
        Raises ``ValueError`` when this store has no simulation output bucket,
        so the file never falls back to the artifact bucket.
        """
        outputs = self._simulation_outputs
        if outputs is None:
            msg = "a report run's output file needs a simulation output bucket"
            raise ValueError(msg)
        payload, row_identity = serialize_simulation_frames(
            frames,
            calculation_provenance=calculation_provenance,
            uk_local_authority_metadata=uk_local_authority_metadata,
        )
        artifact = _write_once(
            outputs.store,
            f"gs://{outputs.bucket_name}/",
            simulation_run_output_path(
                environment=location.environment,
                country=simulation.geography.country,
                simulation_run_id=location.simulation_run_id,
                content_sha256=sha256(payload).hexdigest(),
            ),
            payload,
            content_type=SIMULATION_MEDIA_TYPE,
        )
        return _simulation_descriptor(
            simulation, artifact, row_identity, calculation_provenance
        )

    def write_aggregate(
        self,
        *,
        prefix: str,
        payload: Mapping[str, Any],
    ) -> ArtifactReference:
        """Validate and store the run's aggregate report."""
        validated = AggregateReportArtifactPayload.model_validate(payload)
        encoded = canonical_json_bytes(validated.model_dump(mode="json"))
        return self._write_immutable(
            aggregate_path(
                prefix=prefix,
                content_sha256=self._key_digest(encoded),
            ),
            encoded,
            content_type=REPORT_MEDIA_TYPE,
        )

    def write_comparison(
        self,
        *,
        prefix: str,
        payload: Mapping[str, Any],
    ) -> ArtifactReference:
        """Validate and store the run's result-comparison report."""
        validated = ResultComparisonArtifactPayload.model_validate(payload)
        return self._write_immutable(
            comparison_path(prefix=prefix),
            canonical_json_bytes(validated.model_dump(mode="json")),
            content_type=REPORT_MEDIA_TYPE,
        )

    def read(self, uri: str) -> bytes:
        """Return the bytes at a ``gs://`` URI in this store's bucket.

        A store with a simulation output bucket also reads that bucket.
        Raises ``ValueError`` for another bucket and ``FileNotFoundError`` when
        the object is missing.
        """
        buckets = [(self.bucket_name, self._store)]
        if self._simulation_outputs is not None:
            buckets.append(
                (self._simulation_outputs.bucket_name, self._simulation_outputs.store)
            )
        for bucket_name, store in buckets:
            expected_prefix = f"gs://{bucket_name}/"
            if uri.startswith(expected_prefix):
                payload = store.read_bytes(uri.removeprefix(expected_prefix))
                if payload is None:
                    raise FileNotFoundError(uri)
                return payload
        msg = "Stage 12 artifact URI names another bucket"
        raise ValueError(msg)


def _write_once(
    store: ArtifactStore,
    uri_prefix: str,
    path: str,
    payload: bytes,
    *,
    content_type: str,
) -> ArtifactReference:
    """Store ``payload`` at ``path`` unless identical bytes are already there.

    Raises ``RuntimeError`` when the path holds other bytes.
    """
    digest = sha256(payload).hexdigest()
    created = store.upload_bytes(path, payload, content_type=content_type)
    if not created:
        existing = store.read_bytes(path)
        if existing is None or sha256(existing).hexdigest() != digest:
            msg = "immutable Stage 12 artifact path contains other data"
            raise RuntimeError(msg)
    return ArtifactReference(
        uri=f"{uri_prefix}{path}",
        media_type=ArtifactMediaType(content_type),
        content_sha256=digest,
        size_bytes=len(payload),
    )


def _simulation_descriptor(
    simulation: PlannedSimulationExecutionInput,
    artifact: ArtifactReference,
    row_identity: RowIdentity,
    calculation_provenance: SPMProvenance | None,
) -> SimulationArtifactDescriptor:
    """Describe a stored simulation output for the coordinator and the records."""
    return SimulationArtifactDescriptor(
        evaluation_id=simulation.evaluation_id,
        simulation_execution_id=simulation.simulation_execution_id,
        role=simulation.role,
        artifact=artifact,
        output_plan_sha256=stage12_output_plan_sha256(simulation.output_plan),
        row_identity=row_identity,
        bundle=simulation.bundle,
        calculation_provenance=calculation_provenance,
    )


class SimulationOutputBucket:
    """The bucket that keeps a report run's simulation output files.

    The Stage 12 artifact bucket deletes every object 30 days after it is
    written. This bucket, one per environment, deletes nothing: its lifecycle
    only moves a file to Coldline after 90 days. A later report run can
    therefore reuse a succeeded simulation run's file.
    """

    def __init__(self, bucket_name: str, *, store: ArtifactStore | None = None) -> None:
        """Bind to ``bucket_name``, which must be non-empty (there is no default)."""
        if not bucket_name:
            msg = "a simulation output bucket must be explicitly configured"
            raise ValueError(msg)
        self.bucket_name = bucket_name
        self.store = store or ArtifactStore(bucket_name)


@dataclass(frozen=True)
class SimulationRunOutputLocation:
    """The environment and simulation run a report run's output file belongs to.

    Its key also names the simulation's country and the file's SHA-256; see
    ``simulation_run_output_path``.
    """

    environment: str
    simulation_run_id: UUID
