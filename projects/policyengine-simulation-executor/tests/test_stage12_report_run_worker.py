"""A worker claims the simulation run its call names, under its own Modal call ID.

The worker records that run's status and output file on the run tables
alone: every test here hands it a comparison store that fails on any use. A
report run's coordinator starts it with a planned simulation; the simulation
service may start it on a simulation run of its own, which the worker plans
by itself.
"""

from __future__ import annotations

from hashlib import sha256

import pytest
from fixtures.fake_object_store import InMemoryObjectStore
from policyengine_simulation_contract.stage12_execution import (
    PlannedSimulationExecutionInput,
    SimulationRole,
    stage12_output_plan_sha256,
)
from policyengine_simulation_executor import spm as runtime_spm
from policyengine_simulation_executor import stage12_artifacts
from policyengine_simulation_executor.stage12_artifacts import (
    SimulationOutputBucket,
    Stage12ArtifactStore,
    serialize_simulation_frames,
)
from policyengine_simulation_executor.stage12_runtime import (
    SimulationWorkerDependencies,
    run_single_simulation,
)
from policyengine_simulation_executor.stage12_runtime import (
    simulation as simulation_module,
)
from policyengine_stage12_persistence import RunStatus
from stage12_fake_report_runs import (
    BASELINE_RUN_ID,
    REFORM_RUN_ID,
    ComparisonStoreNotUsed,
    FakeReportRuns,
    report_run_context,
    report_run_planned_simulation,
    report_run_simulation,
    simulation_run_context,
)
from stage12_fake_stores import FakeArtifacts, FakeStore, simulation_frames
from stage12_runtime_support import (
    child_record,
    invocation_context,
    planned_simulation,
    shared_output_plan,
)

WORKER_CALL_ID = "fc-baseline-worker"
ARTIFACT_BUCKET = "policyengine-stage12-staging-artifacts"
OUTPUT_BUCKET = "policyengine-v2-staging-simulation-outputs"
STANDALONE_PREFIX = f"stage-12-runs/staging/2026/09/{BASELINE_RUN_ID}"


class OutputFiles:
    """Stored output files by URI, as Cloud Storage holds them."""

    def __init__(self) -> None:
        self.payloads: dict[str, bytes] = {}

    def read(self, uri: str) -> bytes:
        if uri not in self.payloads:
            raise FileNotFoundError(uri)
        return self.payloads[uri]


def _run_worker(report_runs, *, simulation=None, context=None, **dependencies):
    simulation = simulation or report_run_planned_simulation(SimulationRole.BASELINE)
    dependencies.setdefault("store", ComparisonStoreNotUsed())
    dependencies.setdefault("artifacts", FakeArtifacts())
    dependencies.setdefault("calculator", lambda _simulation: simulation_frames())
    return run_single_simulation(
        simulation.model_dump(mode="json"),
        (context or simulation_run_context()).model_dump(mode="json"),
        required_country=simulation.geography.country,
        modal_call_id=dependencies.pop("modal_call_id", WORKER_CALL_ID),
        dependencies=SimulationWorkerDependencies(
            report_runs=report_runs, **dependencies
        ),
    )


def test_a_worker_claims_its_simulation_run_and_completes_it_with_its_output() -> None:
    report_runs = FakeReportRuns()

    result = _run_worker(report_runs)

    run = report_runs.simulation_runs[BASELINE_RUN_ID]
    assert run.status is RunStatus.SUCCEEDED
    assert run.modal_call_id == WORKER_CALL_ID
    assert run.writes == ["claim_simulation_run", "complete_simulation_run"]
    assert run.output_uri == result["artifact"]["uri"]
    assert run.output_sha256 == result["artifact"]["content_sha256"]
    assert result["simulation_execution_id"] == str(BASELINE_RUN_ID)
    # The other simulation run is not this worker's.
    assert report_runs.simulation_runs[REFORM_RUN_ID].writes == []


def test_a_failed_calculation_fails_the_workers_simulation_run() -> None:
    report_runs = FakeReportRuns()

    def failing_calculation(_simulation):
        msg = "the calculation ran out of memory"
        raise RuntimeError(msg)

    with pytest.raises(RuntimeError, match="execution failed"):
        _run_worker(report_runs, calculator=failing_calculation)

    run = report_runs.simulation_runs[BASELINE_RUN_ID]
    assert run.status is RunStatus.FAILED
    assert run.writes == ["claim_simulation_run", "fail_simulation_run"]
    assert run.error_message.startswith("simulation_execution_failed: ")


