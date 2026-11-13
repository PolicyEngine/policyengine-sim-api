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
from importlib.metadata import version as installed_version
from types import SimpleNamespace
from uuid import UUID

import pandas as pd
import pytest
from google.api_core.exceptions import NotFound
from policyengine_simulation_contract.spm import (
    SPMRuntimeVersions,
    SPMSelection,
    build_spm_provenance,
    validate_spm_calculation_provenance,
)
from policyengine_simulation_contract.stage12_execution import ReportRunIds
from policyengine_simulation_executor import spm as runtime_spm
from policyengine_simulation_executor import stage12_artifacts
from policyengine_simulation_executor.stage12_artifacts import (
    deserialize_simulation_frames,
    serialize_simulation_frames,
    simulation_row_identity,
)
from policyengine_simulation_executor.stage12_runtime import build_spm_result
from policyengine_simulation_executor.stage12_runtime.reused_outputs import (
    CloudStorageOutputFiles,
)
from policyengine_stage12_persistence import RunStatus
from stage12_child_call_support import TimeoutInvoker
from stage12_fake_report_runs import (
    BASELINE_RUN_ID,
    REFORM_RUN_ID,
    REPORT_RUN_ID,
    ComparisonStoreNotUsed,
    FakeReportRuns,
    FakeRun,
    economy_aggregate,
    report_run_context,
    report_run_input,
)
from stage12_fake_stores import FakeArtifacts, simulation_frames
from stage12_run_worker_support import RunWorkerInvoker
from stage12_runtime_support import coordinate_report

COORDINATOR_CALL_ID = "fc-coordinator"
# Another bucket than the Stage 12 artifact bucket the fakes write to.
OUTPUT_BUCKET = "detailed-save-outputs"
# A catalog row the report's simulations don't use.
OTHER_CATALOG_ID = UUID("00000000-0000-0000-0000-0000000000f1")
# The certified SPM defaults of the installed .py bundle (6.17.1), which a US
# request takes for each SPM option it leaves out.
CERTIFIED_SPM_DEFAULTS = {
    "as_of": None,
    "county_vintage": "2020",
    "forecast_content_sha256": (
        "3d86d5c4c0423480e6b69b75d222ffa4a7a2639e4094df5ba2504af01be17173"
    ),
    "geography_id": None,
    "geography_kind": "county",
    "scenario": "ce_trend",
}


# The packages an SPM calculation receipt records the versions of.
SPM_RUNTIME_DISTRIBUTIONS = (
    "policyengine",
    "policyengine-core",
    "policyengine-us",
    "spm-calculator",
)
# .py's own resolver, kept before the autouse fixture replaces it.
INSTALLED_NORMALIZE_RUNTIME_SPM = runtime_spm.normalize_runtime_spm


def spm_receipt(selection=CERTIFIED_SPM_DEFAULTS, *, years=("2026",)):
    """Return the receipt a worker writes for an SPM calculation under ``selection``."""
    resolved = SPMSelection(**selection)
    return validate_spm_calculation_provenance(
        config=resolved,
        receipt=build_spm_provenance(
            forecast_id="forecast-1",
            forecast_sha256=selection["forecast_content_sha256"],
            selection=resolved,
            years=years,
            runtime_versions=SPMRuntimeVersions.model_validate(
                {
                    distribution: installed_version(distribution)
                    for distribution in SPM_RUNTIME_DISTRIBUTIONS
                }
            ),
        ),
    )


# What a worker of the shared US report writes when nothing overrides the
# certified defaults.
WORKER_SPM_RECEIPT = spm_receipt()


@dataclass
class InstalledSpmResolution:
    """Resolve SPM requests as the installed bundle does, recording each request.

    A US request takes ``certified_defaults`` for each SPM option it leaves
    out, so it always calculates an SPM measure; None stands for a bundle
    with no certified SPM measurement. Any other country calculates none.
    """

    certified_defaults: dict | None = field(
        default_factory=partial(dict, CERTIFIED_SPM_DEFAULTS)
    )
    requests: list[dict] = field(default_factory=list)

    def resolve(self, params):
        self.requests.append(params)
        if params["country"] != "us" or self.certified_defaults is None:
            return None
        return {**self.certified_defaults, **(params.get("spm") or {})}


