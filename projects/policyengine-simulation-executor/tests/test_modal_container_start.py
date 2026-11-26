"""Tests for the container start attributes of the simulation workers' calls.

Each worker call's operation says how the container serving it started
(``container_start``) and which call it is in that container
(``container_call_number``), so Cloud Logging and Cloud Trace show the calls
that ran in containers restored from a memory snapshot.
"""

from __future__ import annotations

import importlib
import sys

import pytest
from conftest import NoOpObservabilityRuntime
from fixtures.fake_modal import install_fake_modal
from policyengine_simulation_executor import simulation_runtime
from src.modal import budget_window_batch, segmented_national

WORKER_RESULT = {"ok": True}
SIMULATION_PARAMS = {"country": "us"}
# The Modal task IDs of the container that takes the snapshot, of a container
# restored from it, and of a container that serves a plain function.
SNAPSHOT_TASK_ID = "ta-01SNAPSHOTTING"
RESTORED_TASK_ID = "ta-01RESTORED"
PLAIN_FUNCTION_TASK_ID = "ta-01PLAINFUNCTION"
CONTAINER_START_KEYS = {"container_start", "container_call_number"}
# The function each worker call hands its job to, by module.
WORKER_JOBS = [
    (segmented_national, "dispatch_run_simulation"),
    (simulation_runtime, "run_simulation_impl"),
    (budget_window_batch, "run_budget_window_batch_impl"),
]

# Each worker class with the method that serves its calls.
WORKER_METHODS = [
    ("SimulationWorker", "run_simulation"),
    ("SimulationSegmentWorker", "run_simulation_segment"),
]


class _AttributeRecordingRuntime(NoOpObservabilityRuntime):
    """Keeps every attribute a call sets on its operation."""

    def __init__(self) -> None:
        self.attributes: dict[str, object] = {}

    def set_context(self, **attributes: object) -> None:
        self.attributes.update(attributes)

    def shutdown(self) -> None:
        return None


class _AttributeRefusingRuntime(NoOpObservabilityRuntime):
    """Fails every attempt to set an attribute, as a broken exporter might."""

    def set_context(self, **_attributes: object) -> None:
        msg = "attribute export failed"
        raise RuntimeError(msg)

    def shutdown(self) -> None:
        return None


@pytest.fixture
def modal_app(monkeypatch):
    """Import the app afresh, as a new container does, with stubbed worker jobs."""
    install_fake_modal(monkeypatch)
    monkeypatch.delenv("MODAL_TASK_ID", raising=False)
    monkeypatch.delenv("MODAL_ENABLE_SNAP_RESTORE", raising=False)
    sys.modules.pop("src.modal.app", None)
    app = importlib.import_module("src.modal.app")
    # The real preload imports both countries' models, and the real warm-up
    # calculates a region group (tests/test_snapshot_warm_up.py).
    monkeypatch.setattr(app, "preload_country_models", lambda: None)
    monkeypatch.setattr(app, "warm_up_first_calculation", lambda: None)
    _replace_worker_jobs(monkeypatch, lambda _params, **_job_options: WORKER_RESULT)
    yield app
    sys.modules.pop("src.modal.app", None)


def _replace_worker_jobs(monkeypatch, job) -> None:
    """Make every worker call hand its parameters to ``job`` instead."""
    for job_module, job_name in WORKER_JOBS:
        monkeypatch.setattr(job_module, job_name, job)


def _record_call_attributes(app, monkeypatch) -> list[dict[str, object]]:
    """Give each call its own recording runtime, as each call configures its own.

    Returns the attributes each call set, in call order.
    """
    attributes_by_call: list[dict[str, object]] = []

    def configure_recording_runtime(*, service_role: str) -> NoOpObservabilityRuntime:
        runtime = _AttributeRecordingRuntime()
        attributes_by_call.append(runtime.attributes)
        return runtime

    monkeypatch.setattr(
        app, "_configure_modal_observability", configure_recording_runtime
    )
    return attributes_by_call