def test_an_invalid_policy_fails_the_simulation_run_with_its_reason() -> None:
    report_runs = FakeReportRuns()
    simulation = report_run_planned_simulation(SimulationRole.REFORM).model_copy(
        update={"policy": {"reform": {"2026": 1}}}
    )

    with pytest.raises(RuntimeError, match="execution failed"):
        _run_worker(
            report_runs,
            simulation=simulation,
            context=simulation_run_context(REFORM_RUN_ID),
        )

    run = report_runs.simulation_runs[REFORM_RUN_ID]
    assert run.status is RunStatus.FAILED
    assert run.error_message.startswith("invalid_policy_period: ")


@pytest.mark.parametrize(
    ("status", "modal_call_id"),
    [
        # The simulation service recorded another call it started.
        (RunStatus.PENDING, "fc-other-worker"),
        (RunStatus.RUNNING, "fc-other-worker"),
        (RunStatus.FAILED, None),
    ],
)
def test_a_run_another_call_holds_or_that_failed_is_not_calculated(
    status, modal_call_id
) -> None:
    report_runs = FakeReportRuns()
    run = report_runs.simulation_runs[BASELINE_RUN_ID]
    run.status, run.modal_call_id = status, modal_call_id
    calculated = []

    result = _run_worker(
        report_runs,
        calculator=lambda simulation: (
            calculated.append(simulation) or simulation_frames()
        ),
    )

    assert result == {
        "deduplicated": True,
        "simulation_run_id": str(BASELINE_RUN_ID),
        "status": status.value,
    }
    assert calculated == []
    assert run.writes == ["claim_simulation_run"]


def test_a_restarted_worker_resumes_its_own_claim() -> None:
    # Modal runs a pre-empted worker again under the same call ID.
    report_runs = FakeReportRuns()
    run = report_runs.simulation_runs[BASELINE_RUN_ID]
    run.status, run.modal_call_id = RunStatus.RUNNING, WORKER_CALL_ID

    _run_worker(report_runs)

    assert run.status is RunStatus.SUCCEEDED
    assert run.writes == ["claim_simulation_run", "complete_simulation_run"]


def test_a_run_that_already_succeeded_returns_its_stored_output_uncalculated(
    monkeypatch,
) -> None:
    # A pre-empted worker completed its run before Modal ran it again.
    monkeypatch.setattr(runtime_spm, "normalize_runtime_spm", lambda _params: None)
    report_runs = FakeReportRuns()
    files = OutputFiles()
    payload, _row_identity = serialize_simulation_frames(simulation_frames())
    stored_uri = f"gs://{OUTPUT_BUCKET}/simulation-runs/staging/us/run.parquet"
    files.payloads[stored_uri] = payload
    run = report_runs.simulation_runs[BASELINE_RUN_ID]
    run.status, run.modal_call_id = RunStatus.SUCCEEDED, WORKER_CALL_ID
    run.output_uri, run.output_sha256 = stored_uri, sha256(payload).hexdigest()
    calculated = []

    result = _run_worker(
        report_runs,
        output_files=files,
        calculator=lambda simulation: (
            calculated.append(simulation) or simulation_frames()
        ),
    )

    assert calculated == []
    assert result["artifact"]["uri"] == stored_uri
    assert result["artifact"]["content_sha256"] == run.output_sha256
    assert result["output_plan_sha256"] == stage12_output_plan_sha256(
        shared_output_plan()
    )
    assert run.writes == ["claim_simulation_run"]


@pytest.mark.parametrize("taken_by", ["coordinator-failure", "next-worker"])
def test_a_worker_that_lost_its_run_records_nothing(taken_by) -> None:
    report_runs = FakeReportRuns()
    run = report_runs.simulation_runs[BASELINE_RUN_ID]

    def calculation_meanwhile(_simulation):
        if taken_by == "coordinator-failure":
            report_runs.fail_simulation_run(
                BASELINE_RUN_ID,
                error_message="simulation_cancelled: Cancelled after a failure.",
                failed_at=None,
            )
        else:
            report_runs.release_simulation_run(
                BASELINE_RUN_ID, modal_call_id=WORKER_CALL_ID, released_at=None
            )
            report_runs.claim_simulation_run(
                BASELINE_RUN_ID, modal_call_id="fc-next-worker", claimed_at=None
            )
        return simulation_frames()

    with pytest.raises(RuntimeError, match="cancelled"):
        _run_worker(report_runs, calculator=calculation_meanwhile)

    assert run.output_uri is None
    # The worker's completion was refused, and it failed nothing afterwards.
    assert run.writes[-1] == "complete_simulation_run"
    if taken_by == "next-worker":
        assert (run.status, run.modal_call_id) == (RunStatus.RUNNING, "fc-next-worker")
    else:
        assert run.error_message.startswith("simulation_cancelled: ")


