"""Canonical private artifact encoding and paths for Stage 12 comparison runs."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime
from hashlib import sha256
from io import BytesIO
from typing import Any, cast
from uuid import UUID

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
from policyengine_simulation_contract.stage12_execution import (
    SIMULATION_PARQUET_PAYLOAD_CONTRACT,
    AggregateReportArtifactPayload,
    ArtifactMediaType,
    ArtifactReference,
    ResultComparisonArtifactPayload,
    RowIdentity,
    SimulationArtifactDescriptor,
    SimulationExecutionInput,
)
from pydantic import JsonValue

from policyengine_simulation_executor.artifact_store import ArtifactStore

SIMULATION_MEDIA_TYPE = ArtifactMediaType.PARQUET.value
REPORT_MEDIA_TYPE = ArtifactMediaType.JSON.value
PARQUET_CONTRACT = SIMULATION_PARQUET_PAYLOAD_CONTRACT


def canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def comparison_run_prefix(
    *,
    environment: str,
    created_at: datetime,
    evaluation_id: UUID,
) -> str:
    if not environment or "/" in environment or ".." in environment:
        raise ValueError("invalid Stage 12 artifact environment")
    if created_at.tzinfo is None:
        raise ValueError("Stage 12 artifact timestamp must include a timezone")
    return (
        f"stage-12-runs/{environment}/{created_at:%Y}/{created_at:%m}/{evaluation_id}"
    )


def input_path(*, prefix: str, role: str) -> str:
    return f"{prefix}/inputs/{role}.json"


def simulation_path(*, prefix: str, role: str) -> str:
    return f"{prefix}/simulations/{role}.parquet"


def aggregate_path(*, prefix: str) -> str:
    return f"{prefix}/reports/aggregate.json"


def comparison_path(*, prefix: str) -> str:
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
            raise ValueError(f"simulation output entity {entity!r} has no {identifier}")
        if source[identifier].duplicated().any():
            raise ValueError(f"simulation output entity {entity!r} has duplicate IDs")
        source = source.sort_values(identifier, kind="stable").reset_index(drop=True)
        dtypes[entity] = {column: str(dtype) for column, dtype in source.dtypes.items()}
        identifier_values[entity] = [
            str(value) for value in source[identifier].tolist()
        ]
        source.insert(0, PARQUET_CONTRACT.row_order_column, range(len(source)))
        source.insert(0, PARQUET_CONTRACT.entity_column, entity)
        parts.append(source)
    if not parts:
        raise ValueError("simulation output contains no entity tables")
    combined = pd.concat(parts, ignore_index=True, sort=False)
    columns = [
        PARQUET_CONTRACT.entity_column,
        PARQUET_CONTRACT.row_order_column,
    ] + sorted(
        column
        for column in combined.columns
        if column
        not in {
            PARQUET_CONTRACT.entity_column,
            PARQUET_CONTRACT.row_order_column,
        }
    )
    return combined.reindex(columns=columns), dtypes, identifier_values


def serialize_simulation_frames(
    frames: Mapping[str, pd.DataFrame],
    *,
    calculation_provenance: Mapping[str, Any] | None = None,
) -> tuple[bytes, RowIdentity]:
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
            canonical_json_bytes(calculation_provenance)
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
    table = pq.read_table(BytesIO(payload))
    metadata = table.schema.metadata or {}
    if (
        metadata.get(PARQUET_CONTRACT.schema_version_metadata_key.encode())
        != str(PARQUET_CONTRACT.payload_schema_version).encode()
    ):
        raise ValueError("unsupported Stage 12 simulation artifact schema")
    raw_dtypes = metadata.get(PARQUET_CONTRACT.dtype_metadata_key.encode())
    if raw_dtypes is None:
        raise ValueError("Stage 12 simulation artifact has no dtype metadata")
    dtypes = json.loads(raw_dtypes)
    combined = table.to_pandas()
    frames: dict[str, pd.DataFrame] = {}
    for entity in sorted(combined[PARQUET_CONTRACT.entity_column].unique()):
        frame = combined.loc[combined[PARQUET_CONTRACT.entity_column] == entity].copy()
        frame = frame.sort_values(PARQUET_CONTRACT.row_order_column, kind="stable")
        frame = frame.drop(
            columns=[
                PARQUET_CONTRACT.entity_column,
                PARQUET_CONTRACT.row_order_column,
            ]
        )
        frame = frame.dropna(axis=1, how="all").reset_index(drop=True)
        for column, dtype in dtypes[entity].items():
            if column in frame.columns and str(frame[column].dtype) != dtype:
                frame[column] = frame[column].astype(dtype)
        frames[str(entity)] = frame.reindex(columns=sorted(frame.columns))
    return frames


def deserialize_calculation_provenance(payload: bytes) -> dict[str, Any] | None:
    table = pq.read_table(BytesIO(payload))
    raw = (table.schema.metadata or {}).get(
        PARQUET_CONTRACT.calculation_provenance_metadata_key.encode()
    )
    if raw is None:
        return None
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise TypeError("Stage 12 calculation provenance must be an object")
    return value


class Stage12ArtifactStore:
    def __init__(self, bucket_name: str, *, store: ArtifactStore | None = None):
        if not bucket_name:
            raise ValueError(
                "Stage 12 requires an explicitly configured private bucket"
            )
        self.bucket_name = bucket_name
        self._store = store or ArtifactStore(bucket_name)

    def _write_immutable(
        self,
        path: str,
        payload: bytes,
        *,
        content_type: str,
    ) -> ArtifactReference:
        digest = sha256(payload).hexdigest()
        created = self._store.upload_bytes(
            path,
            payload,
            content_type=content_type,
        )
        if not created:
            existing = self._store.read_bytes(path)
            if existing is None or sha256(existing).hexdigest() != digest:
                raise RuntimeError(
                    "immutable Stage 12 artifact path contains other data"
                )
        return ArtifactReference(
            uri=f"gs://{self.bucket_name}/{path}",
            media_type=ArtifactMediaType(content_type),
            content_sha256=digest,
            size_bytes=len(payload),
        )

    def write_input(
        self,
        *,
        prefix: str,
        simulation: SimulationExecutionInput,
    ) -> ArtifactReference:
        return self._write_immutable(
            input_path(prefix=prefix, role=simulation.role.value),
            canonical_json_bytes(simulation.model_dump(mode="json")),
            content_type=REPORT_MEDIA_TYPE,
        )

    def write_simulation(
        self,
        *,
        prefix: str,
        simulation: SimulationExecutionInput,
        frames: Mapping[str, pd.DataFrame],
        calculation_provenance: Mapping[str, Any] | None = None,
    ) -> SimulationArtifactDescriptor:
        normalized_provenance = cast(
            dict[str, JsonValue] | None,
            (
                dict(calculation_provenance)
                if calculation_provenance is not None
                else None
            ),
        )
        payload, row_identity = serialize_simulation_frames(
            frames,
            calculation_provenance=normalized_provenance,
        )
        artifact = self._write_immutable(
            simulation_path(prefix=prefix, role=simulation.role.value),
            payload,
            content_type=SIMULATION_MEDIA_TYPE,
        )
        return SimulationArtifactDescriptor(
            evaluation_id=simulation.evaluation_id,
            simulation_execution_id=simulation.simulation_execution_id,
            role=simulation.role,
            artifact=artifact,
            row_identity=row_identity,
            bundle=simulation.bundle,
            calculation_provenance=normalized_provenance,
        )

    def write_aggregate(
        self,
        *,
        prefix: str,
        payload: Mapping[str, Any],
    ) -> ArtifactReference:
        validated = AggregateReportArtifactPayload.model_validate(payload)
        return self._write_immutable(
            aggregate_path(prefix=prefix),
            canonical_json_bytes(validated.model_dump(mode="json")),
            content_type=REPORT_MEDIA_TYPE,
        )

    def write_comparison(
        self,
        *,
        prefix: str,
        payload: Mapping[str, Any],
    ) -> ArtifactReference:
        validated = ResultComparisonArtifactPayload.model_validate(payload)
        return self._write_immutable(
            comparison_path(prefix=prefix),
            canonical_json_bytes(validated.model_dump(mode="json")),
            content_type=REPORT_MEDIA_TYPE,
        )

    def read(self, uri: str) -> bytes:
        expected_prefix = f"gs://{self.bucket_name}/"
        if not uri.startswith(expected_prefix):
            raise ValueError("Stage 12 artifact URI names another bucket")
        payload = self._store.read_bytes(uri.removeprefix(expected_prefix))
        if payload is None:
            raise FileNotFoundError(uri)
        return payload