def _worker_after_snapshot_hook(
    app, monkeypatch, worker_class_name: str, *, snapshots_enabled: bool = True
):
    """Create a worker and run its snapshot hook, as Modal does in a new container.

    Modal's worker sets ``MODAL_ENABLE_SNAP_RESTORE`` to ``1`` in containers
    it takes snapshots from or restores, and leaves it unset in a container of
    the same class that starts without snapshots. Modal runs the snapshot hook
    in every container that doesn't restore, but takes a snapshot only when
    the variable is ``1``.
    """
    monkeypatch.setenv("MODAL_TASK_ID", SNAPSHOT_TASK_ID)
    if snapshots_enabled:
        monkeypatch.setenv("MODAL_ENABLE_SNAP_RESTORE", "1")
    worker = getattr(app, worker_class_name)()
    worker.load_country_models()
    return worker


def _restore_into_another_container(monkeypatch, worker) -> None:
    """Act as a restore: the memory stays, and MODAL_TASK_ID names the new container.

    Modal 1.6.4's ``memory_restore`` sets ``MODAL_TASK_ID`` to the task ID of
    the restored container, then Modal runs the ``snap=False`` hooks.
    """
    monkeypatch.setenv("MODAL_TASK_ID", RESTORED_TASK_ID)
    worker.reseed_random_after_restore()


@pytest.mark.parametrize(
    "function_name",
    ["run_simulation", "run_simulation_segment", "run_budget_window_batch"],
)
def test_a_plain_function_call_reports_a_container_started_without_a_snapshot(
    modal_app, monkeypatch, function_name
):
    monkeypatch.setenv("MODAL_TASK_ID", PLAIN_FUNCTION_TASK_ID)
    attributes_by_call = _record_call_attributes(modal_app, monkeypatch)

    assert getattr(modal_app, function_name)(SIMULATION_PARAMS) == WORKER_RESULT

    [call_attributes] = attributes_by_call
    assert call_attributes["container_start"] == "no_snapshot"
    assert call_attributes["container_call_number"] == 1


@pytest.mark.parametrize(("worker_class_name", "method_name"), WORKER_METHODS)
def test_a_call_in_the_container_that_ran_the_snapshot_hook_reports_took_snapshot(
    modal_app, monkeypatch, worker_class_name, method_name
):
    worker = _worker_after_snapshot_hook(modal_app, monkeypatch, worker_class_name)
    attributes_by_call = _record_call_attributes(modal_app, monkeypatch)

    assert getattr(worker, method_name)(SIMULATION_PARAMS) == WORKER_RESULT

    [call_attributes] = attributes_by_call
    assert call_attributes["container_start"] == "took_snapshot"
    assert call_attributes["container_call_number"] == 1


@pytest.mark.parametrize(("worker_class_name", "method_name"), WORKER_METHODS)
def test_a_worker_container_started_without_snapshots_reports_no_snapshot(
    modal_app, monkeypatch, worker_class_name, method_name
):
    worker = _worker_after_snapshot_hook(
        modal_app, monkeypatch, worker_class_name, snapshots_enabled=False
    )
    attributes_by_call = _record_call_attributes(modal_app, monkeypatch)

    assert getattr(worker, method_name)(SIMULATION_PARAMS) == WORKER_RESULT

    # The hook loaded the models, but Modal took no snapshot from this container.
    [call_attributes] = attributes_by_call
    assert call_attributes["container_start"] == "no_snapshot"
    assert call_attributes["container_call_number"] == 1


