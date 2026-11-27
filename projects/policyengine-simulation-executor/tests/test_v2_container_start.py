"""Tests for the container start attributes of the v2 simulation workers' calls.

Each call of a v2 simulation worker (``SimulationWorkerUS``,
``SimulationWorkerUK`` and ``SimulationSegmentWorkerUS``, and the plain
functions kept for callers on older code) says on its operation how the
container serving it started (``container_start``) and which call it is in
that container (``container_call_number``), as the run_simulation app's
workers do (``tests/test_modal_container_start.py``).
"""

from __future__ import annotations

import importlib
import sys

import pytest
from conftest import NoOpObservabilityRuntime
from fixtures.fake_modal import install_fake_modal
from policyengine_simulation_contract.stage12_simulation_workers import (
    SEGMENT_WORKER_METHOD,
    worker_class_name,
)
from policyengine_simulation_executor import stage12_runtime

WORKER_RESULT = {"ok": True}
# The Modal task IDs of the container that takes the snapshot, of a container
# restored from it, and of a container that serves a plain function.
SNAPSHOT_TASK_ID = "ta-01SNAPSHOTTING"
RESTORED_TASK_ID = "ta-01RESTORED"
PLAIN_FUNCTION_TASK_ID = "ta-01PLAINFUNCTION"
CONTAINER_START_KEYS = {"container_start", "container_call_number"}
# Each worker class's method has the name of the plain function it replaces.
WORKER_METHOD_NAMES = (
    "run_single_simulation_us",
    "run_single_simulation_uk",
    "run_single_simulation_segment_us",
)


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


class _SegmentResult:
    """Stands in for a segment's artifact descriptor."""

    def __init__(self, result: dict) -> None:
        self._result = result

    def model_dump(self, *, mode: str) -> dict:
        assert mode == "python"
        return self._result


def _replace_worker_jobs(monkeypatch, job) -> None:
    """Make every v2 worker call run ``job`` instead of its calculation."""

    def run_single_simulation(_payload, _context, **_keywords):
        return job()

    def calculate_segment(_payload, **_keywords):
        return _SegmentResult(job())

    monkeypatch.setattr(stage12_runtime, "run_single_simulation", run_single_simulation)
    monkeypatch.setattr(stage12_runtime, "calculate_segment", calculate_segment)


@pytest.fixture
def v2_app(monkeypatch):
    """Import the v2 app afresh, as a new container does, with stubbed jobs."""
    install_fake_modal(monkeypatch)
    monkeypatch.delenv("MODAL_TASK_ID", raising=False)
    monkeypatch.delenv("MODAL_ENABLE_SNAP_RESTORE", raising=False)
    sys.modules.pop("src.modal.v2_images", None)
    sys.modules.pop("src.modal.v2_app", None)
    app = importlib.import_module("src.modal.v2_app")
    # The real preload imports the country's models.
    monkeypatch.setattr(app, "preload_country_models", lambda _countries: None)
    monkeypatch.setattr(app.modal, "current_function_call_id", lambda: "fc-call")
    _replace_worker_jobs(monkeypatch, lambda: WORKER_RESULT)
    yield app
    sys.modules.pop("src.modal.v2_images", None)
    sys.modules.pop("src.modal.v2_app", None)


def _call_worker(worker, method_name: str) -> dict:
    """Call a worker method or plain function with the arguments its callers pass."""
    if method_name == SEGMENT_WORKER_METHOD:
        return worker(
            {"simulation": {"role": "baseline"}, "segment_index": 0},
            observability_context=None,
        )
    return worker({"role": "baseline"}, {}, observability_context=None)


def _record_call_attributes(v2_app, monkeypatch) -> list[dict[str, object]]:
    """Give each call its own recording runtime, as each call configures its own.

    Returns the attributes each call set, in call order.
    """
    attributes_by_call: list[dict[str, object]] = []

    def configure_recording_runtime(**_identity: object) -> NoOpObservabilityRuntime:
        runtime = _AttributeRecordingRuntime()
        attributes_by_call.append(runtime.attributes)
        return runtime

    monkeypatch.setattr(
        v2_app, "init_process_observability", configure_recording_runtime
    )
    return attributes_by_call


def _worker_after_snapshot_hook(
    v2_app, monkeypatch, method_name: str, *, snapshots_enabled: bool = True
):
    """Create a worker and run its snapshot hook, as Modal does in a new container.

    Returns the worker's method. Modal's worker sets
    ``MODAL_ENABLE_SNAP_RESTORE`` to ``1`` in containers it takes snapshots
    from or restores, and leaves it unset in a container of the same class
    that starts without snapshots.
    """
    monkeypatch.setenv("MODAL_TASK_ID", SNAPSHOT_TASK_ID)
    if snapshots_enabled:
        monkeypatch.setenv("MODAL_ENABLE_SNAP_RESTORE", "1")
    worker = getattr(v2_app, worker_class_name(method_name))()
    worker.load_country_models()
    return getattr(worker, method_name)


