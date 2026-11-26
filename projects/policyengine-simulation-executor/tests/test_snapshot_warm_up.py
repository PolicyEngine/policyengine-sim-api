"""The warm-up calculation the simulation workers run before their memory snapshot.

A process's first calculation for a year does one-time work that later
calculations reuse. The workers' snapshot hook runs one calculation of a
region group whose dataset and baseline are in the image, so containers
restored from the snapshot start with that work done. These tests replace
the calculation itself with a fake that touches .py's in-process simulation
cache and the data folder as a real one does.
"""

from __future__ import annotations

import importlib
import logging
import os
import sys
from collections.abc import Iterator, MutableMapping
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from conftest import NoOpObservabilityRuntime
from fixtures.fake_modal import install_fake_modal
from policyengine_observability.runtime import ObservabilityRuntime
from policyengine_simulation_executor import (
    precompute_planning,
    simulation_runtime,
    state_datasets,
)
from policyengine_simulation_executor.national_partition import national_region_groups
from src.modal import segmented_national

if TYPE_CHECKING:
    from policyengine.core.cache import LRUCache

# The image's baked year file for the warm-up year, and the baked baseline's
# storage ID, as the precompute names them.
BAKED_DATASET_NAME = "enhanced_cps_2024"
BAKED_DATASET_FILENAME = "enhanced_cps_2024_year_2026.h5"
BAKED_BASELINE_STORAGE_ID = "bl1-warmupgroupbaseline"
SNAPSHOT_TASK_ID = "ta-01SNAPSHOTTING"
# A request the container serves after its snapshot hook: another region
# group, under another reform, as in the measurement on Modal `testing`.
LATER_REQUEST = {
    "country": "us",
    "scope": "macro",
    "time_period": 2026,
    "region_group": ["state/mn", "state/mo", "state/nh"],
    "reform": {
        "gov.irs.credits.ctc.amount.base[0].amount": {"2026-01-01.2100-12-31": 3000}
    },
    "_emit_microdata": True,
}
US_MODEL_MODULES = ("policyengine_us", "policyengine.tax_benefit_models.us")


class _PolicyEngineLikeCalculation:
    """Stands in for the simulation path, leaving what a real calculation leaves.

    A real calculation's ``ensure()`` adds its baseline to .py's in-process
    cache under the baseline's deterministic storage ID, adds its reform under
    a new random ID, and saves the reform's output beside the dataset. Each
    call returns what it found in the cache and the data folder before it
    calculated, which a real call's result depends on: a cached baseline is
    used instead of the baked file.
    """

    def __init__(self, cache: LRUCache, data_folder) -> None:
        self.cache = cache
        self.data_folder = data_folder
        self.requests: list[dict] = []
        self.runtimes: list[object] = []

    def __call__(self, params: dict, *, runtime, progress=None) -> dict:
        self.requests.append(params)
        self.runtimes.append(runtime)
        with runtime.span("calculation"):
            runtime.set_context(region="+".join(params.get("region_group", [])))
        state_seen = {
            "cached_simulations": sorted(self.cache._cache),
            "data_folder_files": sorted(
                path.name for path in self.data_folder.iterdir()
            ),
        }
        reform_storage_id = str(uuid4())
        self.cache.add(BAKED_BASELINE_STORAGE_ID, SimpleNamespace(role="baseline"))
        self.cache.add(reform_storage_id, SimpleNamespace(role="reform"))
        (self.data_folder / f"{reform_storage_id}.h5").write_bytes(b"reform output")
        return {
            "state_seen": state_seen,
            "region_group": params.get("region_group"),
            "reform": params.get("reform"),
        }


class _CallRuntime(NoOpObservabilityRuntime):
    """A worker call's runtime that records and sends nothing."""

    def shutdown(self) -> None:
        return None


class _EnvironmentReadRecorder(MutableMapping):
    """Wraps ``os.environ`` and records the name of every variable read."""

    def __init__(self, environment: MutableMapping) -> None:
        self.environment = environment
        self.names_read: set[str] = set()

    def __getitem__(self, name: str) -> str:
        self.names_read.add(name)
        return self.environment[name]

    def __setitem__(self, name: str, value: str) -> None:
        self.environment[name] = value

    def __delitem__(self, name: str) -> None:
        del self.environment[name]

    def __iter__(self) -> Iterator[str]:
        # Listing the variables reads them all, as the credential setup's
        # log of Google-related variables does.
        for name in list(self.environment):
            self.names_read.add(name)
            yield name

    def __len__(self) -> int:
        return len(self.environment)


