"""A report run that names a simulation run that already succeeded reuses its output.

The coordinator never claims, starts or calculates such a run again: it reads
the run's output file from whichever bucket its URI names, checks the file's
digest and planned outputs, and aggregates it as if a worker had just written
it. A file it can't use fails the report run instead.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import dataclass, field
from functools import partial
from hashlib import sha256
from types import SimpleNamespace
from uuid import UUID

import pandas as pd
import pytest
from google.api_core.exceptions import NotFound
from policyengine_simulation_contract.stage12_execution import (
    ComparisonRunLifecycleStatus,
    ReportRunIds,
    SimulationRole,
)
from policyengine_simulation_executor import spm as runtime_spm
from policyengine_simulation_executor.stage12_artifacts import (
    serialize_simulation_frames,
)
from policyengine_simulation_executor.stage12_runtime import (
    report_coordination as report_coordination_module,
)
from policyengine_simulation_executor.stage12_runtime import (
    reused_outputs as reused_outputs_module,
)
from policyengine_simulation_executor.stage12_runtime.reused_outputs import (
    CloudStorageOutputFiles,
)
from policyengine_stage12_persistence import RunStatus
from stage12_child_call_support import (
    ConcurrentInvoker,
    InjectedChildFaults,
    TimeoutInvoker,
)
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
    NOW,
    REFORM_ID,
    coordinate_report,
    parent_record,
    planned_simulation,
    report_execution_input,
    spm_calculation_provenance,
)

COORDINATOR_CALL_ID = "fc-coordinator"
# Another bucket than the Stage 12 artifact bucket the fakes write to.
OUTPUT_BUCKET = "detailed-save-outputs"
# A catalog row the report's simulations don't use.
OTHER_CATALOG_ID = UUID("00000000-0000-0000-0000-0000000000f1")
# The complete SPM selection ``spm_calculation_provenance``'s receipt records.
SPM_SELECTION = {
    "forecast_content_sha256": "f" * 64,
    "scenario": "official",
    "geography_kind": "national",
    "geography_id": None,
    "county_vintage": "2020",
    "as_of": None,
}


@dataclass
class ResolvedSpmSelection:
    """What the runtime resolves a simulation's SPM request to, and what it read."""

    selection: dict | None = None
    requests: list[dict] = field(default_factory=list)

    def resolve(self, params):
        self.requests.append(params)
        return self.selection


@pytest.fixture(autouse=True)
def resolved_spm(monkeypatch) -> ResolvedSpmSelection:
    """Resolve every SPM request to no SPM calculation, unless a test sets one."""
    resolved = ResolvedSpmSelection()
    monkeypatch.setattr(runtime_spm, "normalize_runtime_spm", resolved.resolve)
    return resolved


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


def _succeeded_with_output(
    report_runs, files, run_id, *, frames=None, calculation_provenance=None
):
    """Mark a simulation run succeeded with a stored output file of ``frames``."""
    frames = frames or simulation_frames()
    payload, _row_identity = serialize_simulation_frames(
        frames, calculation_provenance=calculation_provenance
    )
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
    **seams,
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
        **seams,
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