@pytest.mark.parametrize(("worker_class_name", "method_name"), WORKER_METHODS)
def test_a_call_in_a_restored_container_reports_restored_from_snapshot(
    modal_app, monkeypatch, worker_class_name, method_name
):
    worker = _worker_after_snapshot_hook(modal_app, monkeypatch, worker_class_name)
    _restore_into_another_container(monkeypatch, worker)
    attributes_by_call = _record_call_attributes(modal_app, monkeypatch)

    assert getattr(worker, method_name)(SIMULATION_PARAMS) == WORKER_RESULT

    [call_attributes] = attributes_by_call
    assert call_attributes["container_start"] == "restored_from_snapshot"
    assert call_attributes["container_call_number"] == 1


def test_each_call_in_a_container_reports_the_next_call_number(modal_app, monkeypatch):
    worker = _worker_after_snapshot_hook(
        modal_app, monkeypatch, "SimulationSegmentWorker"
    )
    _restore_into_another_container(monkeypatch, worker)
    attributes_by_call = _record_call_attributes(modal_app, monkeypatch)

    for _call in range(3):
        assert worker.run_simulation_segment(SIMULATION_PARAMS) == WORKER_RESULT

    assert [
        call_attributes["container_call_number"]
        for call_attributes in attributes_by_call
    ] == [1, 2, 3]
    assert {
        call_attributes["container_start"] for call_attributes in attributes_by_call
    } == {"restored_from_snapshot"}


def test_both_attributes_reach_the_operation_record_and_span_but_no_metric(
    modal_app, monkeypatch, recorded_observability_runtime
):
    runtime, written_records = recorded_observability_runtime
    # The runtime sets attributes on the operation's span through this method
    # alone; OpenTelemetry is disabled in the recorded runtime.
    span_attributes: dict[str, object] = {}
    set_span_attributes = runtime._otel.set_span_attributes

    def record_span_attributes(attributes):
        span_attributes.update(attributes)
        set_span_attributes(attributes)

    monkeypatch.setattr(runtime._otel, "set_span_attributes", record_span_attributes)
    # Every attribute the operation hands to its metrics, before the package
    # filters them.
    metric_attribute_sets: list[dict] = []
    for metric_method_name in ("record_operation", "record_error"):
        record_metric = getattr(runtime._otel, metric_method_name)

        def record_metric_attributes(
            duration_or_attributes, *more, record=record_metric
        ):
            attributes = more[0] if more else duration_or_attributes
            metric_attribute_sets.append(dict(attributes))
            return record(duration_or_attributes, *more)

        monkeypatch.setattr(runtime._otel, metric_method_name, record_metric_attributes)
    monkeypatch.setattr(
        modal_app, "_configure_modal_observability", lambda *, service_role: runtime
    )
    worker = _worker_after_snapshot_hook(modal_app, monkeypatch, "SimulationWorker")
    _restore_into_another_container(monkeypatch, worker)

    assert worker.run_simulation(SIMULATION_PARAMS) == WORKER_RESULT

    [completion] = [
        record
        for record in written_records
        if record.get("event.name") == "operation.completed"
    ]
    # Read after the package's attribute filter, which drops some key names.
    assert completion["attributes"]["container_start"] == "restored_from_snapshot"
    assert completion["attributes"]["container_call_number"] == 1
    assert span_attributes["container_start"] == "restored_from_snapshot"
    assert span_attributes["container_call_number"] == 1
    # Metrics carry neither: each call number would make another series.
    assert metric_attribute_sets
    for metric_attributes in metric_attribute_sets:
        assert not CONTAINER_START_KEYS & set(metric_attributes)


@pytest.mark.parametrize(
    "function_name",
    ["run_simulation", "run_simulation_segment", "run_budget_window_batch"],
)
def test_a_plain_function_sets_both_attributes_before_its_job_runs(
    modal_app, monkeypatch, function_name
):
    _record_call_attributes(modal_app, monkeypatch)
    attributes_when_the_job_started: list[dict[str, object]] = []

    def job_reading_its_operation_attributes(_params, *, runtime, **_job_options):
        attributes_when_the_job_started.append(dict(runtime.attributes))
        return WORKER_RESULT

    _replace_worker_jobs(monkeypatch, job_reading_its_operation_attributes)

    assert getattr(modal_app, function_name)(SIMULATION_PARAMS) == WORKER_RESULT

    [job_attributes] = attributes_when_the_job_started
    assert job_attributes["container_start"] == "no_snapshot"
    assert job_attributes["container_call_number"] == 1