def _restore_into_another_container(monkeypatch, worker_method) -> None:
    """Act as a restore: the memory stays, and MODAL_TASK_ID names the new container.

    Modal 1.6.4's ``memory_restore`` sets ``MODAL_TASK_ID`` to the task ID of
    the restored container, then Modal runs the ``snap=False`` hooks.
    """
    monkeypatch.setenv("MODAL_TASK_ID", RESTORED_TASK_ID)
    worker_method.__self__.reseed_random_after_restore()


@pytest.mark.parametrize("method_name", WORKER_METHOD_NAMES)
def test_a_call_in_the_container_that_ran_the_snapshot_hook_reports_took_snapshot(
    v2_app, monkeypatch, method_name
):
    worker_method = _worker_after_snapshot_hook(v2_app, monkeypatch, method_name)
    attributes_by_call = _record_call_attributes(v2_app, monkeypatch)

    assert _call_worker(worker_method, method_name) == WORKER_RESULT

    [call_attributes] = attributes_by_call
    assert call_attributes["container_start"] == "took_snapshot"
    assert call_attributes["container_call_number"] == 1


@pytest.mark.parametrize("method_name", WORKER_METHOD_NAMES)
def test_a_call_in_a_restored_container_reports_restored_from_snapshot(
    v2_app, monkeypatch, method_name
):
    worker_method = _worker_after_snapshot_hook(v2_app, monkeypatch, method_name)
    _restore_into_another_container(monkeypatch, worker_method)
    attributes_by_call = _record_call_attributes(v2_app, monkeypatch)

    assert _call_worker(worker_method, method_name) == WORKER_RESULT

    [call_attributes] = attributes_by_call
    assert call_attributes["container_start"] == "restored_from_snapshot"
    assert call_attributes["container_call_number"] == 1


@pytest.mark.parametrize("method_name", WORKER_METHOD_NAMES)
def test_a_worker_container_started_without_snapshots_reports_no_snapshot(
    v2_app, monkeypatch, method_name
):
    worker_method = _worker_after_snapshot_hook(
        v2_app, monkeypatch, method_name, snapshots_enabled=False
    )
    attributes_by_call = _record_call_attributes(v2_app, monkeypatch)

    assert _call_worker(worker_method, method_name) == WORKER_RESULT

    # The hook loaded the models, but Modal took no snapshot from this container.
    [call_attributes] = attributes_by_call
    assert call_attributes["container_start"] == "no_snapshot"
    assert call_attributes["container_call_number"] == 1


@pytest.mark.parametrize("method_name", WORKER_METHOD_NAMES)
def test_a_plain_function_call_reports_a_container_started_without_a_snapshot(
    v2_app, monkeypatch, method_name
):
    # The plain functions kept for callers on older code have no snapshot hook.
    monkeypatch.setenv("MODAL_TASK_ID", PLAIN_FUNCTION_TASK_ID)
    attributes_by_call = _record_call_attributes(v2_app, monkeypatch)

    assert _call_worker(getattr(v2_app, method_name), method_name) == WORKER_RESULT

    [call_attributes] = attributes_by_call
    assert call_attributes["container_start"] == "no_snapshot"
    assert call_attributes["container_call_number"] == 1


@pytest.mark.parametrize("method_name", WORKER_METHOD_NAMES)
def test_each_call_in_a_container_reports_the_next_call_number(
    v2_app, monkeypatch, method_name
):
    worker_method = _worker_after_snapshot_hook(v2_app, monkeypatch, method_name)
    _restore_into_another_container(monkeypatch, worker_method)
    attributes_by_call = _record_call_attributes(v2_app, monkeypatch)

    for _call in range(3):
        assert _call_worker(worker_method, method_name) == WORKER_RESULT

    assert [
        call_attributes["container_call_number"]
        for call_attributes in attributes_by_call
    ] == [1, 2, 3]
    assert {
        call_attributes["container_start"] for call_attributes in attributes_by_call
    } == {"restored_from_snapshot"}