def _unrequested_spm_calculation(report_runs, files):
    return _succeeded_with_output(
        report_runs,
        files,
        BASELINE_RUN_ID,
        calculation_provenance=spm_calculation_provenance(),
    )


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
    "unrequested-spm-calculation": (
        _unrequested_spm_calculation,
        "reused_simulation_spm_mismatch",
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


@pytest.mark.parametrize(
    ("column", "recorded"),
    [("dataset_id", OTHER_CATALOG_ID), ("region_id", None)],
    ids=["other-dataset", "no-region"],
)
def test_a_reused_run_must_share_the_dataset_and_region_of_its_report_runs_other_run(
    column, recorded
) -> None:
    # The baseline and reform of one report calculate one population and
    # geography, so the report run's two simulation runs name one catalog
    # dataset and region.
    store = FakeStore()
    report_runs = FakeReportRuns()
    files = FakeOutputFiles()
    _succeeded_with_output(report_runs, files, BASELINE_RUN_ID)
    setattr(report_runs.simulation_runs[BASELINE_RUN_ID], column, recorded)
    artifacts = FakeArtifacts()
    invoker = ConcurrentInvoker(artifacts)

    with pytest.raises(RuntimeError, match="coordination failed"):
        _coordinate(store, report_runs, invoker, artifacts, files)

    message = report_runs.report_runs[REPORT_RUN_ID].error_message
    assert message.startswith("reused_simulation_run_mismatch: ")
    assert f"baseline simulation run {BASELINE_RUN_ID}" in message
    assert f"reform simulation run {REFORM_RUN_ID}" in message
    assert f"{column} {recorded or 'none'} instead of " in message
    assert invoker.events == []
    for run_id in (BASELINE_RUN_ID, REFORM_RUN_ID):
        assert report_runs.simulation_runs[run_id].writes == []


def test_a_reused_output_without_the_resolved_spm_calculation_fails_the_report(
    resolved_spm,
) -> None:
    # The runtime resolves a US request with no SPM option to the bundle's
    # certified defaults, so a worker would calculate SPM and write a receipt.
    resolved_spm.selection = SPM_SELECTION
    store = FakeStore()
    report_runs = FakeReportRuns()
    files = FakeOutputFiles()
    _succeeded_with_output(report_runs, files, BASELINE_RUN_ID)
    artifacts = FakeArtifacts()
    invoker = ConcurrentInvoker(artifacts)

    with pytest.raises(RuntimeError, match="coordination failed"):
        _coordinate(store, report_runs, invoker, artifacts, files)

    report_run = report_runs.report_runs[REPORT_RUN_ID]
    assert report_run.error_message.startswith("reused_simulation_spm_mismatch: ")
    assert "without an SPM calculation receipt" in report_run.error_message
    assert invoker.events == []
    assert report_runs.simulation_runs[BASELINE_RUN_ID].writes == []


def _reused_under_spm_receipt(report_runs, files, run_ids, receipt):
    for run_id in run_ids:
        _succeeded_with_output(
            report_runs, files, run_id, calculation_provenance=receipt
        )


OTHER_SPM_SELECTIONS = {
    "scenario": {**SPM_SELECTION, "scenario": "zero_real"},
    "geography_kind": {**SPM_SELECTION, "geography_kind": "county"},
    "county_vintage": {**SPM_SELECTION, "county_vintage": "2010"},
}


@pytest.mark.parametrize(
    "reused_run_ids",
    [(BASELINE_RUN_ID, REFORM_RUN_ID), (BASELINE_RUN_ID,), (REFORM_RUN_ID,)],
    ids=["both-reused", "baseline-reused", "reform-reused"],
)
@pytest.mark.parametrize(
    ("differing_field", "requested_selection"),
    list(OTHER_SPM_SELECTIONS.items()),
    ids=list(OTHER_SPM_SELECTIONS),
)
def test_outputs_reused_from_another_spm_selection_fail_before_any_worker_starts(
    resolved_spm, reused_run_ids, differing_field, requested_selection
) -> None:
    resolved_spm.selection = requested_selection
    store = FakeStore()
    report_runs = FakeReportRuns()
    files = FakeOutputFiles()
    _reused_under_spm_receipt(
        report_runs, files, reused_run_ids, spm_calculation_provenance()
    )
    artifacts = FakeArtifacts()
    invoker = ConcurrentInvoker(artifacts)

    with pytest.raises(RuntimeError, match="coordination failed"):
        _coordinate(store, report_runs, invoker, artifacts, files)

    report_run = report_runs.report_runs[REPORT_RUN_ID]
    assert report_run.status is RunStatus.FAILED
    assert report_run.error_message.startswith("reused_simulation_spm_mismatch: ")
    assert f"SPM receipt {differing_field} differs" in report_run.error_message
    # Neither simulation is calculated, and no simulation run is written.
    assert invoker.events == []
    for run_id in (BASELINE_RUN_ID, REFORM_RUN_ID):
        assert report_runs.simulation_runs[run_id].writes == []
    assert store.parent.error_code == "reused_simulation_spm_mismatch"


def test_an_output_whose_spm_receipt_lacks_the_report_year_fails_the_report(
    resolved_spm,
) -> None:
    resolved_spm.selection = SPM_SELECTION
    report_runs = FakeReportRuns()
    files = FakeOutputFiles()
    _reused_under_spm_receipt(
        report_runs,
        files,
        (BASELINE_RUN_ID,),
        spm_calculation_provenance().model_copy(update={"years": ("2027",)}),
    )
    artifacts = FakeArtifacts()
    invoker = ConcurrentInvoker(artifacts)

    with pytest.raises(RuntimeError, match="coordination failed"):
        _coordinate(FakeStore(), report_runs, invoker, artifacts, files)

    report_run = report_runs.report_runs[REPORT_RUN_ID]
    assert report_run.error_message.startswith("reused_simulation_spm_mismatch: ")
    assert "covers 2027, not 2026" in report_run.error_message
    assert invoker.events == []


def test_an_output_with_the_resolved_spm_selection_is_reused(resolved_spm) -> None:
    resolved_spm.selection = SPM_SELECTION
    report_runs = FakeReportRuns()
    files = FakeOutputFiles()
    receipt = spm_calculation_provenance()
    _reused_under_spm_receipt(report_runs, files, (BASELINE_RUN_ID,), receipt)
    artifacts = FakeArtifacts()
    invoker = ConcurrentInvoker(artifacts)
    aggregated = []

    def recording_aggregator(**inputs):
        aggregated.append(inputs)
        return economy_aggregate()

    _coordinate(
        FakeStore(),
        report_runs,
        invoker,
        artifacts,
        files,
        aggregator=recording_aggregator,
    )

    assert invoker.events == ["spawn:reform"]
    assert report_runs.report_runs[REPORT_RUN_ID].status is RunStatus.SUCCEEDED
    (inputs,) = aggregated
    assert inputs["baseline"].descriptor.calculation_provenance == receipt
    # The selection is resolved from the reused simulation's own parameters.
    (request,) = resolved_spm.requests
    assert request["time_period"] == "2026"
    assert request["country"] == "us"


SECOND_REPORT_RUN_ID = UUID("00000000-0000-0000-0000-0000000000a2")
SECOND_REFORM_RUN_ID = UUID("00000000-0000-0000-0000-0000000000c2")


def test_two_report_runs_reusing_one_succeeded_run_at_once_leave_it_unchanged() -> None:
    """Two report runs share one succeeded baseline run and its output file.

    Each report run has its own Stage 12 report: in production its own
    evaluation, with its own parent and child rows, which never interact with
    another evaluation's. So each coordinator gets its own Stage 12 store,
    standing in for those separate rows of one database; the test inputs give
    both reports the same evaluation ID, which one shared store would treat
    as one report claimed twice. What the two report runs do share, the run
    tables and the output file, is shared: the fake report run store and the
    output file reader. The check is that reusing, succeeding and failing in
    one report run writes nothing to the shared run and doesn't change the
    other report run.
    """
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


def test_a_succeeded_reform_run_is_reused_while_its_baseline_is_calculated() -> None:
    store = FakeStore()
    report_runs = FakeReportRuns()
    files = FakeOutputFiles()
    stored = _succeeded_with_output(
        report_runs, files, REFORM_RUN_ID, frames=simulation_frames(120.0)
    )
    artifacts = FakeArtifacts()
    invoker = ConcurrentInvoker(artifacts)

    result = _coordinate(store, report_runs, invoker, artifacts, files)

    assert invoker.events == ["spawn:baseline"]
    assert set(store.children) == {BASELINE_ID}
    assert report_runs.simulation_runs[REFORM_RUN_ID].writes == []
    assert report_runs.report_runs[REPORT_RUN_ID].status is RunStatus.SUCCEEDED
    assert result["reform_artifact_sha256"] == stored.sha256
    assert files.reads == [stored.uri]


class RestartInvoker(ConcurrentInvoker):
    """Start new children; an earlier attempt's restored child can only be cancelled."""

    def __init__(self, artifacts):
        super().__init__(artifacts)
        self.cancelled: list[str] = []

    def restore(self, invocation_id):
        self.events.append(f"restore:{invocation_id}")
        return SimpleNamespace(
            object_id=invocation_id,
            cancel=partial(self.cancelled.append, invocation_id),
        )


def _preempt(_seconds):
    raise KeyboardInterrupt


def test_a_restarted_call_reuses_the_output_its_own_baseline_worker_recorded() -> None:
    store = FakeStore()
    report_runs = FakeReportRuns()
    files = FakeOutputFiles()
    artifacts = FakeArtifacts()
    # The first attempt starts both children, then Modal pre-empts it.
    with pytest.raises(KeyboardInterrupt):
        _coordinate(
            store, report_runs, TimeoutInvoker(), artifacts, files, sleep=_preempt
        )
    # Before the pre-empted call cancelled it, the baseline's worker wrote its
    # output and recorded its child record and simulation run as succeeded.
    descriptor = artifacts.add_simulation(planned_simulation(SimulationRole.BASELINE))
    files.payloads[descriptor.artifact.uri] = artifacts.payloads[
        descriptor.artifact.uri
    ]
    store.children[BASELINE_ID] = store.children[BASELINE_ID].model_copy(
        update={
            "status": ComparisonRunLifecycleStatus.SUCCEEDED,
            "output_uri": descriptor.artifact.uri,
            "output_sha256": descriptor.artifact.content_sha256,
            "output_schema_version": 1,
            "row_identity_columns": descriptor.row_identity.identifier_columns,
            "row_count": descriptor.row_identity.row_count,
            "row_identity_sha256": descriptor.row_identity.identity_sha256,
            "completed_at": NOW,
        }
    )
    baseline_run = report_runs.simulation_runs[BASELINE_RUN_ID]
    baseline_run.status = RunStatus.SUCCEEDED
    baseline_run.modal_call_id = "fc-baseline-worker"
    baseline_run.output_uri = descriptor.artifact.uri
    baseline_run.output_sha256 = descriptor.artifact.content_sha256
    baseline_before = deepcopy(baseline_run)
    restart_invoker = RestartInvoker(artifacts)

    result = _coordinate(store, report_runs, restart_invoker, artifacts, files)

    # Only the reform starts again, after its earlier call is cancelled.
    assert restart_invoker.events == ["restore:call-reform", "spawn:reform"]
    assert restart_invoker.cancelled == ["call-reform"]
    assert files.reads == [descriptor.artifact.uri]
    assert result["baseline_artifact_sha256"] == descriptor.artifact.content_sha256
    assert report_runs.simulation_runs[BASELINE_RUN_ID] == baseline_before
    assert store.children[BASELINE_ID].status is ComparisonRunLifecycleStatus.SUCCEEDED
    report_run = report_runs.report_runs[REPORT_RUN_ID]
    assert report_run.status is RunStatus.SUCCEEDED
    assert report_run.writes == [
        "claim_report_run",
        "claim_report_run",
        "complete_report_run",
    ]


def test_a_reused_output_file_is_read_and_decoded_once(monkeypatch) -> None:
    decoded_digests: list[str] = []

    def counting_decoder(module):
        decode = module.deserialize_simulation_frames

        def counted(payload):
            decoded_digests.append(sha256(payload).hexdigest())
            return decode(payload)

        return counted

    # Every module of the coordinator that decodes a simulation output file.
    for module in (reused_outputs_module, report_coordination_module):
        if hasattr(module, "deserialize_simulation_frames"):
            monkeypatch.setattr(
                module, "deserialize_simulation_frames", counting_decoder(module)
            )
    report_runs = FakeReportRuns()
    files = FakeOutputFiles()
    stored = _succeeded_with_output(report_runs, files, BASELINE_RUN_ID)
    artifacts = FakeArtifacts()

    _coordinate(
        FakeStore(), report_runs, ConcurrentInvoker(artifacts), artifacts, files
    )

    assert files.reads == [stored.uri]
    assert decoded_digests.count(stored.sha256) == 1


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