@pytest.mark.parametrize(("worker_class_name", "method_name"), WORKER_METHODS)
def test_both_attributes_are_set_before_the_job_runs(
    modal_app, monkeypatch, worker_class_name, method_name
):
    worker = _worker_after_snapshot_hook(modal_app, monkeypatch, worker_class_name)
    _restore_into_another_container(monkeypatch, worker)
    _record_call_attributes(modal_app, monkeypatch)
    attributes_when_the_job_started: list[dict[str, object]] = []

    def job_reading_its_operation_attributes(_params, *, runtime, **_job_options):
        attributes_when_the_job_started.append(dict(runtime.attributes))
        return WORKER_RESULT

    _replace_worker_jobs(monkeypatch, job_reading_its_operation_attributes)

    assert getattr(worker, method_name)(SIMULATION_PARAMS) == WORKER_RESULT

    [job_attributes] = attributes_when_the_job_started
    assert job_attributes["container_start"] == "restored_from_snapshot"
    assert job_attributes["container_call_number"] == 1


# Each job failure with the outcome its operation record reports.
JOB_FAILURES = [
    (RuntimeError("model failed"), "error"),
    (KeyboardInterrupt(), "interrupted"),
]


@pytest.mark.parametrize("job_failure_and_outcome", JOB_FAILURES)
@pytest.mark.parametrize("worker_method", WORKER_METHODS)
def test_a_failed_or_preempted_call_still_reports_both_attributes(
    modal_app,
    monkeypatch,
    recorded_observability_runtime,
    worker_method,
    job_failure_and_outcome,
):
    worker_class_name, method_name = worker_method
    job_failure, expected_outcome = job_failure_and_outcome
    runtime, written_records = recorded_observability_runtime
    monkeypatch.setattr(
        modal_app, "_configure_modal_observability", lambda *, service_role: runtime
    )
    worker = _worker_after_snapshot_hook(modal_app, monkeypatch, worker_class_name)
    _restore_into_another_container(monkeypatch, worker)

    def failing_job(_params, **_job_options):
        raise job_failure

    _replace_worker_jobs(monkeypatch, failing_job)

    with pytest.raises(type(job_failure)):
        getattr(worker, method_name)(SIMULATION_PARAMS)

    [completion] = [
        record
        for record in written_records
        if record.get("event.name") == "operation.completed"
    ]
    assert completion["outcome"] == expected_outcome
    assert completion["attributes"]["container_start"] == "restored_from_snapshot"
    assert completion["attributes"]["container_call_number"] == 1


@pytest.mark.parametrize(("worker_class_name", "method_name"), WORKER_METHODS)
def test_a_failure_to_read_the_container_start_leaves_the_call_unchanged(
    modal_app, monkeypatch, worker_class_name, method_name
):
    from policyengine_simulation_contract import job_progress_state

    monkeypatch.setattr(
        modal_app.modal, "current_function_call_id", lambda: "fc-worker-call"
    )
    # With a call ID, the annual worker records its job stages in a Modal Dict.
    monkeypatch.setattr(
        job_progress_state, "put_job_progress", lambda _job_id, _record: None
    )
    monkeypatch.setattr(job_progress_state, "get_job_progress", lambda _job_id: None)
    worker = _worker_after_snapshot_hook(modal_app, monkeypatch, worker_class_name)
    attributes_by_call = _record_call_attributes(modal_app, monkeypatch)

    def fail_to_read_the_container_start() -> dict[str, object]:
        msg = "container start unreadable"
        raise RuntimeError(msg)

    monkeypatch.setattr(
        modal_app._CONTAINER_CALLS,
        "next_call_attributes",
        fail_to_read_the_container_start,
    )

    assert getattr(worker, method_name)(SIMULATION_PARAMS) == WORKER_RESULT

    # The call's Modal call ID is still set.
    [call_attributes] = attributes_by_call
    assert call_attributes["function_call_id"] == "fc-worker-call"
    assert not CONTAINER_START_KEYS & set(call_attributes)