@pytest.mark.parametrize("method_name", WORKER_METHOD_NAMES)
def test_both_attributes_reach_the_operation_record_and_span_but_no_metric(
    v2_app, monkeypatch, recorded_observability_runtime, method_name
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
    monkeypatch.setattr(v2_app, "init_process_observability", lambda **_: runtime)
    worker_method = _worker_after_snapshot_hook(v2_app, monkeypatch, method_name)
    _restore_into_another_container(monkeypatch, worker_method)

    assert _call_worker(worker_method, method_name) == WORKER_RESULT

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


@pytest.mark.parametrize("method_name", WORKER_METHOD_NAMES)
def test_both_attributes_are_set_before_the_calculation_runs(
    v2_app, monkeypatch, method_name
):
    worker_method = _worker_after_snapshot_hook(v2_app, monkeypatch, method_name)
    _restore_into_another_container(monkeypatch, worker_method)
    attributes_by_call = _record_call_attributes(v2_app, monkeypatch)
    attributes_when_the_calculation_started: list[dict[str, object]] = []

    def calculation_reading_its_operation_attributes():
        attributes_when_the_calculation_started.append(dict(attributes_by_call[-1]))
        return WORKER_RESULT

    _replace_worker_jobs(monkeypatch, calculation_reading_its_operation_attributes)

    assert _call_worker(worker_method, method_name) == WORKER_RESULT

    [calculation_attributes] = attributes_when_the_calculation_started
    assert calculation_attributes["container_start"] == "restored_from_snapshot"
    assert calculation_attributes["container_call_number"] == 1


# Each calculation failure with the outcome its operation record reports.
CALCULATION_FAILURES = [
    (RuntimeError("model failed"), "error"),
    (KeyboardInterrupt(), "interrupted"),
]


@pytest.mark.parametrize("failure_and_outcome", CALCULATION_FAILURES)
@pytest.mark.parametrize("method_name", WORKER_METHOD_NAMES)
def test_a_failed_or_preempted_call_still_reports_both_attributes(
    v2_app,
    monkeypatch,
    recorded_observability_runtime,
    method_name,
    failure_and_outcome,
):
    calculation_failure, expected_outcome = failure_and_outcome
    runtime, written_records = recorded_observability_runtime
    monkeypatch.setattr(v2_app, "init_process_observability", lambda **_: runtime)
    worker_method = _worker_after_snapshot_hook(v2_app, monkeypatch, method_name)
    _restore_into_another_container(monkeypatch, worker_method)

    def failing_calculation():
        raise calculation_failure

    _replace_worker_jobs(monkeypatch, failing_calculation)

    with pytest.raises(type(calculation_failure)):
        _call_worker(worker_method, method_name)

    [completion] = [
        record
        for record in written_records
        if record.get("event.name") == "operation.completed"
    ]
    assert completion["outcome"] == expected_outcome
    assert completion["attributes"]["container_start"] == "restored_from_snapshot"
    assert completion["attributes"]["container_call_number"] == 1


@pytest.mark.parametrize("method_name", WORKER_METHOD_NAMES)
def test_a_runtime_refusing_the_attributes_leaves_the_call_result_unchanged(
    v2_app, monkeypatch, method_name
):
    worker_method = _worker_after_snapshot_hook(v2_app, monkeypatch, method_name)
    _restore_into_another_container(monkeypatch, worker_method)
    monkeypatch.setattr(
        v2_app, "init_process_observability", lambda **_: _AttributeRefusingRuntime()
    )

    assert _call_worker(worker_method, method_name) == WORKER_RESULT
    # The call still counts in the container, so the next call is number 2.
    attributes_by_call = _record_call_attributes(v2_app, monkeypatch)
    assert _call_worker(worker_method, method_name) == WORKER_RESULT
    [call_attributes] = attributes_by_call
    assert call_attributes["container_start"] == "restored_from_snapshot"
    assert call_attributes["container_call_number"] == 2


@pytest.mark.parametrize("method_name", WORKER_METHOD_NAMES)
def test_a_failure_to_read_the_container_start_leaves_the_call_unchanged(
    v2_app, monkeypatch, method_name
):
    worker_method = _worker_after_snapshot_hook(v2_app, monkeypatch, method_name)
    attributes_by_call = _record_call_attributes(v2_app, monkeypatch)

    def fail_to_read_the_container_start():
        msg = "container start unreadable"
        raise RuntimeError(msg)

    monkeypatch.setattr(
        v2_app._CONTAINER_CALLS, "container_start", fail_to_read_the_container_start
    )

    assert _call_worker(worker_method, method_name) == WORKER_RESULT

    [call_attributes] = attributes_by_call
    assert not CONTAINER_START_KEYS & set(call_attributes)


@pytest.mark.parametrize("method_name", WORKER_METHOD_NAMES)
def test_a_failure_to_record_the_snapshot_hook_still_loads_the_models(
    v2_app, monkeypatch, method_name
):
    preloaded_countries: list[tuple[str, ...]] = []
    monkeypatch.setattr(
        v2_app,
        "preload_country_models",
        lambda countries: preloaded_countries.append(tuple(countries)),
    )

    def fail_to_record_the_snapshot_hook():
        msg = "environment unreadable"
        raise RuntimeError(msg)

    monkeypatch.setattr(
        v2_app._CONTAINER_CALLS,
        "record_snapshot_hook",
        fail_to_record_the_snapshot_hook,
    )

    worker_method = _worker_after_snapshot_hook(v2_app, monkeypatch, method_name)

    # The container starts with its models, and its calls report no_snapshot.
    assert len(preloaded_countries) == 1
    attributes_by_call = _record_call_attributes(v2_app, monkeypatch)
    assert _call_worker(worker_method, method_name) == WORKER_RESULT
    [call_attributes] = attributes_by_call
    assert call_attributes["container_start"] == "no_snapshot"