@pytest.fixture
def warm_up_module():
    return importlib.import_module("src.modal.snapshot_warm_up")


@pytest.fixture
def snapshotted_container(monkeypatch):
    """Act as a container Modal snapshots, with the US models preloaded."""
    monkeypatch.setenv("MODAL_ENABLE_SNAP_RESTORE", "1")
    monkeypatch.setenv("MODAL_TASK_ID", SNAPSHOT_TASK_ID)
    for module_name in US_MODEL_MODULES:
        monkeypatch.setitem(
            sys.modules,
            module_name,
            sys.modules.get(module_name) or ModuleType(module_name),
        )


@pytest.fixture
def requested_identities(monkeypatch):
    """Answer the precompute's baseline identity, recording each year and group."""
    requested: list[tuple[int, list[str]]] = []

    def baked_identity(year, group):
        requested.append((year, list(group)))
        return SimpleNamespace(
            dataset=SimpleNamespace(
                dataset=BAKED_DATASET_NAME, filename=BAKED_DATASET_FILENAME
            ),
            storage_id=BAKED_BASELINE_STORAGE_ID,
        )

    monkeypatch.setattr(precompute_planning, "cohort_identity", baked_identity)
    return requested


@pytest.fixture
def image_data_folder(tmp_path, monkeypatch, requested_identities):
    """Create the image's data folder, holding the warm-up group's baked inputs."""
    data_folder = tmp_path / "data"
    data_folder.mkdir()
    (data_folder / BAKED_DATASET_FILENAME).write_bytes(b"baked year file")
    (data_folder / f"{BAKED_BASELINE_STORAGE_ID}.h5").write_bytes(b"baked baseline")
    monkeypatch.setenv("POLICYENGINE_DATA_FOLDER", str(data_folder))
    # .py's own checks would read the files' stamps, which these stand-ins lack.
    monkeypatch.setattr(
        state_datasets, "year_file_reuse_problem", lambda _path, _dataset: None
    )
    return data_folder


@pytest.fixture
def simulation_cache(monkeypatch):
    """Start .py's in-process simulation cache empty, as in a new container."""
    # Imported here, after the executor modules set
    # POLICYENGINE_SKIP_COUNTRY_IMPORTS: importing .py first would load both
    # countries' models.
    from policyengine.core import simulation as policyengine_simulation
    from policyengine.core.cache import LRUCache

    cache = LRUCache(max_size=100)
    monkeypatch.setattr(policyengine_simulation, "_cache", cache)
    return cache


@pytest.fixture
def calculation(monkeypatch, simulation_cache, image_data_folder):
    """Replace the simulation path's calculation with a .py-like fake."""
    fake_calculation = _PolicyEngineLikeCalculation(simulation_cache, image_data_folder)
    monkeypatch.setattr(
        simulation_runtime, "_run_simulation_impl_core", fake_calculation
    )
    return fake_calculation


@pytest.fixture
def modal_app(monkeypatch):
    """Import the app afresh, as a new container does, without the real preload."""
    install_fake_modal(monkeypatch)
    sys.modules.pop("src.modal.app", None)
    app = importlib.import_module("src.modal.app")
    monkeypatch.setattr(app, "preload_country_models", lambda: None)
    yield app
    sys.modules.pop("src.modal.app", None)


def _configured_by(runtime_class):
    """Return a stand-in for ``_configure_modal_observability``."""

    def configure_runtime(*, service_role: str):
        return runtime_class()

    return configure_runtime


def _restore_initial_state(cache: LRUCache, data_folder, files_before: set) -> None:
    """Put the cache and data folder back as they were when the container started."""
    cache.clear()
    for path in data_folder.iterdir():
        if path.name not in files_before:
            path.unlink()


def test_the_snapshot_hook_runs_the_warm_up_once_after_the_preload(
    modal_app, monkeypatch
):
    steps: list[str] = []
    monkeypatch.setattr(
        modal_app, "preload_country_models", lambda: steps.append("preload")
    )
    monkeypatch.setattr(
        modal_app, "warm_up_first_calculation", lambda: steps.append("warm-up")
    )

    for worker_class_name in ("SimulationWorker", "SimulationSegmentWorker"):
        worker = getattr(modal_app, worker_class_name)()
        worker.load_country_models()
        # Restored containers run only the snapshot=False hook.
        worker.reseed_random_after_restore()

    assert steps == ["preload", "warm-up", "preload", "warm-up"]


