"""Tests for canonical private Stage 12 artifacts."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from io import BytesIO
from unittest.mock import patch
from uuid import UUID

import numpy as np
import pandas as pd
import pyarrow.parquet as pq
import pytest
from policyengine_simulation_contract.stage12_execution import (
    UKLocalAuthorityBoundaryVersion,
    UKLocalAuthorityMetadata,
)
from policyengine_simulation_contract.spm import (
    SPMRuntimeVersions,
    SPMSelection,
    build_spm_provenance,
    validate_spm_calculation_provenance,
)
from pydantic import ValidationError

from policyengine_simulation_executor.stage12_artifacts import (
    Stage12ArtifactStore,
    comparison_run_prefix,
    deserialize_calculation_provenance,
    deserialize_simulation_frames,
    deserialize_uk_local_authority_metadata,
    serialize_simulation_frames,
)


def _frames():
    return {
        "household": pd.DataFrame(
            {
                "household_id": [2, 1],
                "household_net_income": np.array([20, 10], dtype=np.float32),
            }
        ),
        "person": pd.DataFrame(
            {
                "person_id": [2, 1],
                "household_id": [1, 1],
                "age": [40, 30],
            }
        ),
    }


def _calculation_provenance():
    selection = SPMSelection(
        forecast_content_sha256="a" * 64,
        scenario="official",
        geography_kind="national",
        geography_id=None,
        county_vintage="2020",
        as_of=None,
    )
    receipt = build_spm_provenance(
        forecast_id="forecast-2026",
        forecast_sha256="a" * 64,
        selection=selection,
        years=("2026",),
        runtime_versions=SPMRuntimeVersions.model_validate(
            {
                "policyengine": "6.2.1",
                "policyengine-core": "3.32.10",
                "policyengine-us": "2.2.1",
                "spm-calculator": "1.0.0",
            }
        ),
    )
    return validate_spm_calculation_provenance(config=selection, receipt=receipt)


def test_parquet_encoding_is_deterministic_and_preserves_rows_and_dtypes() -> None:
    first, first_identity = serialize_simulation_frames(_frames())
    second, second_identity = serialize_simulation_frames(_frames())

    assert first == second
    assert first_identity == second_identity
    assert first_identity.row_count == 4
    restored = deserialize_simulation_frames(first)
    assert restored["household"]["household_id"].tolist() == [1, 2]
    assert str(restored["household"]["household_net_income"].dtype) == "float32"
    assert restored["person"]["person_id"].tolist() == [1, 2]


def test_parquet_round_trip_preserves_an_all_null_entity_column() -> None:
    frames = _frames()
    frames["person"]["planned_value"] = pd.Series(
        [pd.NA, pd.NA],
        dtype="Float64",
    )

    payload, _ = serialize_simulation_frames(frames)
    restored = deserialize_simulation_frames(payload)

    assert "planned_value" in restored["person"].columns
    assert str(restored["person"]["planned_value"].dtype) == "Float64"
    assert restored["person"]["planned_value"].isna().all()


def test_parquet_retains_detached_calculation_provenance() -> None:
    provenance = _calculation_provenance()
    payload, _ = serialize_simulation_frames(
        _frames(),
        calculation_provenance=provenance,
    )

    assert deserialize_calculation_provenance(payload) == provenance


def test_parquet_rejects_old_rich_calculation_provenance() -> None:
    payload, _ = serialize_simulation_frames(_frames())
    table = pq.read_table(BytesIO(payload))
    metadata = dict(table.schema.metadata or {})
    metadata[b"policyengine.stage12.calculation_provenance"] = json.dumps(
        {
            "spm_config": {"scenario": "official"},
            "spm_provenance": {
                "forecast_id": "old-rich-receipt",
                "geographies": [],
            },
        }
    ).encode()
    output = BytesIO()
    pq.write_table(table.replace_schema_metadata(metadata), output)

    with pytest.raises(ValidationError):
        deserialize_calculation_provenance(output.getvalue())


def test_parquet_retains_typed_uk_local_authority_metadata() -> None:
    metadata = UKLocalAuthorityMetadata(
        boundary_version=UKLocalAuthorityBoundaryVersion.LAD22
    )

    payload, _ = serialize_simulation_frames(
        _frames(),
        uk_local_authority_metadata=metadata,
    )

    assert deserialize_uk_local_authority_metadata(payload) == metadata


def test_parquet_omits_uk_metadata_for_non_uk_simulations() -> None:
    payload, _ = serialize_simulation_frames(_frames())

    assert deserialize_uk_local_authority_metadata(payload) is None


def test_metadata_reads_do_not_materialize_simulation_rows() -> None:
    provenance = _calculation_provenance()
    metadata = UKLocalAuthorityMetadata(
        boundary_version=UKLocalAuthorityBoundaryVersion.LAD23
    )
    payload, _ = serialize_simulation_frames(
        _frames(),
        calculation_provenance=provenance,
        uk_local_authority_metadata=metadata,
    )

    with patch(
        "policyengine_simulation_executor.stage12_artifacts.pq.read_table",
        side_effect=AssertionError("metadata reads must not materialize Parquet rows"),
    ):
        assert deserialize_calculation_provenance(payload) == provenance
        assert deserialize_uk_local_authority_metadata(payload) == metadata


def test_duplicate_entity_identifiers_are_rejected() -> None:
    frames = _frames()
    frames["person"]["person_id"] = [1, 1]

    with pytest.raises(ValueError, match="duplicate IDs"):
        serialize_simulation_frames(frames)


def test_private_layout_is_environment_and_date_scoped() -> None:
    prefix = comparison_run_prefix(
        environment="staging",
        created_at=datetime(2026, 9, 14, tzinfo=UTC),
        evaluation_id=UUID("00000000-0000-0000-0000-000000000001"),
    )
    assert prefix == (
        "stage-12-runs/staging/2026/09/00000000-0000-0000-0000-000000000001"
    )


class FakeObjectStore:
    def __init__(self):
        self.values = {}

    def upload_bytes(self, path, payload, *, content_type):
        if path in self.values:
            return False
        self.values[path] = payload
        return True

    def read_bytes(self, path):
        return self.values.get(path)


def test_immutable_retry_accepts_identical_bytes_and_rejects_other_bytes() -> None:
    objects = FakeObjectStore()
    store = Stage12ArtifactStore("private-stage12", store=objects)

    first = store._write_immutable("path", b"same", content_type="application/json")
    second = store._write_immutable("path", b"same", content_type="application/json")
    assert second == first

    with pytest.raises(RuntimeError, match="other data"):
        store._write_immutable("path", b"different", content_type="application/json")


def test_aggregate_write_rejects_an_undeclared_payload_shape() -> None:
    store = Stage12ArtifactStore("private-stage12", store=FakeObjectStore())

    with pytest.raises(ValueError, match="evaluation_id"):
        store.write_aggregate(
            prefix="stage-12-runs/staging/2026/09/comparison-run-1",
            payload={"result": {}},
        )
