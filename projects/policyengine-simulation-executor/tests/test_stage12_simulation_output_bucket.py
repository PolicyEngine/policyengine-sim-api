"""A report run's simulation output files go to their own bucket, which keeps them.

The Stage 12 artifact bucket deletes every object after 30 days; a later
report run may reuse a succeeded simulation run's file, so a call that names
a report run writes it to the simulation output bucket instead.
"""

from __future__ import annotations

from hashlib import sha256
from uuid import UUID

import pytest
from fixtures.fake_object_store import InMemoryObjectStore
from policyengine_simulation_contract.stage12_execution import SimulationRole
from policyengine_simulation_executor import stage12_artifacts
from policyengine_simulation_executor.stage12_artifacts import (
    SimulationOutputBucket,
    SimulationRunOutputLocation,
    Stage12ArtifactStore,
    simulation_run_output_path,
)
from policyengine_simulation_executor.stage12_runtime.dependencies import (
    artifact_store,
)
from stage12_fake_report_runs import REFORM_RUN_ID, report_run_context
from stage12_fake_stores import simulation_frames
from stage12_runtime_support import invocation_context, planned_simulation

ARTIFACT_BUCKET = "policyengine-stage12-staging-artifacts"
OUTPUT_BUCKET = "policyengine-v2-staging-simulation-outputs"
SIMULATION_RUN_ID = UUID("00000000-0000-0000-0000-0000000000b1")
PREFIX = "stage-12-runs/staging/2026/11/report-run"
LOCATION = SimulationRunOutputLocation(
    environment="staging", simulation_run_id=SIMULATION_RUN_ID
)


def _report_run_store(
    artifact_objects: InMemoryObjectStore | None = None,
    output_objects: InMemoryObjectStore | None = None,
) -> Stage12ArtifactStore:
    return Stage12ArtifactStore(
        ARTIFACT_BUCKET,
        store=artifact_objects or InMemoryObjectStore(),
        content_addressed=True,
        simulation_outputs=SimulationOutputBucket(
            OUTPUT_BUCKET, store=output_objects or InMemoryObjectStore()
        ),
    )


def test_run_output_path_names_environment_country_run_and_digest() -> None:
    assert simulation_run_output_path(
        environment="production",
        country="uk",
        simulation_run_id=SIMULATION_RUN_ID,
        content_sha256="a" * 64,
    ) == (
        f"simulation-runs/production/uk/{SIMULATION_RUN_ID}/output-{'a' * 64}.parquet"
    )


@pytest.mark.parametrize("environment", ["", "staging/other", ".."])
def test_a_simulation_run_output_refuses_an_environment_outside_its_prefix(
    environment: str,
) -> None:
    with pytest.raises(ValueError, match="invalid Stage 12 artifact environment"):
        simulation_run_output_path(
            environment=environment,
            country="us",
            simulation_run_id=SIMULATION_RUN_ID,
            content_sha256="a" * 64,
        )


def test_a_simulation_run_output_goes_to_the_simulation_output_bucket() -> None:
    artifact_objects = InMemoryObjectStore()
    output_objects = InMemoryObjectStore()
    store = _report_run_store(artifact_objects, output_objects)
    baseline = planned_simulation(SimulationRole.BASELINE)

    descriptor = store.write_simulation_run_output(
        LOCATION,
        simulation=baseline,
        frames=simulation_frames(),
    )

    digest = descriptor.artifact.content_sha256
    path = f"simulation-runs/staging/us/{SIMULATION_RUN_ID}/output-{digest}.parquet"
    assert descriptor.artifact.uri == f"gs://{OUTPUT_BUCKET}/{path}"
    assert list(output_objects.objects) == [path]
    assert artifact_objects.objects == {}
    assert descriptor.role is SimulationRole.BASELINE
    assert descriptor.simulation_execution_id == baseline.simulation_execution_id
    payload = store.read(descriptor.artifact.uri)
    assert sha256(payload).hexdigest() == digest
    assert descriptor.artifact.size_bytes == len(payload)


def test_a_restarted_simulation_run_keeps_each_different_output_apart() -> None:
    output_objects = InMemoryObjectStore()
    store = _report_run_store(output_objects=output_objects)
    baseline = planned_simulation(SimulationRole.BASELINE)

    def write(value: float):
        return store.write_simulation_run_output(
            LOCATION,
            simulation=baseline,
            frames=simulation_frames(value),
        )

    first = write(100.0)
    # A restarted call can calculate different last bits for the same run.
    second = write(100.000001)
    repeated = write(100.0)

    assert second.artifact.uri != first.artifact.uri
    assert repeated.artifact == first.artifact
    assert len(output_objects.objects) == 2


def test_a_store_without_a_simulation_output_bucket_refuses_a_run_output() -> None:
    artifact_objects = InMemoryObjectStore()
    store = Stage12ArtifactStore(
        ARTIFACT_BUCKET, store=artifact_objects, content_addressed=True
    )

    with pytest.raises(ValueError, match="simulation output bucket"):
        store.write_simulation_run_output(
            LOCATION,
            simulation=planned_simulation(SimulationRole.BASELINE),
            frames=simulation_frames(),
        )

    assert artifact_objects.objects == {}


def test_the_simulation_output_bucket_is_never_the_artifact_bucket() -> None:
    with pytest.raises(ValueError, match="differ from the Stage 12 artifact bucket"):
        Stage12ArtifactStore(
            ARTIFACT_BUCKET,
            store=InMemoryObjectStore(),
            simulation_outputs=SimulationOutputBucket(
                ARTIFACT_BUCKET, store=InMemoryObjectStore()
            ),
        )