@pytest.mark.usefixtures("snapshotted_container")
def test_the_warm_up_calculates_one_baked_group_for_the_first_precompute_year(
    warm_up_module, calculation, requested_identities
):
    warm_up_module.warm_up_first_calculation()

    [request] = calculation.requests
    first_precompute_year = precompute_planning.PRECOMPUTE_YEARS[0]
    # A segmented national run's child request, which the precompute bakes a
    # current-law baseline for in every year it covers.
    assert request["country"] == "us"
    assert request["scope"] == "macro"
    assert int(request["time_period"]) == first_precompute_year
    assert request["region_group"] in national_region_groups("us")
    assert request["_emit_microdata"] is True
    assert "region" not in request
    assert "baseline" not in request
    # A reform, so the warm-up calculates the reform as a request does.
    assert request["reform"]
    assert requested_identities == [(first_precompute_year, request["region_group"])]


@pytest.mark.usefixtures("snapshotted_container")
@pytest.mark.parametrize(
    "missing_filename",
    [BAKED_DATASET_FILENAME, f"{BAKED_BASELINE_STORAGE_ID}.h5"],
)
def test_the_warm_up_skips_with_a_warning_unless_the_image_holds_its_inputs(
    warm_up_module, calculation, image_data_folder, missing_filename, caplog
):
    # Without a baked year file the dataset would be built from its source,
    # which may download it; without a baked baseline it would be calculated.
    (image_data_folder / missing_filename).unlink()

    with caplog.at_level(logging.WARNING, logger=warm_up_module.__name__):
        warm_up_module.warm_up_first_calculation()

    assert calculation.requests == []
    assert missing_filename in caplog.text


@pytest.mark.usefixtures("snapshotted_container")
def test_the_warm_up_skips_with_a_warning_when_the_baked_year_file_would_be_rebuilt(
    warm_up_module, calculation, monkeypatch, caplog
):
    # .py's ensure_datasets rebuilds a year file that fails its reuse checks
    # from the dataset's source, which it may download from Hugging Face.
    checked_year_files: list[tuple[str, str]] = []

    def out_of_date(path, dataset):
        checked_year_files.append((Path(path).name, dataset))
        return "the year file records another register of renamed inputs"

    monkeypatch.setattr(state_datasets, "year_file_reuse_problem", out_of_date)

    with caplog.at_level(logging.WARNING, logger=warm_up_module.__name__):
        warm_up_module.warm_up_first_calculation()

    assert calculation.requests == []
    assert checked_year_files == [(BAKED_DATASET_FILENAME, BAKED_DATASET_NAME)]
    assert "another register of renamed inputs" in caplog.text


@pytest.mark.usefixtures("snapshotted_container")
def test_the_warm_up_reads_no_secret_and_sets_up_no_credentials(
    warm_up_module, calculation, monkeypatch, caplog
):
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
    monkeypatch.setenv(
        "GOOGLE_APPLICATION_CREDENTIALS_JSON", '{"type": "service_account"}'
    )
    monkeypatch.setenv("HUGGING_FACE_TOKEN", "hf_secret_value")
    # Recorded as well as raised: the warm-up turns an exception into a
    # warning, so a raise alone could go unnoticed.
    refusals: list[str] = []

    def refuse(*_args, **_kwargs):
        refusals.append("credential setup")
        msg = "the warm-up must not set up credentials before the snapshot"
        raise AssertionError(msg)

    monkeypatch.setattr(simulation_runtime, "setup_gcp_credentials", refuse)
    monkeypatch.setattr(simulation_runtime, "run_simulation_impl", refuse)
    # Records the warm-up's own reads. The calculation is replaced here, so
    # what the simulation path reads is checked by reading its code instead.
    environment = _EnvironmentReadRecorder(os.environ)
    monkeypatch.setattr(os, "environ", environment)

    with caplog.at_level(logging.WARNING, logger=warm_up_module.__name__):
        warm_up_module.warm_up_first_calculation()

    assert refusals == []
    assert caplog.records == []
    assert len(calculation.requests) == 1
    # Only the snapshot flag Modal sets and the image's data folder.
    assert environment.names_read <= {
        "MODAL_ENABLE_SNAP_RESTORE",
        "POLICYENGINE_DATA_FOLDER",
    }
    assert "GOOGLE_APPLICATION_CREDENTIALS" not in environment.environment


