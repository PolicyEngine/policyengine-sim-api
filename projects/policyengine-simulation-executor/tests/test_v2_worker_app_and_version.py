"""Tests that each v2 simulation worker call names the app and .py version serving it.

The v2 workers' records say which worker served a call through their service
name, but not which deployed v2 app or which policyengine.py version did. As
the run_simulation app's workers do, each call's runtime carries the .py
version as its service version, and its operation names the Modal app in
``modal_app_name``.
"""

from __future__ import annotations

import importlib
import importlib.metadata
import sys
from contextlib import contextmanager

import pytest
from fixtures.fake_modal import install_fake_modal
from policyengine_simulation_contract.stage12_simulation_workers import (
    SEGMENT_WORKER_METHOD,
    worker_class_name,
)
from policyengine_simulation_executor import stage12_runtime

WORKER_RESULT = {"ok": True}
# Each worker with the service name its records carry today; existing queries
# select the workers' records by these names.
WORKER_SERVICE_NAMES = {
    "run_single_simulation_us": "policyengine-stage12-us-worker",
    "run_single_simulation_uk": "policyengine-stage12-uk-worker",
    "run_single_simulation_segment_us": "policyengine-stage12-us-segment-worker",
}


class _SegmentResult:
    """Stands in for a segment's artifact descriptor."""

    def model_dump(self, *, mode: str) -> dict:
        assert mode == "python"
        return WORKER_RESULT


class _OperationRecordingRuntime:
    """Keeps the attributes each operation opens with."""

    def __init__(self) -> None:
        self.operation_attributes: list[dict] = []

    def set_context(self, **_attributes: object) -> None:
        return None

    def capture_context(self) -> dict:
        return {}

    @contextmanager
    def operation(self, _name, *, attributes, **_keywords):
        self.operation_attributes.append(dict(attributes))
        yield

    def shutdown(self) -> None:
        return None


@pytest.fixture
def v2_app(monkeypatch):
    """Import the v2 app afresh, with every worker's calculation stubbed."""
    install_fake_modal(monkeypatch)
    sys.modules.pop("src.modal.v2_images", None)
    sys.modules.pop("src.modal.v2_app", None)
    app = importlib.import_module("src.modal.v2_app")
    monkeypatch.setattr(app, "preload_country_models", lambda _countries: None)
    monkeypatch.setattr(app.modal, "current_function_call_id", lambda: "fc-call")
    monkeypatch.setattr(
        stage12_runtime,
        "run_single_simulation",
        lambda _payload, _context, **_keywords: WORKER_RESULT,
    )
    monkeypatch.setattr(
        stage12_runtime, "calculate_segment", lambda _payload, **_: _SegmentResult()
    )
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


def _worker(v2_app, method_name: str, worker_kind: str):
    """Return the class method or the plain function serving one worker's calls."""
    if worker_kind == "class_method":
        return getattr(getattr(v2_app, worker_class_name(method_name))(), method_name)
    return getattr(v2_app, method_name)


@pytest.mark.parametrize("worker_kind", ["class_method", "plain_function"])
@pytest.mark.parametrize("method_name", sorted(WORKER_SERVICE_NAMES))
def test_each_v2_worker_call_names_its_app_and_py_version(
    v2_app, monkeypatch, method_name, worker_kind
):
    runtime = _OperationRecordingRuntime()
    runtime_identities: list[dict] = []

    def configure_recording_runtime(**identity):
        runtime_identities.append(identity)
        return runtime

    monkeypatch.setattr(
        v2_app, "init_process_observability", configure_recording_runtime
    )

    worker = _worker(v2_app, method_name, worker_kind)
    assert _call_worker(worker, method_name) == WORKER_RESULT

    [identity] = runtime_identities
    # The .py version installed in the worker's image, which serves the call.
    assert identity["service_version"] == importlib.metadata.version("policyengine")
    # The service name stays the worker's, as existing queries expect.
    assert identity["service_name"] == WORKER_SERVICE_NAMES[method_name]
    [attributes] = runtime.operation_attributes
    assert attributes["modal_app_name"] == v2_app.APP_NAME


@pytest.mark.parametrize("method_name", sorted(WORKER_SERVICE_NAMES))
def test_the_app_name_reaches_the_operation_record(
    v2_app, monkeypatch, recorded_observability_runtime, method_name
):
    runtime, written_records = recorded_observability_runtime
    monkeypatch.setattr(v2_app, "init_process_observability", lambda **_: runtime)

    assert _call_worker(getattr(v2_app, method_name), method_name) == WORKER_RESULT

    [completion] = [
        record
        for record in written_records
        if record.get("event.name") == "operation.completed"
    ]
    # Read after the package's attribute filter, which drops some key names.
    assert completion["attributes"]["modal_app_name"] == v2_app.APP_NAME
