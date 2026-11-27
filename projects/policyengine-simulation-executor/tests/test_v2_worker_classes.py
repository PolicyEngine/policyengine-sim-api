"""The v2 runtime's simulation workers: Modal classes restored from memory snapshots."""

from __future__ import annotations

import importlib
import random
import sys
from contextlib import contextmanager

import pytest
from fixtures.fake_modal import install_fake_modal
from policyengine_simulation_contract.stage12_simulation_workers import (
    SEGMENT_WORKER_CLASS,
    SEGMENT_WORKER_METHOD,
    worker_class_name,
)
from policyengine_simulation_executor.stage12_timeouts import (
    UK_SIMULATION_TIMEOUT_SECONDS,
    US_SIMULATION_TIMEOUT_SECONDS,
)

WORKER_METHOD_NAMES = (
    "run_single_simulation_us",
    "run_single_simulation_uk",
    "run_single_simulation_segment_us",
)


def _load_v2_app(monkeypatch):
    install_fake_modal(monkeypatch)
    sys.modules.pop("src.modal.v2_images", None)
    sys.modules.pop("src.modal.v2_app", None)
    return importlib.import_module("src.modal.v2_app")


def _worker_class(v2_app, method_name: str) -> type:
    class_name = worker_class_name(method_name)
    assert class_name is not None, method_name
    return getattr(v2_app, class_name)


def _expected_worker_options(v2_app) -> dict[str, dict]:
    """Each worker's Modal options, shared by its class and its plain function."""
    simulation_worker_resources = {
        "cpu": 8.0,
        "memory": 32768,
        "retries": 0,
        # Both simulations of every coordinator call that may run at once,
        # and the runs the entry service starts directly.
        "max_containers": 50,
        "secrets": v2_app.report_run_writer_secrets,
    }
    return {
        "run_single_simulation_us": {
            "image": v2_app.us_worker_image,
            "timeout": US_SIMULATION_TIMEOUT_SECONDS,
            **simulation_worker_resources,
        },
        "run_single_simulation_uk": {
            "image": v2_app.uk_worker_image,
            "timeout": UK_SIMULATION_TIMEOUT_SECONDS,
            **simulation_worker_resources,
        },
        "run_single_simulation_segment_us": {
            "image": v2_app.us_worker_image,
            "cpu": 8.0,
            "memory": 32768,
            "timeout": 3600,
            "retries": 0,
            "max_containers": 1000,
            # A segment returns its result to its worker and records nothing.
            "secrets": v2_app.worker_secrets,
        },
    }


def test_the_v2_simulation_workers_are_classes_restored_from_memory_snapshots(
    monkeypatch,
) -> None:
    v2_app = _load_v2_app(monkeypatch)

    assert dict(v2_app.app.cls_calls) == {
        worker_class_name(method_name): {**options, "enable_memory_snapshot": True}
        for method_name, options in _expected_worker_options(v2_app).items()
    }


def test_each_v2_worker_class_defines_the_method_its_callers_name(
    monkeypatch,
) -> None:
    v2_app = _load_v2_app(monkeypatch)
    recorded_methods = v2_app.modal.lifecycle_hooks.methods

    for method_name in WORKER_METHOD_NAMES:
        worker_method = getattr(_worker_class(v2_app, method_name), method_name)
        assert worker_method in recorded_methods
    # A segmented US simulation spawns its segments through this class.
    segment_worker = getattr(v2_app, SEGMENT_WORKER_CLASS)
    assert getattr(segment_worker, SEGMENT_WORKER_METHOD) in recorded_methods


def test_plain_functions_keep_the_workers_for_callers_on_older_code(
    monkeypatch,
) -> None:
    v2_app = _load_v2_app(monkeypatch)
    function_options = dict(v2_app.app.function_calls)

    # An entry service revision from before the classes, and a coordinator or
    # US worker call running while the app is redeployed in place, look these
    # functions up by name. They keep the workers' options, without snapshots.
    for method_name, options in _expected_worker_options(v2_app).items():
        assert function_options[method_name] == options