def test_a_segmented_worker_learns_its_run_was_taken_from_it(monkeypatch) -> None:
    monkeypatch.setattr(simulation_module, "should_segment_simulation", lambda _: True)
    report_runs = FakeReportRuns()
    checks = []

    def segmented(_simulation, cancellation_requested):
        checks.append(cancellation_requested())
        report_runs.fail_simulation_run(
            BASELINE_RUN_ID, error_message="simulation_cancelled: x", failed_at=None
        )
        checks.append(cancellation_requested())
        return simulation_module.SimulationCalculation(frames=simulation_frames())

    with pytest.raises(RuntimeError, match="cancelled"):
        _run_worker(report_runs, segmented_calculator=segmented)

    assert checks == [False, True]


def test_a_detailed_save_worker_needs_its_modal_call_id() -> None:
    report_runs = FakeReportRuns()

    with pytest.raises(ValueError, match="Modal call ID"):
        _run_worker(report_runs, modal_call_id=None)

    assert report_runs.events == []


def test_a_worker_input_naming_another_simulation_run_fails_the_claimed_run() -> None:
    report_runs = FakeReportRuns()

    with pytest.raises(RuntimeError, match="execution failed"):
        _run_worker(
            report_runs,
            simulation=report_run_planned_simulation(SimulationRole.REFORM),
            context=simulation_run_context(BASELINE_RUN_ID),
        )

    run = report_runs.simulation_runs[BASELINE_RUN_ID]
    assert run.status is RunStatus.FAILED
    assert run.error_message.startswith("simulation_run_input_mismatch: ")
    assert report_runs.simulation_runs[REFORM_RUN_ID].writes == []


def test_a_worker_context_naming_a_report_run_is_refused_unwritten() -> None:
    report_runs = FakeReportRuns()

    with pytest.raises(ValueError, match="simulation run"):
        _run_worker(report_runs, context=report_run_context())

    assert report_runs.events == []


def test_a_standalone_simulation_run_is_planned_and_calculated_on_its_own() -> None:
    report_runs = FakeReportRuns()
    planned = []
    standalone = report_run_simulation(SimulationRole.BASELINE).model_copy(
        update={"role": SimulationRole.STANDALONE, "evaluation_id": BASELINE_RUN_ID}
    )
    context = simulation_run_context(BASELINE_RUN_ID).model_copy(
        update={"artifact_prefix": STANDALONE_PREFIX}
    )

    def planner(simulation):
        planned.append(simulation)
        return shared_output_plan()

    result = _run_worker(
        report_runs,
        simulation=standalone,
        context=context,
        output_plan_resolver=planner,
    )

    assert planned == [standalone]
    assert result["role"] == "standalone"
    assert result["output_plan_sha256"] == stage12_output_plan_sha256(
        shared_output_plan()
    )
    run = report_runs.simulation_runs[BASELINE_RUN_ID]
    assert (run.status, run.modal_call_id) == (RunStatus.SUCCEEDED, WORKER_CALL_ID)
    assert run.output_uri == result["artifact"]["uri"]


def test_a_comparison_worker_records_no_simulation_run() -> None:
    simulation = planned_simulation(SimulationRole.BASELINE)
    store = FakeStore()
    store.children[simulation.simulation_execution_id] = child_record(simulation)
    report_runs = FakeReportRuns()

    _run_worker(
        report_runs, simulation=simulation, context=invocation_context(), store=store
    )

    assert report_runs.events == []