def test_a_simulation_output_bucket_needs_a_name() -> None:
    with pytest.raises(ValueError, match="explicitly configured"):
        SimulationOutputBucket("", store=InMemoryObjectStore())


def test_the_store_reads_its_artifact_and_simulation_output_buckets_only() -> None:
    store = _report_run_store()
    planned_input = store.write_input(
        prefix=PREFIX, simulation=planned_simulation(SimulationRole.REFORM)
    )
    output = store.write_simulation_run_output(
        LOCATION,
        simulation=planned_simulation(SimulationRole.REFORM),
        frames=simulation_frames(),
    )

    assert planned_input.uri.startswith(f"gs://{ARTIFACT_BUCKET}/{PREFIX}/inputs/")
    assert store.read(planned_input.uri)
    assert store.read(output.artifact.uri)
    with pytest.raises(FileNotFoundError):
        store.read(f"gs://{OUTPUT_BUCKET}/simulation-runs/missing.parquet")
    with pytest.raises(ValueError, match="another bucket"):
        store.read("gs://another-bucket/simulation-runs/output.parquet")


def test_the_store_keeps_a_report_runs_output_with_its_simulation_run() -> None:
    artifact_objects = InMemoryObjectStore()
    output_objects = InMemoryObjectStore()
    store = _report_run_store(artifact_objects, output_objects)

    descriptor = store.write_simulation_output(
        report_run_context(),
        simulation=planned_simulation(SimulationRole.REFORM),
        frames=simulation_frames(),
    )

    digest = descriptor.artifact.content_sha256
    path = f"simulation-runs/staging/us/{REFORM_RUN_ID}/output-{digest}.parquet"
    assert descriptor.artifact.uri == f"gs://{OUTPUT_BUCKET}/{path}"
    assert list(output_objects.objects) == [path]
    assert artifact_objects.objects == {}


def test_the_store_keeps_a_comparison_runs_output_under_its_prefix() -> None:
    artifact_objects = InMemoryObjectStore()
    output_objects = InMemoryObjectStore()
    # An output bucket is configured, but a comparison run never uses it.
    store = Stage12ArtifactStore(
        ARTIFACT_BUCKET,
        store=artifact_objects,
        simulation_outputs=SimulationOutputBucket(OUTPUT_BUCKET, store=output_objects),
    )

    descriptor = store.write_simulation_output(
        invocation_context(),
        simulation=planned_simulation(SimulationRole.BASELINE),
        frames=simulation_frames(),
    )

    path = f"{invocation_context().artifact_prefix}/simulations/baseline.parquet"
    assert descriptor.artifact.uri == f"gs://{ARTIFACT_BUCKET}/{path}"
    assert list(artifact_objects.objects) == [path]
    assert output_objects.objects == {}


@pytest.fixture
def in_memory_buckets(monkeypatch) -> dict[str, InMemoryObjectStore]:
    """Replace each bucket ``artifact_store`` opens with an in-memory store."""
    buckets: dict[str, InMemoryObjectStore] = {}

    def open_bucket(bucket_name: str) -> InMemoryObjectStore:
        return buckets.setdefault(bucket_name, InMemoryObjectStore())

    monkeypatch.setattr(stage12_artifacts, "ArtifactStore", open_bucket)
    monkeypatch.setenv("STAGE12_ARTIFACT_BUCKET", ARTIFACT_BUCKET)
    return buckets


def test_a_report_run_store_writes_run_outputs_to_the_configured_bucket(
    monkeypatch, in_memory_buckets
) -> None:
    monkeypatch.setenv("V2_SIMULATION_OUTPUT_BUCKET", OUTPUT_BUCKET)

    store = artifact_store(report_run=True)
    output = store.write_simulation_run_output(
        LOCATION,
        simulation=planned_simulation(SimulationRole.BASELINE),
        frames=simulation_frames(),
    )
    planned_input = store.write_input(
        prefix=PREFIX, simulation=planned_simulation(SimulationRole.BASELINE)
    )

    assert output.artifact.uri.startswith(f"gs://{OUTPUT_BUCKET}/simulation-runs/")
    # A report run's store still names each artifact's digest in its key.
    assert planned_input.uri.endswith(f"-{planned_input.content_sha256}.json")
    assert set(in_memory_buckets) == {ARTIFACT_BUCKET, OUTPUT_BUCKET}


def test_a_report_run_store_without_the_output_bucket_variable_fails(
    monkeypatch, in_memory_buckets
) -> None:
    monkeypatch.delenv("V2_SIMULATION_OUTPUT_BUCKET", raising=False)

    with pytest.raises(ValueError, match="V2_SIMULATION_OUTPUT_BUCKET"):
        artifact_store(report_run=True)

    assert in_memory_buckets == {}


def test_a_comparison_store_needs_no_simulation_output_bucket(
    monkeypatch, in_memory_buckets
) -> None:
    monkeypatch.delenv("V2_SIMULATION_OUTPUT_BUCKET", raising=False)

    store = artifact_store()
    planned_input = store.write_input(
        prefix=PREFIX, simulation=planned_simulation(SimulationRole.BASELINE)
    )

    assert planned_input.uri == f"gs://{ARTIFACT_BUCKET}/{PREFIX}/inputs/baseline.json"
    assert set(in_memory_buckets) == {ARTIFACT_BUCKET}