def test_each_v2_worker_class_has_one_snapshot_hook_and_one_restore_hook(
    monkeypatch,
) -> None:
    v2_app = _load_v2_app(monkeypatch)

    enter_hooks = v2_app.modal.lifecycle_hooks.enter_hooks
    snapshot_hooks = [method for method, snap in enter_hooks if snap]
    restore_hooks = [method for method, snap in enter_hooks if not snap]
    assert len(snapshot_hooks) == 1
    assert len(restore_hooks) == 1
    for method_name in WORKER_METHOD_NAMES:
        worker_class = _worker_class(v2_app, method_name)
        assert worker_class.load_country_models is snapshot_hooks[0]
        assert worker_class.reseed_random_after_restore is restore_hooks[0]


def test_each_v2_worker_class_loads_only_its_own_countrys_models(
    monkeypatch,
) -> None:
    v2_app = _load_v2_app(monkeypatch)
    preloaded_countries: list[tuple[str, ...]] = []
    monkeypatch.setattr(
        v2_app,
        "preload_country_models",
        lambda countries: preloaded_countries.append(tuple(countries)),
    )

    for method_name in WORKER_METHOD_NAMES:
        _worker_class(v2_app, method_name)().load_country_models()

    # Each image holds one country's data. A UK container never imports the
    # US models, so a failed US import can't affect it, and the reverse.
    assert preloaded_countries == [("us",), ("uk",), ("us",)]


def test_the_v2_snapshot_hook_configures_no_observability(monkeypatch) -> None:
    v2_app = _load_v2_app(monkeypatch)
    preloads: list[str] = []
    monkeypatch.setattr(
        v2_app, "preload_country_models", lambda _countries: preloads.append("ran")
    )

    def observability_before_the_snapshot(*_arguments, **_keywords):
        # A process identity configured here would be shared by every
        # container restored from the snapshot.
        msg = "observability must be configured per call, after the restore"
        raise AssertionError(msg)

    monkeypatch.setattr(
        v2_app, "init_process_observability", observability_before_the_snapshot
    )

    for method_name in WORKER_METHOD_NAMES:
        _worker_class(v2_app, method_name)().load_country_models()

    assert preloads == ["ran", "ran", "ran"]


def test_each_restored_v2_container_draws_its_own_random_numbers(monkeypatch) -> None:
    v2_app = _load_v2_app(monkeypatch)
    # Every container restored from one snapshot starts from the random state
    # the snapshot captured, and Python reseeds only after a fork.
    snapshot_state = random.getstate()

    v2_app.SimulationWorkerUS().reseed_random_after_restore()
    first_container_span_id = random.getrandbits(64)
    random.setstate(snapshot_state)
    v2_app.SimulationSegmentWorkerUS().reseed_random_after_restore()
    second_container_span_id = random.getrandbits(64)

    # OpenTelemetry draws span IDs this way, and the database and dataset
    # download retries draw their delays from the same generator.
    assert first_container_span_id != second_container_span_id


class RecordingRuntime:
    """Stands in for the per-call observability runtime."""

    def __init__(self) -> None:
        self.operation_names: list[str] = []
        self.shutdown_calls = 0

    def set_context(self, **_attributes) -> None:
        pass

    def capture_context(self) -> dict:
        return {}

    @contextmanager
    def operation(self, name, **_keywords):
        self.operation_names.append(name)
        yield

    def shutdown(self) -> None:
        self.shutdown_calls += 1


def _call_worker(monkeypatch, v2_app, worker) -> dict:
    """Call ``worker`` as its callers do; return what it ran and returned."""
    from policyengine_simulation_executor import stage12_runtime

    runtime = RecordingRuntime()
    received_keywords: dict = {}
    monkeypatch.setattr(v2_app, "init_process_observability", lambda **_: runtime)
    monkeypatch.setattr(
        v2_app.modal, "current_function_call_id", lambda: "fc-worker-call"
    )

    def run_single_simulation(payload, context, **keywords):
        received_keywords.update(keywords)
        return {"calculated": payload["role"], "context": context}

    class SegmentResult:
        def model_dump(self, *, mode):
            assert mode == "python"
            return {"calculated": "segment"}

    monkeypatch.setattr(stage12_runtime, "run_single_simulation", run_single_simulation)
    monkeypatch.setattr(
        stage12_runtime, "calculate_segment", lambda *_, **__: SegmentResult()
    )
    if worker.__name__ == SEGMENT_WORKER_METHOD:
        result = worker(
            {"simulation": {"role": "baseline"}, "segment_index": 0},
            observability_context=None,
        )
    else:
        result = worker({"role": "baseline"}, {}, observability_context=None)
    return {
        "result": result,
        "operations": runtime.operation_names,
        "shutdown_calls": runtime.shutdown_calls,
        "modal_call_id": received_keywords.get("modal_call_id"),
        "required_country": received_keywords.get("required_country"),
    }