@pytest.mark.usefixtures("snapshotted_container")
def test_the_warm_up_sends_no_telemetry_and_the_first_call_is_still_number_1(
    modal_app, calculation, monkeypatch, caplog
):
    # Recorded as well as raised, since the warm-up turns an exception into
    # a warning.
    refusals: list[str] = []

    def refuse_observability(*_args, **_kwargs):
        # A runtime configured before the snapshot would be shared by every
        # restored container, and would export the warm-up's records.
        refusals.append("observability configured")
        msg = "the warm-up must not configure observability"
        raise AssertionError(msg)

    monkeypatch.setattr(
        modal_app, "_configure_modal_observability", refuse_observability
    )
    monkeypatch.setattr(modal_app, "init_process_observability", refuse_observability)
    monkeypatch.setattr(ObservabilityRuntime, "__init__", refuse_observability)

    worker = modal_app.SimulationSegmentWorker()
    with caplog.at_level(logging.WARNING, logger="src.modal.snapshot_warm_up"):
        worker.load_country_models()

    assert refusals == []
    assert caplog.records == []
    [warm_up_runtime] = calculation.runtimes
    assert not isinstance(warm_up_runtime, ObservabilityRuntime)

    call_attributes: dict[str, object] = {}

    class RecordingRuntime(_CallRuntime):
        def set_context(self, **attributes):
            call_attributes.update(attributes)

    monkeypatch.setattr(
        modal_app,
        "_configure_modal_observability",
        _configured_by(RecordingRuntime),
    )
    monkeypatch.setattr(
        simulation_runtime,
        "run_simulation_impl",
        lambda _params, **_options: {"ok": True},
    )

    assert worker.run_simulation_segment(dict(LATER_REQUEST)) == {"ok": True}
    assert call_attributes["container_start"] == "took_snapshot"
    assert call_attributes["container_call_number"] == 1


@pytest.mark.usefixtures("snapshotted_container")
def test_a_failed_warm_up_only_logs_a_warning(
    warm_up_module, calculation, monkeypatch, caplog
):
    files_before = {path.name for path in calculation.data_folder.iterdir()}

    def failing_calculation(params, **options):
        calculation(params, **options)
        msg = "the reform parameter is not in this model version"
        raise ValueError(msg)

    monkeypatch.setattr(
        simulation_runtime, "_run_simulation_impl_core", failing_calculation
    )

    with caplog.at_level(logging.WARNING, logger=warm_up_module.__name__):
        warm_up_module.warm_up_first_calculation()

    assert "the reform parameter is not in this model version" in caplog.text
    # What the failed calculation left is removed too.
    assert len(calculation.cache) == 0
    assert {path.name for path in calculation.data_folder.iterdir()} == files_before


@pytest.mark.usefixtures("snapshotted_container", "image_data_folder")
def test_a_failure_before_the_calculation_only_logs_a_warning(
    warm_up_module, calculation, monkeypatch, caplog
):
    def unreadable_identity(_year, _group):
        msg = "the bundle receipt is unreadable"
        raise OSError(msg)

    monkeypatch.setattr(precompute_planning, "cohort_identity", unreadable_identity)

    with caplog.at_level(logging.WARNING, logger=warm_up_module.__name__):
        warm_up_module.warm_up_first_calculation()

    assert calculation.requests == []
    assert "the bundle receipt is unreadable" in caplog.text


@pytest.mark.usefixtures("snapshotted_container")
def test_a_failed_warm_up_never_stops_the_container_starting(
    modal_app, calculation, monkeypatch
):
    attempts: list[str] = []

    def failing_calculation(_params, **_options):
        attempts.append("calculated")
        msg = "out of memory"
        raise MemoryError(msg)

    monkeypatch.setattr(
        simulation_runtime, "_run_simulation_impl_core", failing_calculation
    )
    monkeypatch.setattr(
        modal_app,
        "_configure_modal_observability",
        _configured_by(_CallRuntime),
    )
    monkeypatch.setattr(
        segmented_national,
        "dispatch_run_simulation",
        lambda _params, **_options: {"ok": True},
    )

    worker = modal_app.SimulationWorker()
    worker.load_country_models()

    assert attempts == ["calculated"]
    assert worker.run_simulation({"country": "us"}) == {"ok": True}