@pytest.mark.parametrize(
    "options",
    [{}, {"spm": {"scenario": "ce_trend"}}, {"spm": {"geography_kind": "national"}}],
    ids=["no-options", "ce-trend-scenario", "national-geography"],
)
def test_the_spm_stand_in_resolves_us_requests_as_the_installed_bundle_does(
    options,
) -> None:
    # Fails once a .py upgrade changes the certified defaults the stand-in
    # copies, until CERTIFIED_SPM_DEFAULTS follows it.
    request = {
        "country": "us",
        "scope": "macro",
        "time_period": "2026",
        "region": "us",
        **options,
    }

    assert InstalledSpmResolution().resolve(dict(request)) == (
        INSTALLED_NORMALIZE_RUNTIME_SPM(dict(request))
    )


@pytest.fixture(autouse=True)
def resolved_spm(monkeypatch) -> InstalledSpmResolution:
    """Resolve every SPM request as the installed bundle does."""
    resolution = InstalledSpmResolution()
    monkeypatch.setattr(runtime_spm, "normalize_runtime_spm", resolution.resolve)
    return resolution


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
    report_runs,
    files,
    run_id,
    *,
    frames=None,
    calculation_provenance=WORKER_SPM_RECEIPT,
):
    """Mark a simulation run succeeded with a stored output file of ``frames``.

    The file carries the SPM receipt a worker writes by default; pass
    ``calculation_provenance=None`` for a file with none.
    """
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


def _assert_left_unstarted(simulation_run) -> None:
    """Check a simulation run the failed report never started is left pending.

    Another report run naming it may start it; otherwise the stuck-run sweep
    fails it.
    """
    assert (simulation_run.status, simulation_run.writes) == (RunStatus.PENDING, [])