@pytest.mark.parametrize("method_name", WORKER_METHOD_NAMES)
def test_a_v2_worker_class_method_runs_the_same_code_as_its_plain_function(
    monkeypatch, method_name
) -> None:
    v2_app = _load_v2_app(monkeypatch)
    worker_instance = _worker_class(v2_app, method_name)()

    through_the_class = _call_worker(
        monkeypatch, v2_app, getattr(worker_instance, method_name)
    )
    through_the_function = _call_worker(
        monkeypatch, v2_app, getattr(v2_app, method_name)
    )

    assert through_the_class == through_the_function
    assert through_the_class["shutdown_calls"] == 1
    assert len(through_the_class["operations"]) == 1
    if method_name != SEGMENT_WORKER_METHOD:
        assert through_the_class["modal_call_id"] == "fc-worker-call"


@pytest.mark.parametrize(
    "client_version",
    # Development builds and release candidates of 1.6.4 come before it.
    ["1.4.2", "1.6.3", "1.6.4rc1", "not-a-version"],
)
def test_a_v2_deploy_with_a_modal_client_older_than_1_6_4_is_refused(
    monkeypatch, client_version
) -> None:
    # Containers restored from a memory snapshot reload Secret values only
    # from Modal 1.6.4, and each app runs the client that deployed it.
    install_fake_modal(monkeypatch)
    monkeypatch.setattr(sys.modules["modal"], "__version__", client_version)
    sys.modules.pop("src.modal.v2_images", None)
    sys.modules.pop("src.modal.v2_app", None)

    with pytest.raises(RuntimeError, match=r"1\.6\.4") as refused:
        importlib.import_module("src.modal.v2_app")

    assert client_version in str(refused.value)
    sys.modules.pop("src.modal.v2_images", None)
    sys.modules.pop("src.modal.v2_app", None)


def test_the_v2_client_check_comes_before_the_cache_manifest_is_read(
    monkeypatch,
) -> None:
    install_fake_modal(monkeypatch)
    monkeypatch.setattr(sys.modules["modal"], "__version__", "1.6.3")
    monkeypatch.setenv("STAGE12_CACHE_BUCKET", "cache-bucket")
    monkeypatch.setenv("STAGE12_CACHE_MANIFEST_DIGEST", "a" * 64)
    manifest_reads: list[str] = []

    class RecordingCacheStore:
        def __init__(self, bucket_name, **_keywords):
            manifest_reads.append(bucket_name)

        def read_manifest(self, digest):
            manifest_reads.append(digest)

    from policyengine_simulation_executor.stage12_cache import store

    monkeypatch.setattr(store, "Stage12CacheStore", RecordingCacheStore)
    sys.modules.pop("src.modal.v2_images", None)
    sys.modules.pop("src.modal.v2_app", None)

    with pytest.raises(RuntimeError, match=r"1\.6\.4"):
        importlib.import_module("src.modal.v2_app")

    # A refused deploy reads nothing from the cache bucket.
    assert manifest_reads == []
    sys.modules.pop("src.modal.v2_images", None)
    sys.modules.pop("src.modal.v2_app", None)


def test_a_v2_container_imports_the_app_whatever_the_client_version(
    monkeypatch,
) -> None:
    # Inside a container the client is the one that deployed the app, so the
    # check applies only where the app is deployed or run from.
    install_fake_modal(monkeypatch)
    monkeypatch.setattr(sys.modules["modal"], "__version__", "1.4.2")
    monkeypatch.setattr(sys.modules["modal"], "is_local", lambda: False)
    sys.modules.pop("src.modal.v2_images", None)
    sys.modules.pop("src.modal.v2_app", None)

    v2_app = importlib.import_module("src.modal.v2_app")

    assert v2_app.SimulationWorkerUS is not None
    sys.modules.pop("src.modal.v2_images", None)
    sys.modules.pop("src.modal.v2_app", None)