@pytest.mark.usefixtures("snapshotted_container")
def test_a_call_after_the_warm_up_finds_no_simulation_or_file_the_warm_up_added(
    warm_up_module, calculation, simulation_cache, image_data_folder
):
    files_before = {path.name for path in image_data_folder.iterdir()}
    without_warm_up = calculation(
        dict(LATER_REQUEST), runtime=NoOpObservabilityRuntime()
    )
    _restore_initial_state(simulation_cache, image_data_folder, files_before)

    warm_up_module.warm_up_first_calculation()
    after_warm_up = calculation(dict(LATER_REQUEST), runtime=NoOpObservabilityRuntime())

    # The call finds no simulation the warm-up cached and no file it saved,
    # so it loads the baked baseline from the image as it would without one.
    assert len(calculation.requests) == 3
    assert after_warm_up == without_warm_up


@pytest.mark.usefixtures("snapshotted_container")
def test_the_warm_up_warns_when_it_saved_over_a_baked_file(
    warm_up_module, calculation, image_data_folder, monkeypatch, caplog
):
    # .py calculates a baked baseline again and saves it over the image's
    # file when it finds it incomplete or out of date. The rewritten file goes
    # into the snapshot, so restored containers load it and report a hit.
    baked_baseline = image_data_folder / f"{BAKED_BASELINE_STORAGE_ID}.h5"

    def recalculating_calculation(params, **options):
        result = calculation(params, **options)
        baked_baseline.write_bytes(b"baseline calculated again by the warm-up")
        return result

    monkeypatch.setattr(
        simulation_runtime, "_run_simulation_impl_core", recalculating_calculation
    )

    with caplog.at_level(logging.WARNING, logger=warm_up_module.__name__):
        warm_up_module.warm_up_first_calculation()

    assert len(calculation.requests) == 1
    [warning] = caplog.records
    assert baked_baseline.name in warning.getMessage()


@pytest.mark.usefixtures("image_data_folder")
def test_a_container_modal_takes_no_snapshot_from_skips_the_warm_up(
    warm_up_module, calculation, monkeypatch
):
    # Modal runs the snapshot hook in every container that isn't restored,
    # but takes a snapshot only where its worker set this flag. Elsewhere the
    # warm-up would delay the first call by more than it saves.
    monkeypatch.delenv("MODAL_ENABLE_SNAP_RESTORE", raising=False)
    for module_name in US_MODEL_MODULES:
        monkeypatch.setitem(
            sys.modules,
            module_name,
            sys.modules.get(module_name) or ModuleType(module_name),
        )

    warm_up_module.warm_up_first_calculation()

    assert calculation.requests == []


@pytest.mark.usefixtures("image_data_folder")
def test_the_warm_up_skips_with_a_warning_when_the_us_models_were_not_preloaded(
    warm_up_module, calculation, monkeypatch, caplog
):
    # Importing them again could fetch the data release manifest again.
    monkeypatch.setenv("MODAL_ENABLE_SNAP_RESTORE", "1")
    for module_name in US_MODEL_MODULES:
        monkeypatch.delitem(sys.modules, module_name, raising=False)

    with caplog.at_level(logging.WARNING, logger=warm_up_module.__name__):
        warm_up_module.warm_up_first_calculation()

    assert calculation.requests == []
    assert "Warm-up calculation skipped" in caplog.text
    assert "policyengine.tax_benefit_models.us" in caplog.text


def test_the_warm_up_reform_changes_a_parameter_of_the_installed_us_model(
    warm_up_module,
):
    # policyengine-us's own parameter tree, about 12 s to load; .py's US model
    # would also read the data release manifest from Hugging Face.
    from policyengine_us.system import system

    [(parameter_name, values_by_period)] = warm_up_module.warm_up_request()[
        "reform"
    ].items()
    [(period, reform_value)] = values_by_period.items()
    period_start, _, _period_end = period.partition(".")

    assert period_start == f"{precompute_planning.PRECOMPUTE_YEARS[0]}-01-01"
    # A reform equal to current law would leave nothing to calculate.
    assert system.parameters.get_child(parameter_name)(period_start) != reform_value