@pytest.mark.parametrize(("worker_class_name", "method_name"), WORKER_METHODS)
def test_a_runtime_refusing_the_attributes_leaves_the_call_result_unchanged(
    modal_app, monkeypatch, worker_class_name, method_name
):
    worker = _worker_after_snapshot_hook(modal_app, monkeypatch, worker_class_name)
    _restore_into_another_container(monkeypatch, worker)

    def configure_refusing_runtime(*, service_role: str) -> NoOpObservabilityRuntime:
        return _AttributeRefusingRuntime()

    monkeypatch.setattr(
        modal_app, "_configure_modal_observability", configure_refusing_runtime
    )

    assert getattr(worker, method_name)(SIMULATION_PARAMS) == WORKER_RESULT
    # The call still counts in the container, so the next call is number 2.
    attributes_by_call = _record_call_attributes(modal_app, monkeypatch)
    assert getattr(worker, method_name)(SIMULATION_PARAMS) == WORKER_RESULT
    [call_attributes] = attributes_by_call
    assert call_attributes["container_start"] == "restored_from_snapshot"
    assert call_attributes["container_call_number"] == 2


def test_a_call_whose_container_start_cannot_be_read_still_counts(
    modal_app, monkeypatch
):
    worker = _worker_after_snapshot_hook(
        modal_app, monkeypatch, "SimulationSegmentWorker"
    )
    _restore_into_another_container(monkeypatch, worker)
    read_container_start = modal_app._CONTAINER_CALLS.container_start

    def fail_to_read_the_container_start():
        msg = "container start unreadable"
        raise RuntimeError(msg)

    monkeypatch.setattr(
        modal_app._CONTAINER_CALLS, "container_start", fail_to_read_the_container_start
    )
    assert worker.run_simulation_segment(SIMULATION_PARAMS) == WORKER_RESULT
    monkeypatch.setattr(
        modal_app._CONTAINER_CALLS, "container_start", read_container_start
    )
    attributes_by_call = _record_call_attributes(modal_app, monkeypatch)

    assert worker.run_simulation_segment(SIMULATION_PARAMS) == WORKER_RESULT

    # The first call was the container's first, so this is its second.
    [call_attributes] = attributes_by_call
    assert call_attributes["container_call_number"] == 2


def test_a_failure_to_record_the_snapshot_hook_still_loads_the_models(
    modal_app, monkeypatch
):
    preload_calls: list[str] = []
    monkeypatch.setattr(
        modal_app, "preload_country_models", lambda: preload_calls.append("loaded")
    )

    def fail_to_record_the_snapshot_hook():
        msg = "environment unreadable"
        raise RuntimeError(msg)

    monkeypatch.setattr(
        modal_app._CONTAINER_CALLS,
        "record_snapshot_hook",
        fail_to_record_the_snapshot_hook,
    )
    monkeypatch.setenv("MODAL_ENABLE_SNAP_RESTORE", "1")

    worker = modal_app.SimulationWorker()
    worker.load_country_models()

    # The container starts with its models, and its calls report no_snapshot.
    assert preload_calls == ["loaded"]
    attributes_by_call = _record_call_attributes(modal_app, monkeypatch)
    assert worker.run_simulation(SIMULATION_PARAMS) == WORKER_RESULT
    [call_attributes] = attributes_by_call
    assert call_attributes["container_start"] == "no_snapshot"