def _coordinate(  # noqa: PLR0913  # the stores, the seams and the call's identity
    report_runs,
    invoker,
    artifacts,
    files,
    *,
    report=None,
    context=None,
    coordinator_call_id=COORDINATOR_CALL_ID,
    aggregator=economy_aggregate,
    **seams,
):
    report = report or report_run_input()
    context = context or report_run_context()
    return coordinate_report(
        report.model_dump(mode="json"),
        context.model_dump(mode="json"),
        application_name=context.modal_application,
        coordinator_invocation_id=coordinator_call_id,
        store=ComparisonStoreNotUsed(),
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
    report_runs = FakeReportRuns()
    files = FakeOutputFiles()
    stored = _succeeded_with_output(
        report_runs,
        files,
        BASELINE_RUN_ID,
        frames=simulation_frames(90.0),
    )
    artifacts = FakeArtifacts()
    invoker = RunWorkerInvoker(report_runs, artifacts)
    aggregated = []

    def recording_aggregator(**inputs):
        aggregated.append(inputs)
        return economy_aggregate()

    result = _coordinate(
        report_runs, invoker, artifacts, files, aggregator=recording_aggregator
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
    assert files.reads == [stored.uri]
    (inputs,) = aggregated
    reused = inputs["baseline"]
    assert reused.descriptor.simulation_execution_id == BASELINE_RUN_ID
    assert reused.descriptor.artifact.uri == stored.uri
    assert reused.descriptor.artifact.content_sha256 == stored.sha256
    pd.testing.assert_frame_equal(
        reused.frames["household"], stored.frames["household"]
    )
    assert result == {"report_run_id": str(REPORT_RUN_ID), "status": "succeeded"}


def test_a_report_run_whose_two_runs_succeeded_starts_no_worker() -> None:
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
    invoker = RunWorkerInvoker(report_runs, artifacts)

    result = _coordinate(report_runs, invoker, artifacts, files)

    assert invoker.events == []
    assert report_runs.report_runs[REPORT_RUN_ID].status is RunStatus.SUCCEEDED
    assert report_runs.report_runs[REPORT_RUN_ID].writes == [
        "claim_report_run",
        "complete_report_run",
    ]
    for run_id in (BASELINE_RUN_ID, REFORM_RUN_ID):
        assert report_runs.simulation_runs[run_id].writes == []
    assert sorted(files.reads) == sorted([baseline.uri, reform.uri])
    assert result == {"report_run_id": str(REPORT_RUN_ID), "status": "succeeded"}


@pytest.mark.parametrize("status", [RunStatus.PENDING, RunStatus.RUNNING])
def test_a_pending_or_running_simulation_run_is_calculated_as_before(status) -> None:
    report_runs = FakeReportRuns()
    report_runs.simulation_runs[BASELINE_RUN_ID].status = status
    files = FakeOutputFiles()
    artifacts = FakeArtifacts()
    invoker = RunWorkerInvoker(report_runs, artifacts)

    _coordinate(report_runs, invoker, artifacts, files)

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
    report_runs = FakeReportRuns()
    files = FakeOutputFiles()
    make_unusable(report_runs, files)
    baseline_before = deepcopy(report_runs.simulation_runs[BASELINE_RUN_ID])
    artifacts = FakeArtifacts()
    invoker = RunWorkerInvoker(report_runs, artifacts)

    with pytest.raises(RuntimeError, match="coordination failed"):
        _coordinate(report_runs, invoker, artifacts, files)

    report_run = report_runs.report_runs[REPORT_RUN_ID]
    assert report_run.status is RunStatus.FAILED
    assert report_run.error_message.startswith(f"{error_code}: ")
    assert f"baseline simulation run {BASELINE_RUN_ID}" in report_run.error_message
    # Nothing is calculated, and the reused run is not written; the reform
    # was never started, and is left pending.
    assert invoker.events == []
    assert report_runs.simulation_runs[BASELINE_RUN_ID] == baseline_before
    _assert_left_unstarted(report_runs.simulation_runs[REFORM_RUN_ID])


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
    report_runs = FakeReportRuns()
    files = FakeOutputFiles()
    _succeeded_with_output(report_runs, files, BASELINE_RUN_ID)
    setattr(report_runs.simulation_runs[BASELINE_RUN_ID], column, recorded)
    artifacts = FakeArtifacts()
    invoker = RunWorkerInvoker(report_runs, artifacts)

    with pytest.raises(RuntimeError, match="coordination failed"):
        _coordinate(report_runs, invoker, artifacts, files)

    message = report_runs.report_runs[REPORT_RUN_ID].error_message
    assert message.startswith("reused_simulation_run_mismatch: ")
    assert f"baseline simulation run {BASELINE_RUN_ID}" in message
    assert f"reform simulation run {REFORM_RUN_ID}" in message
    assert f"{column} {recorded or 'none'} instead of " in message
    assert invoker.events == []
    assert report_runs.simulation_runs[BASELINE_RUN_ID].writes == []
    _assert_left_unstarted(report_runs.simulation_runs[REFORM_RUN_ID])


def _report_with_options(options):
    """Return the shared report with ``options`` on both of its simulations."""
    report = report_run_input()
    return report.model_copy(
        update={
            "baseline": report.baseline.model_copy(update={"options": options}),
            "reform": report.reform.model_copy(update={"options": options}),
        }
    )


REUSED_SIDES = {
    "both-reused": (BASELINE_RUN_ID, REFORM_RUN_ID),
    "baseline-reused": (BASELINE_RUN_ID,),
    "reform-reused": (REFORM_RUN_ID,),
}
REPORT_OPTIONS = {"spm": {"scenario": "ce_trend"}, "include_cliffs": True}


def spm_receipt_writing_invoker(report_runs, artifacts):
    """Start real workers that write the certified defaults' SPM receipt."""
    return RunWorkerInvoker(
        report_runs, artifacts, calculation_provenance=WORKER_SPM_RECEIPT
    )


def spm_checking_aggregator(*, report, baseline, reform, **_inputs):
    """Combine both sides' SPM receipts as the real aggregation does, then aggregate.

    ``build_spm_result`` raises unless both sides carry a receipt covering
    the report year and the two receipts are the same.
    """
    build_spm_result(
        report=report,
        baseline_descriptor=baseline.descriptor,
        reform_descriptor=reform.descriptor,
    )
    return economy_aggregate()


@pytest.mark.parametrize(
    "reused_run_ids", list(REUSED_SIDES.values()), ids=list(REUSED_SIDES)
)
def test_each_reused_simulations_options_country_and_year_reach_the_spm_resolver(
    resolved_spm, reused_run_ids
) -> None:
    report_runs = FakeReportRuns()
    files = FakeOutputFiles()
    for run_id in reused_run_ids:
        _succeeded_with_output(report_runs, files, run_id)
    artifacts = FakeArtifacts()
    # A side that is calculated gets the receipt a real worker would write,
    # and the aggregation combines both sides' receipts as it really does.
    invoker = spm_receipt_writing_invoker(report_runs, artifacts)

    _coordinate(
        report_runs,
        invoker,
        artifacts,
        files,
        report=_report_with_options(REPORT_OPTIONS),
        aggregator=spm_checking_aggregator,
    )

    # Each reused side's request is what its worker would resolve: the
    # report's options, its SPM option among them, and its country, region
    # and year.
    assert resolved_spm.requests == [
        {
            "country": "us",
            "scope": "macro",
            "time_period": "2026",
            "region": "us",
            **REPORT_OPTIONS,
        }
    ] * len(reused_run_ids)
    assert report_runs.report_runs[REPORT_RUN_ID].status is RunStatus.SUCCEEDED
    assert len(invoker.events) == 2 - len(reused_run_ids)


@pytest.mark.parametrize(
    "column",
    ["dataset_id", "region_id"],
    ids=["different-datasets", "different-regions"],
)
def test_two_reused_runs_naming_different_datasets_or_regions_fail_the_report(
    column,
) -> None:
    report_runs = FakeReportRuns()
    files = FakeOutputFiles()
    for run_id in (BASELINE_RUN_ID, REFORM_RUN_ID):
        _succeeded_with_output(report_runs, files, run_id)
    baseline_value = getattr(report_runs.simulation_runs[BASELINE_RUN_ID], column)
    setattr(report_runs.simulation_runs[REFORM_RUN_ID], column, OTHER_CATALOG_ID)
    artifacts = FakeArtifacts()
    invoker = RunWorkerInvoker(report_runs, artifacts)

    with pytest.raises(RuntimeError, match="coordination failed"):
        _coordinate(report_runs, invoker, artifacts, files)

    message = report_runs.report_runs[REPORT_RUN_ID].error_message
    assert message.startswith("reused_simulation_run_mismatch: ")
    assert f"baseline simulation run {BASELINE_RUN_ID}" in message
    assert f"reform simulation run {REFORM_RUN_ID}" in message
    assert f"{column} {baseline_value} instead of {OTHER_CATALOG_ID}" in message
    # The pair is refused before either file is read or any worker starts.
    assert files.reads == []
    assert invoker.events == []
    for run_id in (BASELINE_RUN_ID, REFORM_RUN_ID):
        assert report_runs.simulation_runs[run_id].writes == []


def test_a_reused_output_without_the_resolved_spm_calculation_fails_the_report() -> (
    None
):
    # A US request with no SPM option takes the certified defaults, so a
    # worker would calculate an SPM measure and write its receipt.
    report_runs = FakeReportRuns()
    files = FakeOutputFiles()
    _succeeded_with_output(
        report_runs, files, BASELINE_RUN_ID, calculation_provenance=None
    )
    artifacts = FakeArtifacts()
    invoker = RunWorkerInvoker(report_runs, artifacts)

    with pytest.raises(RuntimeError, match="coordination failed"):
        _coordinate(report_runs, invoker, artifacts, files)

    report_run = report_runs.report_runs[REPORT_RUN_ID]
    assert report_run.error_message.startswith("reused_simulation_spm_mismatch: ")
    assert "without an SPM calculation receipt" in report_run.error_message
    assert invoker.events == []
    assert report_runs.simulation_runs[BASELINE_RUN_ID].writes == []


def test_an_output_with_an_spm_receipt_fails_where_no_spm_measure_is_calculated(
    resolved_spm,
) -> None:
    # A bundle with no certified SPM measurement calculates none.
    resolved_spm.certified_defaults = None
    report_runs = FakeReportRuns()
    files = FakeOutputFiles()
    _succeeded_with_output(report_runs, files, BASELINE_RUN_ID)
    artifacts = FakeArtifacts()
    invoker = RunWorkerInvoker(report_runs, artifacts)

    with pytest.raises(RuntimeError, match="coordination failed"):
        _coordinate(report_runs, invoker, artifacts, files)

    report_run = report_runs.report_runs[REPORT_RUN_ID]
    assert report_run.error_message.startswith("reused_simulation_spm_mismatch: ")
    assert "calculates no SPM measure" in report_run.error_message
    assert invoker.events == []


@dataclass(frozen=True)
class SpmSelectionMismatch:
    """A report's SPM request, and the selection its reused files record."""

    spm_request: dict | None
    stored_selection: dict


# Each gives another selection than the reused files' receipts record. .py's
# resolver accepts each request: only the 2020 county vintage is supported,
# so the vintage case keeps the default request and stores a receipt
# calculated under 2010 instead.
OTHER_SPM_SELECTIONS = {
    "scenario": SpmSelectionMismatch(
        spm_request={"scenario": "zero_real"},
        stored_selection=CERTIFIED_SPM_DEFAULTS,
    ),
    "geography_kind": SpmSelectionMismatch(
        spm_request={"geography_kind": "national"},
        stored_selection=CERTIFIED_SPM_DEFAULTS,
    ),
    "county_vintage": SpmSelectionMismatch(
        spm_request=None,
        stored_selection={**CERTIFIED_SPM_DEFAULTS, "county_vintage": "2010"},
    ),
}


@pytest.mark.parametrize(
    "reused_run_ids", list(REUSED_SIDES.values()), ids=list(REUSED_SIDES)
)
@pytest.mark.parametrize(
    ("differing_field", "mismatch"),
    list(OTHER_SPM_SELECTIONS.items()),
    ids=list(OTHER_SPM_SELECTIONS),
)
def test_outputs_reused_from_another_spm_selection_fail_before_any_worker_starts(
    reused_run_ids, differing_field, mismatch
) -> None:
    report_runs = FakeReportRuns()
    files = FakeOutputFiles()
    for run_id in reused_run_ids:
        _succeeded_with_output(
            report_runs,
            files,
            run_id,
            calculation_provenance=spm_receipt(mismatch.stored_selection),
        )
    artifacts = FakeArtifacts()
    invoker = RunWorkerInvoker(report_runs, artifacts)
    options = {} if mismatch.spm_request is None else {"spm": mismatch.spm_request}

    with pytest.raises(RuntimeError, match="coordination failed"):
        _coordinate(
            report_runs,
            invoker,
            artifacts,
            files,
            report=_report_with_options(options),
        )

    report_run = report_runs.report_runs[REPORT_RUN_ID]
    assert report_run.status is RunStatus.FAILED
    assert report_run.error_message.startswith("reused_simulation_spm_mismatch: ")
    assert f"SPM receipt {differing_field} differs" in report_run.error_message
    # Neither simulation is calculated, and no reused run is written; a run
    # that was not reused was never started, and is left pending.
    assert invoker.events == []
    for run_id in (BASELINE_RUN_ID, REFORM_RUN_ID):
        simulation_run = report_runs.simulation_runs[run_id]
        if run_id in reused_run_ids:
            assert simulation_run.writes == []
        else:
            _assert_left_unstarted(simulation_run)


def test_an_output_whose_spm_receipt_lacks_the_report_year_fails_the_report() -> None:
    report_runs = FakeReportRuns()
    files = FakeOutputFiles()
    _succeeded_with_output(
        report_runs,
        files,
        BASELINE_RUN_ID,
        calculation_provenance=spm_receipt(years=("2027",)),
    )
    artifacts = FakeArtifacts()
    invoker = RunWorkerInvoker(report_runs, artifacts)

    with pytest.raises(RuntimeError, match="coordination failed"):
        _coordinate(report_runs, invoker, artifacts, files)

    report_run = report_runs.report_runs[REPORT_RUN_ID]
    assert report_run.error_message.startswith("reused_simulation_spm_mismatch: ")
    assert "covers 2027, not 2026" in report_run.error_message
    assert invoker.events == []


@pytest.mark.parametrize(
    "options",
    [{}, {"spm": {"scenario": "ce_trend", "geography_kind": "county"}}],
    ids=["no-spm-option", "spm-option-naming-the-defaults"],
)
def test_an_output_with_the_resolved_spm_selection_is_reused(options) -> None:
    report_runs = FakeReportRuns()
    files = FakeOutputFiles()
    _succeeded_with_output(report_runs, files, BASELINE_RUN_ID)
    artifacts = FakeArtifacts()
    invoker = RunWorkerInvoker(report_runs, artifacts)
    aggregated = []

    def recording_aggregator(**inputs):
        aggregated.append(inputs)
        return economy_aggregate()

    _coordinate(
        report_runs,
        invoker,
        artifacts,
        files,
        report=_report_with_options(options),
        aggregator=recording_aggregator,
    )

    assert invoker.events == ["spawn:reform"]
    assert report_runs.report_runs[REPORT_RUN_ID].status is RunStatus.SUCCEEDED
    (inputs,) = aggregated
    assert inputs["baseline"].descriptor.calculation_provenance == WORKER_SPM_RECEIPT


SECOND_REPORT_RUN_ID = UUID("00000000-0000-0000-0000-0000000000a2")
SECOND_REFORM_RUN_ID = UUID("00000000-0000-0000-0000-0000000000c2")


def test_two_report_runs_reusing_one_succeeded_run_at_once_leave_it_unchanged() -> None:
    """Two report runs share one succeeded baseline run and its output file.

    Both coordinators work on one set of run tables and read one output file.
    The check is that reusing, succeeding and failing in one report run
    writes nothing to the shared run and doesn't change the other report run.
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
            ),
            "artifact_prefix": (
                f"stage-12-runs/staging/2026/09/{SECOND_REPORT_RUN_ID}"
            ),
        }
    )
    first_report = report_run_input()
    second_report = first_report.model_copy(
        update={
            "evaluation_id": SECOND_REPORT_RUN_ID,
            "baseline": first_report.baseline.model_copy(
                update={"evaluation_id": SECOND_REPORT_RUN_ID}
            ),
            "reform": first_report.reform.model_copy(
                update={
                    "evaluation_id": SECOND_REPORT_RUN_ID,
                    "simulation_execution_id": SECOND_REFORM_RUN_ID,
                }
            ),
        }
    )
    first_artifacts, second_artifacts = FakeArtifacts(), FakeArtifacts()
    first_invoker = RunWorkerInvoker(report_runs, first_artifacts)
    # The second report's reform fails; that failure is its report run's alone.
    second_invoker = RunWorkerInvoker(
        report_runs, second_artifacts, failing_role="reform"
    )

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(
            _coordinate,
            report_runs,
            first_invoker,
            first_artifacts,
            files,
            coordinator_call_id="fc-first-coordinator",
        )
        second = executor.submit(
            _coordinate,
            report_runs,
            second_invoker,
            second_artifacts,
            files,
            report=second_report,
            context=second_context,
            coordinator_call_id="fc-second-coordinator",
        )
        first_result = first.result()
        with pytest.raises(RuntimeError, match="coordination failed"):
            second.result()

    assert first_result == {
        "report_run_id": str(REPORT_RUN_ID),
        "status": "succeeded",
    }
    assert report_runs.report_runs[REPORT_RUN_ID].status is RunStatus.SUCCEEDED
    assert report_runs.report_runs[SECOND_REPORT_RUN_ID].status is RunStatus.FAILED
    assert report_runs.simulation_runs[SECOND_REFORM_RUN_ID].status is RunStatus.FAILED
    # Neither report run wrote to the run they share.
    assert report_runs.simulation_runs[BASELINE_RUN_ID] == shared_before
    assert files.reads == [shared.uri, shared.uri]
    assert first_invoker.events == ["spawn:reform"]
    assert second_invoker.events == ["spawn:reform"]


def test_a_succeeded_reform_run_is_reused_while_its_baseline_is_calculated() -> None:
    report_runs = FakeReportRuns()
    files = FakeOutputFiles()
    stored = _succeeded_with_output(
        report_runs, files, REFORM_RUN_ID, frames=simulation_frames(120.0)
    )
    artifacts = FakeArtifacts()
    invoker = RunWorkerInvoker(report_runs, artifacts)

    result = _coordinate(report_runs, invoker, artifacts, files)

    assert invoker.events == ["spawn:baseline"]
    assert report_runs.simulation_runs[REFORM_RUN_ID].writes == []
    assert report_runs.simulation_runs[BASELINE_RUN_ID].status is RunStatus.SUCCEEDED
    assert report_runs.report_runs[REPORT_RUN_ID].status is RunStatus.SUCCEEDED
    assert result == {"report_run_id": str(REPORT_RUN_ID), "status": "succeeded"}
    assert files.reads == [stored.uri]


def _preempt(_seconds):
    raise KeyboardInterrupt


def test_a_restarted_call_reuses_the_output_its_own_baseline_worker_recorded() -> None:
    report_runs = FakeReportRuns()
    files = FakeOutputFiles()
    artifacts = FakeArtifacts()
    # The first attempt starts both children, then Modal pre-empts it.
    with pytest.raises(KeyboardInterrupt):
        _coordinate(report_runs, TimeoutInvoker(), artifacts, files, sleep=_preempt)
    # Meanwhile the baseline's worker, which the pre-empted call left running,
    # wrote its output and completed its simulation run; the reform's never
    # claimed its run.
    stored = _succeeded_with_output(report_runs, files, BASELINE_RUN_ID)
    baseline_run = report_runs.simulation_runs[BASELINE_RUN_ID]
    baseline_run.modal_call_id = "fc-baseline-worker"
    baseline_before = deepcopy(baseline_run)
    restart_invoker = RunWorkerInvoker(report_runs, artifacts)

    result = _coordinate(report_runs, restart_invoker, artifacts, files)

    # Only the reform starts again; no earlier call holds its run.
    assert restart_invoker.events == ["spawn:reform"]
    assert restart_invoker.restored == {}
    assert files.reads == [stored.uri]
    assert result == {"report_run_id": str(REPORT_RUN_ID), "status": "succeeded"}
    assert report_runs.simulation_runs[BASELINE_RUN_ID] == baseline_before
    report_run = report_runs.report_runs[REPORT_RUN_ID]
    assert report_run.status is RunStatus.SUCCEEDED
    assert report_run.writes == [
        "claim_report_run",
        "claim_report_run",
        "complete_report_run",
    ]


def test_a_reused_output_file_is_read_and_decoded_once(monkeypatch) -> None:
    decoded_digests: list[str] = []
    decode = stage12_artifacts.deserialize_simulation_frames

    def counting_decoder(payload):
        decoded_digests.append(sha256(payload).hexdigest())
        return decode(payload)

    # decode_simulation_output, which decodes every reused and worker output
    # file the coordinator aggregates, looks the decoder up in this module.
    monkeypatch.setattr(
        stage12_artifacts, "deserialize_simulation_frames", counting_decoder
    )
    report_runs = FakeReportRuns()
    files = FakeOutputFiles()
    stored = _succeeded_with_output(report_runs, files, BASELINE_RUN_ID)
    artifacts = FakeArtifacts()

    _coordinate(report_runs, RunWorkerInvoker(report_runs, artifacts), artifacts, files)

    assert files.reads == [stored.uri]
    assert decoded_digests.count(stored.sha256) == 1


def test_a_decoded_files_row_identity_is_the_one_it_was_written_with() -> None:
    frames = {
        "household": pd.DataFrame(
            {"household_id": [30, 10, 20], "household_net_income": [3.0, 1.0, 2.0]}
        ),
        "person": pd.DataFrame(
            {"person_id": [2, 1], "household_id": [10, 30], "age": [40, 41]}
        ),
    }
    payload, written_identity = serialize_simulation_frames(frames)

    assert (
        simulation_row_identity(deserialize_simulation_frames(payload))
        == written_identity
    )


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
