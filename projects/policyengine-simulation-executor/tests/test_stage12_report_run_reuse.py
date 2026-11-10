"""A report run that names a simulation run that already succeeded reuses its output.

The coordinator never claims, starts or calculates such a run again: it reads
the run's output file from whichever bucket its URI names, checks the file's
digest and planned outputs, and aggregates it as if a worker had just written
it. A file it can't use fails the report run instead.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import dataclass
from hashlib import sha256
from types import SimpleNamespace
from uuid import UUID

import pandas as pd
import pytest
from google.api_core.exceptions import NotFound
from policyengine_simulation_contract.stage12_execution import (
    ComparisonRunLifecycleStatus,
    ReportRunIds,
)
from policyengine_simulation_executor.stage12_artifacts import (
    serialize_simulation_frames,
)
from policyengine_simulation_executor.stage12_runtime.reused_outputs import (
    CloudStorageOutputFiles,
)
from policyengine_stage12_persistence import RunStatus
from stage12_child_call_support import ConcurrentInvoker, InjectedChildFaults
from stage12_fake_report_runs import (
    BASELINE_RUN_ID,
    REFORM_RUN_ID,
    REPORT_RUN_ID,
    FakeReportRuns,
    FakeRun,
    economy_aggregate,
    report_run_context,
)
from stage12_fake_stores import FakeArtifacts, FakeStore, simulation_frames
from stage12_runtime_support import (
    BASELINE_ID,
    REFORM_ID,
    coordinate_report,
    parent_record,
    report_execution_input,
)

COORDINATOR_CALL_ID = "fc-coordinator"
# Another bucket than the Stage 12 artifact bucket the fakes write to.
OUTPUT_BUCKET = "detailed-save-outputs"


class FakeOutputFiles:
    """Stored output files by URI, recording each read."""

    def __init__(self) -> None:
        self.payloads: dict[str, bytes] = {}
        self.failures: dict[str, Exception] = {}
        self.reads: list[str] = []

    def read(self, uri: str) -> bytes:
        self.reads.append(uri)
        if uri in self.failures:
            raise self.failures[uri]
        if uri not in self.payloads:
            raise FileNotFoundError(uri)
        return self.payloads[uri]


@dataclass(frozen=True)
class StoredOutput:
    """An output file a simulation run recorded when it succeeded."""

    uri: str
    sha256: str
    frames: dict[str, pd.DataFrame]


def _succeeded_with_output(report_runs, files, run_id, *, frames=None):
    """Mark a simulation run succeeded with a stored output file of ``frames``."""
    frames = frames or simulation_frames()
    payload, _row_identity = serialize_simulation_frames(frames)
    stored = StoredOutput(
        uri=f"gs://{OUTPUT_BUCKET}/simulations/{run_id}.parquet",
        sha256=sha256(payload).hexdigest(),
        frames=frames,
    )
    files.payloads[stored.uri] = payload
    run = report_runs.simulation_runs[run_id]
    run.status = RunStatus.SUCCEEDED
    run.output_uri, run.output_sha256 = stored.uri, stored.sha256
    return stored


def _coordinate(  # noqa: PLR0913  # the stores, the seams and the call's identity
    store,
    report_runs,
    invoker,
    artifacts,
    files,
    *,
    context=None,
    coordinator_call_id=COORDINATOR_CALL_ID,
    aggregator=economy_aggregate,
):
    context = context or report_run_context()
    return coordinate_report(
        report_execution_input().model_dump(mode="json"),
        context.model_dump(mode="json"),
        parent_record().model_dump(mode="json"),
        application_name=context.modal_application,
        coordinator_invocation_id=coordinator_call_id,
        store=store,
        artifacts=artifacts,
        invoker=invoker,
        aggregator=aggregator,
        report_runs=report_runs,
        output_files=files,
    )


def test_a_succeeded_baseline_run_is_aggregated_from_its_output_without_a_worker() -> (
    None
):
    store = FakeStore()
    report_runs = FakeReportRuns()
    files = FakeOutputFiles()
    stored = _succeeded_with_output(
        report_runs,
        files,
        BASELINE_RUN_ID,
        frames=simulation_frames(90.0),
    )
    artifacts = FakeArtifacts()
    invoker = ConcurrentInvoker(artifacts)
    aggregated = []

    def recording_aggregator(**inputs):
        aggregated.append(inputs)
        return economy_aggregate()

    result = _coordinate(
        store, report_runs, invoker, artifacts, files, aggregator=recording_aggregator
    )

    # Only the reform is calculated; the baseline run is never claimed again.
    assert invoker.events == ["spawn:reform"]
    assert report_runs.report_runs[REPORT_RUN_ID].status is RunStatus.SUCCEEDED
    baseline_run = report_runs.simulation_runs[BASELINE_RUN_ID]
    assert baseline_run.writes == []
    assert (
        baseline_run.status,
        baseline_run.output_uri,
        baseline_run.output_sha256,
    ) == (
        RunStatus.SUCCEEDED,
        stored.uri,
        stored.sha256,
    )
    # No worker coordinates the reused run, so it has no Stage 12 child record.
    assert set(store.children) == {REFORM_ID}
    assert files.reads == [stored.uri]
    (inputs,) = aggregated
    reused = inputs["baseline"]
    assert reused.descriptor.simulation_execution_id == BASELINE_ID
    assert reused.descriptor.artifact.uri == stored.uri
    assert reused.descriptor.artifact.content_sha256 == stored.sha256
    pd.testing.assert_frame_equal(
        reused.frames["household"], stored.frames["household"]
    )
    assert result["baseline_artifact_sha256"] == stored.sha256
    assert store.parent.status is ComparisonRunLifecycleStatus.SUCCEEDED


def test_a_report_run_whose_two_runs_succeeded_starts_no_worker() -> None:
    store = FakeStore()
    report_runs = FakeReportRuns()
    files = FakeOutputFiles()
    baseline = _succeeded_with_output(report_runs, files, BASELINE_RUN_ID)
    reform = _succeeded_with_output(
        report_runs,
        files,
        REFORM_RUN_ID,
        frames=simulation_frames(120.0),
    )
    artifacts = FakeArtifacts()
    invoker = ConcurrentInvoker(artifacts)

    result = _coordinate(store, report_runs, invoker, artifacts, files)

    assert invoker.events == []
    assert store.children == {}
    assert report_runs.report_runs[REPORT_RUN_ID].status is RunStatus.SUCCEEDED
    assert report_runs.report_runs[REPORT_RUN_ID].writes == [
        "claim_report_run",
        "complete_report_run",
    ]
    for run_id in (BASELINE_RUN_ID, REFORM_RUN_ID):
        assert report_runs.simulation_runs[run_id].writes == []
    assert sorted(files.reads) == sorted([baseline.uri, reform.uri])
    assert result["baseline_artifact_sha256"] == baseline.sha256
    assert result["reform_artifact_sha256"] == reform.sha256


@pytest.mark.parametrize("status", [RunStatus.PENDING, RunStatus.RUNNING])
def test_a_pending_or_running_simulation_run_is_calculated_as_before(status) -> None:
    report_runs = FakeReportRuns()
    report_runs.simulation_runs[BASELINE_RUN_ID].status = status
    files = FakeOutputFiles()
    artifacts = FakeArtifacts()
    invoker = ConcurrentInvoker(artifacts)

    _coordinate(FakeStore(), report_runs, invoker, artifacts, files)

    assert sorted(invoker.events) == ["spawn:baseline", "spawn:reform"]
    assert files.reads == []
    assert report_runs.report_runs[REPORT_RUN_ID].status is RunStatus.SUCCEEDED


def _no_output_recorded(report_runs, files):
    stored = _succeeded_with_output(report_runs, files, BASELINE_RUN_ID)
    run = report_runs.simulation_runs[BASELINE_RUN_ID]
    run.output_uri = run.output_sha256 = None
    return stored


def _file_deleted(report_runs, files):
    stored = _succeeded_with_output(report_runs, files, BASELINE_RUN_ID)
    del files.payloads[stored.uri]
    return stored


def _storage_unavailable(report_runs, files):
    stored = _succeeded_with_output(report_runs, files, BASELINE_RUN_ID)
    files.failures[stored.uri] = ConnectionError("the storage service is unavailable")
    return stored


def _not_a_simulation_output(report_runs, files):
    stored = _succeeded_with_output(report_runs, files, BASELINE_RUN_ID)
    payload = b"not a Parquet file"
    files.payloads[stored.uri] = payload
    report_runs.simulation_runs[BASELINE_RUN_ID].output_sha256 = sha256(
        payload
    ).hexdigest()
    return stored


def _not_a_cloud_storage_object(report_runs, files):
    stored = _succeeded_with_output(report_runs, files, BASELINE_RUN_ID)
    report_runs.simulation_runs[BASELINE_RUN_ID].output_uri = "/tmp/baseline.parquet"
    return stored


def _other_digest(report_runs, files):
    stored = _succeeded_with_output(report_runs, files, BASELINE_RUN_ID)
    report_runs.simulation_runs[BASELINE_RUN_ID].output_sha256 = "0" * 64
    return stored


def _missing_planned_column(report_runs, files):
    frames = simulation_frames()
    frames["household"] = frames["household"].drop(columns=["household_net_income"])
    return _succeeded_with_output(report_runs, files, BASELINE_RUN_ID, frames=frames)


def _other_year(report_runs, files):
    stored = _succeeded_with_output(report_runs, files, BASELINE_RUN_ID)
    report_runs.simulation_runs[BASELINE_RUN_ID].year = 2025
    return stored


def _other_policyengine_version(report_runs, files):
    stored = _succeeded_with_output(report_runs, files, BASELINE_RUN_ID)
    report_runs.simulation_runs[BASELINE_RUN_ID].policyengine_version = "5.1.0"
    return stored


def _other_country_package_version(report_runs, files):
    stored = _succeeded_with_output(report_runs, files, BASELINE_RUN_ID)
    report_runs.simulation_runs[BASELINE_RUN_ID].country_package_version = "1.700.0"
    return stored


UNUSABLE_OUTPUTS = {
    "no-output-recorded": (_no_output_recorded, "reused_simulation_output_missing"),
    "file-deleted": (_file_deleted, "reused_simulation_output_missing"),
    "storage-unavailable": (
        _storage_unavailable,
        "reused_simulation_output_unreadable",
    ),
    "not-a-simulation-output": (
        _not_a_simulation_output,
        "reused_simulation_output_unreadable",
    ),
    "not-a-cloud-storage-object": (
        _not_a_cloud_storage_object,
        "reused_simulation_output_unreadable",
    ),
    "other-digest": (_other_digest, "reused_simulation_output_digest_mismatch"),
    "missing-planned-column": (
        _missing_planned_column,
        "reused_simulation_output_incompatible",
    ),
    "other-year": (_other_year, "reused_simulation_run_mismatch"),
    "other-policyengine-version": (
        _other_policyengine_version,
        "reused_simulation_run_mismatch",
    ),
    "other-country-package-version": (
        _other_country_package_version,
        "reused_simulation_run_mismatch",
    ),
}


@pytest.mark.parametrize(
    ("make_unusable", "error_code"),
    list(UNUSABLE_OUTPUTS.values()),
    ids=list(UNUSABLE_OUTPUTS),
)
def test_a_reused_output_that_cannot_be_used_fails_the_report_run_uncalculated(
    make_unusable, error_code
) -> None:
    store = FakeStore()
    report_runs = FakeReportRuns()
    files = FakeOutputFiles()
    make_unusable(report_runs, files)
    baseline_before = deepcopy(report_runs.simulation_runs[BASELINE_RUN_ID])
    artifacts = FakeArtifacts()
    invoker = ConcurrentInvoker(artifacts)

    with pytest.raises(RuntimeError, match="coordination failed"):
        _coordinate(store, report_runs, invoker, artifacts, files)

    report_run = report_runs.report_runs[REPORT_RUN_ID]
    assert report_run.status is RunStatus.FAILED
    assert report_run.error_message.startswith(f"{error_code}: ")
    assert f"baseline simulation run {BASELINE_RUN_ID}" in report_run.error_message
    # Nothing is calculated, and neither simulation run is written.
    assert invoker.events == []
    assert report_runs.simulation_runs[BASELINE_RUN_ID] == baseline_before
    assert report_runs.simulation_runs[REFORM_RUN_ID].writes == []
    assert store.parent.status is ComparisonRunLifecycleStatus.FAILED
    assert store.parent.error_code == error_code


SECOND_REPORT_RUN_ID = UUID("00000000-0000-0000-0000-0000000000a2")
SECOND_REFORM_RUN_ID = UUID("00000000-0000-0000-0000-0000000000c2")


def test_two_report_runs_reusing_one_succeeded_run_at_once_leave_it_unchanged() -> None:
    report_runs = FakeReportRuns()
    report_runs.report_runs[SECOND_REPORT_RUN_ID] = FakeRun()
    report_runs.simulation_runs[SECOND_REFORM_RUN_ID] = FakeRun()
    report_runs.simulation_run_pairs[SECOND_REPORT_RUN_ID] = (
        BASELINE_RUN_ID,
        SECOND_REFORM_RUN_ID,
    )
    files = FakeOutputFiles()
    shared = _succeeded_with_output(report_runs, files, BASELINE_RUN_ID)
    shared_before = deepcopy(report_runs.simulation_runs[BASELINE_RUN_ID])
    second_context = report_run_context().model_copy(
        update={
            "report_run": ReportRunIds(
                report_run_id=SECOND_REPORT_RUN_ID,
                baseline_simulation_run_id=BASELINE_RUN_ID,
                reform_simulation_run_id=SECOND_REFORM_RUN_ID,
            )
        }
    )
    first_artifacts, second_artifacts = FakeArtifacts(), FakeArtifacts()
    first_invoker = ConcurrentInvoker(first_artifacts)
    # The second report's reform fails; that failure is its report run's alone.
    second_invoker = ConcurrentInvoker(
        second_artifacts, faults=InjectedChildFaults(fail_role="reform")
    )

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(
            _coordinate,
            FakeStore(),
            report_runs,
            first_invoker,
            first_artifacts,
            files,
            coordinator_call_id="fc-first-coordinator",
        )
        second = executor.submit(
            _coordinate,
            FakeStore(),
            report_runs,
            second_invoker,
            second_artifacts,
            files,
            context=second_context,
            coordinator_call_id="fc-second-coordinator",
        )
        first_result = first.result()
        with pytest.raises(RuntimeError, match="coordination failed"):
            second.result()

    assert first_result["baseline_artifact_sha256"] == shared.sha256
    assert report_runs.report_runs[REPORT_RUN_ID].status is RunStatus.SUCCEEDED
    assert report_runs.report_runs[SECOND_REPORT_RUN_ID].status is RunStatus.FAILED
    assert report_runs.simulation_runs[SECOND_REFORM_RUN_ID].status is RunStatus.FAILED
    # Neither report run wrote to the run they share.
    assert report_runs.simulation_runs[BASELINE_RUN_ID] == shared_before
    assert files.reads == [shared.uri, shared.uri]
    assert first_invoker.events == ["spawn:reform"]
    assert second_invoker.events == ["spawn:reform"]


class FakeStorageClient:
    """Objects by bucket and path, read as the Cloud Storage client reads them."""

    def __init__(self, objects: dict[tuple[str, str], bytes]) -> None:
        self.objects = objects

    def bucket(self, bucket_name):
        return SimpleNamespace(
            blob=lambda path: SimpleNamespace(
                download_as_bytes=lambda: self._download(bucket_name, path)
            )
        )

    def _download(self, bucket_name, path):
        if (bucket_name, path) not in self.objects:
            raise NotFound(path)
        return self.objects[bucket_name, path]


def test_stored_output_files_are_read_from_the_bucket_their_uri_names() -> None:
    client = FakeStorageClient(
        {
            (OUTPUT_BUCKET, "simulations/baseline.parquet"): b"baseline",
            ("stage12-artifacts", "simulations/reform.parquet"): b"reform",
        }
    )
    files = CloudStorageOutputFiles(client=client)

    assert files.read(f"gs://{OUTPUT_BUCKET}/simulations/baseline.parquet") == (
        b"baseline"
    )
    assert files.read("gs://stage12-artifacts/simulations/reform.parquet") == (
        b"reform"
    )


def test_a_stored_output_file_that_does_not_exist_raises_file_not_found() -> None:
    files = CloudStorageOutputFiles(client=FakeStorageClient({}))

    with pytest.raises(FileNotFoundError):
        files.read(f"gs://{OUTPUT_BUCKET}/simulations/deleted.parquet")


@pytest.mark.parametrize(
    "uri",
    [
        "/tmp/baseline.parquet",
        "hf://policyengine/baseline.parquet",
        f"gs://{OUTPUT_BUCKET}/",
    ],
)
def test_an_output_uri_that_names_no_cloud_storage_object_is_refused(uri) -> None:
    files = CloudStorageOutputFiles(client=FakeStorageClient({}))

    with pytest.raises(ValueError, match="Cloud Storage object"):
        files.read(uri)
