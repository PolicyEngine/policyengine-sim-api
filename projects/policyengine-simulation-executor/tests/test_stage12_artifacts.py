"""Tests for canonical private Stage 12 artifacts."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import numpy as np
import pandas as pd
import pytest

from policyengine_simulation_executor.stage12_artifacts import (
    Stage12ArtifactStore,
    comparison_run_prefix,
    deserialize_calculation_provenance,
    deserialize_simulation_frames,
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


def test_parquet_retains_detached_calculation_provenance() -> None:
    provenance = {
        "spm_config": {"scenario": "official"},
        "spm_provenance": {"forecast_sha256": "a" * 64},
    }
    payload, _ = serialize_simulation_frames(
        _frames(),
        calculation_provenance=provenance,
    )

    assert deserialize_calculation_provenance(payload) == provenance


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
