"""Artifact keys that name their content, for calls Modal may restart."""

from __future__ import annotations

from hashlib import sha256

import pytest
from fixtures.fake_object_store import InMemoryObjectStore
from policyengine_simulation_contract.stage12_execution import SimulationRole
from policyengine_simulation_executor.stage12_artifacts import Stage12ArtifactStore
from stage12_fake_stores import simulation_frames
from stage12_runtime_support import aggregate_payload, planned_simulation

PREFIX = "stage-12-runs/staging/2026/11/report-run"


def _store(*, content_addressed: bool = True) -> Stage12ArtifactStore:
    return Stage12ArtifactStore(
        "private-stage12",
        store=InMemoryObjectStore(),
        content_addressed=content_addressed,
    )


def test_content_named_inputs_and_aggregates_name_their_digest() -> None:
    store = _store()
    reform = planned_simulation(SimulationRole.REFORM)

    planned_input = store.write_input(prefix=PREFIX, simulation=reform)
    aggregate = store.write_aggregate(prefix=PREFIX, payload=aggregate_payload())

    assert planned_input.uri == (
        f"gs://private-stage12/{PREFIX}/inputs/reform-"
        f"{planned_input.content_sha256}.json"
    )
    assert aggregate.uri == (
        f"gs://private-stage12/{PREFIX}/reports/aggregate-"
        f"{aggregate.content_sha256}.json"
    )
    assert sha256(store.read(aggregate.uri)).hexdigest() == aggregate.content_sha256


def test_comparison_runs_keep_their_fixed_write_once_keys() -> None:
    store = _store(content_addressed=False)
    baseline = planned_simulation(SimulationRole.BASELINE)

    first = store.write_simulation(
        prefix=PREFIX, simulation=baseline, frames=simulation_frames(100.0)
    )

    assert first.artifact.uri == (
        f"gs://private-stage12/{PREFIX}/simulations/baseline.parquet"
    )
    with pytest.raises(RuntimeError, match="other data"):
        store.write_simulation(
            prefix=PREFIX, simulation=baseline, frames=simulation_frames(100.000001)
        )