def test_report_run_worker_names_its_artifacts_by_digest(monkeypatch) -> None:
    built = []
    artifacts = FakeArtifacts()

    def recording_artifact_store(*, report_run=False):
        built.append(report_run)
        return artifacts

    monkeypatch.setattr(simulation_module, "artifact_store", recording_artifact_store)
    report_runs = FakeReportRuns()
    simulation = report_run_planned_simulation(SimulationRole.BASELINE)

    run_single_simulation(
        simulation.model_dump(mode="json"),
        simulation_run_context().model_dump(mode="json"),
        required_country="us",
        modal_call_id=WORKER_CALL_ID,
        dependencies=SimulationWorkerDependencies(
            store=ComparisonStoreNotUsed(),
            calculator=lambda _: simulation_frames(),
            report_runs=report_runs,
        ),
    )

    assert built == [True]
    # The output file went to the simulation output bucket, for its run.
    assert artifacts.simulation_run_outputs == [("staging", BASELINE_RUN_ID)]


def _two_bucket_artifacts():
    """Return a store over in-memory artifact and simulation output buckets."""
    artifact_objects = InMemoryObjectStore()
    output_objects = InMemoryObjectStore()
    artifacts = Stage12ArtifactStore(
        ARTIFACT_BUCKET,
        store=artifact_objects,
        content_addressed=True,
        simulation_outputs=SimulationOutputBucket(OUTPUT_BUCKET, store=output_objects),
    )
    return artifacts, artifact_objects, output_objects


def test_a_detailed_save_worker_keeps_its_output_in_the_simulation_output_bucket() -> (
    None
):
    report_runs = FakeReportRuns()
    artifacts, artifact_objects, output_objects = _two_bucket_artifacts()

    result = _run_worker(report_runs, artifacts=artifacts)

    digest = result["artifact"]["content_sha256"]
    path = f"simulation-runs/staging/us/{BASELINE_RUN_ID}/output-{digest}.parquet"
    output_uri = f"gs://{OUTPUT_BUCKET}/{path}"
    assert result["artifact"]["uri"] == output_uri
    run = report_runs.simulation_runs[BASELINE_RUN_ID]
    assert (run.output_uri, run.output_sha256) == (output_uri, digest)
    assert list(output_objects.objects) == [path]
    # The planned input stays in the artifact bucket, under the call's prefix.
    (input_path,) = artifact_objects.objects
    assert input_path.startswith(f"{simulation_run_context().artifact_prefix}/inputs/")


def test_a_detailed_save_worker_without_an_output_bucket_fails_before_its_claim(
    monkeypatch,
) -> None:
    monkeypatch.setenv("STAGE12_ARTIFACT_BUCKET", ARTIFACT_BUCKET)
    monkeypatch.delenv("V2_SIMULATION_OUTPUT_BUCKET", raising=False)
    # Every bucket the worker opens is in memory, so it never reaches GCP.
    monkeypatch.setattr(
        stage12_artifacts, "ArtifactStore", lambda _bucket_name: InMemoryObjectStore()
    )
    report_runs = FakeReportRuns()
    simulation = report_run_planned_simulation(SimulationRole.BASELINE)

    with pytest.raises(ValueError, match="V2_SIMULATION_OUTPUT_BUCKET"):
        run_single_simulation(
            simulation.model_dump(mode="json"),
            simulation_run_context().model_dump(mode="json"),
            required_country="us",
            modal_call_id=WORKER_CALL_ID,
            dependencies=SimulationWorkerDependencies(
                store=ComparisonStoreNotUsed(),
                calculator=lambda _: simulation_frames(),
                report_runs=report_runs,
            ),
        )

    assert report_runs.events == []


def _uk_planned_simulation():
    """Return the baseline report run simulation, for the whole of the UK."""
    payload = report_run_planned_simulation(SimulationRole.BASELINE).model_dump(
        mode="json"
    )
    payload["geography"] = {"country": "uk", "region": "uk"}
    payload["output_plan"]["country"] = "uk"
    payload["bundle"]["country_package_name"] = "policyengine-uk"
    return PlannedSimulationExecutionInput.model_validate(payload)


def test_a_uk_worker_keeps_its_output_in_the_simulation_output_bucket() -> None:
    report_runs = FakeReportRuns()
    artifacts, artifact_objects, output_objects = _two_bucket_artifacts()

    result = _run_worker(
        report_runs, simulation=_uk_planned_simulation(), artifacts=artifacts
    )

    digest = result["artifact"]["content_sha256"]
    path = f"simulation-runs/staging/uk/{BASELINE_RUN_ID}/output-{digest}.parquet"
    assert result["artifact"]["uri"] == f"gs://{OUTPUT_BUCKET}/{path}"
    assert list(output_objects.objects) == [path]
    assert not any(
        stored_path.endswith(".parquet") for stored_path in artifact_objects.objects
    )
