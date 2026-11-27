"""The warm-up calculation the simulation workers run before their memory snapshot.

A process's first calculation for a year does one-time work that later
calculations reuse. The workers' snapshot hook runs one calculation of a
region group whose year file and baseline are prebuilt into the image, so
containers restored from the snapshot start with that work done. These tests
replace the calculation itself with a fake that touches .py's in-process
simulation cache and the prebuilt folder as a real one does, and replace the
region, dataset and prebuilt-folder lookups, which need the US model.
"""

from __future__ import annotations

import importlib
import logging
import os
import sys
from types import ModuleType, SimpleNamespace
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from conftest import NoOpObservabilityRuntime
from environment_reads import SECRET_ENVIRONMENT_NAMES, EnvironmentReadRecorder
from fixtures.fake_modal import install_fake_modal
from policyengine_observability.runtime import ObservabilityRuntime
from policyengine_simulation_executor import precompute_planning, simulation_runtime
from policyengine_simulation_executor.national_partition import national_region_groups
from policyengine_simulation_executor.stage12_cache import levels
from src.modal import segmented_national

if TYPE_CHECKING:
    from pathlib import Path

    from policyengine.core.cache import LRUCache

# The default dataset's prebuilt folder, its 2026 year file, and the warm-up
# group's baked baseline, as the precompute names them.
PREBUILT_FOLDER = "prebuilt/us/populace_us_2024/all"
PREBUILT_YEAR_FILENAME = "populace_us_2024_year_2026.h5"
BAKED_BASELINE_STORAGE_ID = "bl3-warmupgroupbaseline"
SNAPSHOT_TASK_ID = "ta-01SNAPSHOTTING"
# A request the container serves after its snapshot hook: another region
# group, under another reform, as in the measurements on Modal `testing`.
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
    cache under the baseline's fixed storage ID, adds its reform under a new
    random ID, and saves the reform's output beside the year file, in the
    prebuilt folder. Each call returns what it found in the cache and that
    folder before it calculated, which a real call's result depends on: a
    cached baseline is used instead of the baked file.
    """

    def __init__(self, cache: LRUCache, output_folder: Path) -> None:
        self.cache = cache
        self.output_folder = output_folder
        self.requests: list[dict] = []
        self.runtimes: list[object] = []

    def __call__(self, params: dict, *, runtime, progress=None) -> dict:
        self.requests.append(params)
        self.runtimes.append(runtime)
        with runtime.span("calculation"):
            runtime.set_context(region="+".join(params.get("region_group", [])))
        state_seen = {
            "cached_simulations": sorted(self.cache._cache),
            "output_folder_files": sorted(
                path.name for path in self.output_folder.iterdir()
            ),
        }
        reform_storage_id = str(uuid4())
        self.cache.add(BAKED_BASELINE_STORAGE_ID, SimpleNamespace(role="baseline"))
        self.cache.add(reform_storage_id, SimpleNamespace(role="reform"))
        (self.output_folder / f"{reform_storage_id}.h5").write_bytes(b"reform output")
        return {
            "state_seen": state_seen,
            "region_group": params.get("region_group"),
            "reform": params.get("reform"),
        }


class _CallRuntime(NoOpObservabilityRuntime):
    """A worker call's runtime that records and sends nothing."""

    def shutdown(self) -> None:
        return None


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
    """Answer the precompute's baseline identity, recording each target asked about."""
    requested: list[levels.BaselineTarget] = []

    def baked_identity(target):
        requested.append(target)
        return SimpleNamespace(storage_id=BAKED_BASELINE_STORAGE_ID)

    monkeypatch.setattr(precompute_planning, "baseline_target_identity", baked_identity)
    return requested


@pytest.fixture
def prebuilt_image(tmp_path, monkeypatch, requested_identities):
    """Create the image's prebuilt folder, and answer a run's lookups as in the image.

    The prebuilt-folder lookup answers as ``levels.prebuilt_dataset_folder``
    does: the folder when it holds the year file, and None otherwise, which is
    also its answer for a year file .py wouldn't reuse.
    """
    data_folder = tmp_path / "data"
    prebuilt_folder = data_folder / PREBUILT_FOLDER
    prebuilt_folder.mkdir(parents=True)
    (prebuilt_folder / PREBUILT_YEAR_FILENAME).write_bytes(b"prebuilt year file")
    (prebuilt_folder / f"{BAKED_BASELINE_STORAGE_ID}.h5").write_bytes(b"baseline")
    monkeypatch.setenv("POLICYENGINE_DATA_FOLDER", str(data_folder))
    group_region = SimpleNamespace(
        code="region_group/state/hi+state/ia+state/wi",
        scoping_strategy="group row filter",
        state_code=None,
    )
    default_dataset = simulation_runtime.DatasetSelection(
        "populace_us_2024", "hf://policyengine/populace_us_2024.h5", is_default=True
    )
    image = SimpleNamespace(
        prebuilt_folder=prebuilt_folder,
        group_region=group_region,
        default_dataset=default_dataset,
        regions_resolved=[],
        folder_lookups=[],
    )

    def resolve_region(*, country_module, country, params):
        image.regions_resolved.append((country, dict(params)))
        return group_region

    def prebuilt_folder_lookup(country, region, selection, year):
        image.folder_lookups.append((country, region, selection, year))
        if not (prebuilt_folder / PREBUILT_YEAR_FILENAME).is_file():
            return None
        return levels.PrebuiltFolder(folder=str(prebuilt_folder), dataset_source=None)

    monkeypatch.setattr(simulation_runtime, "_country_module", lambda country: country)
    monkeypatch.setattr(simulation_runtime, "resolve_region", resolve_region)
    monkeypatch.setattr(
        simulation_runtime,
        "_resolve_dataset_selection",
        lambda _params, *, region_resolution: default_dataset,
    )
    monkeypatch.setattr(simulation_runtime, "_prebuilt_folder", prebuilt_folder_lookup)
    return image


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
def calculation(monkeypatch, simulation_cache, prebuilt_image):
    """Replace the simulation path's calculation with a .py-like fake."""
    fake_calculation = _PolicyEngineLikeCalculation(
        simulation_cache, prebuilt_image.prebuilt_folder
    )
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


def _first_group_target() -> levels.BaselineTarget:
    """Return group 1's baseline target for the first prebuilt US year."""
    prebuilt = levels.prebuilt_for("us")
    assert prebuilt is not None
    group_handler = prebuilt.handlers[levels.DatasetLevel.STATE_GROUP]
    return group_handler.baseline_targets(prebuilt.years[0])[0]


def _restore_initial_state(cache: LRUCache, folder: Path, files_before: set) -> None:
    """Put the cache and the folder back as they were when the container started."""
    cache.clear()
    for path in folder.iterdir():
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
def test_the_warm_up_calculates_group_1_for_the_first_prebuilt_year(
    warm_up_module, calculation, requested_identities, prebuilt_image
):
    warm_up_module.warm_up_first_calculation()

    [request] = calculation.requests
    first_group_target = _first_group_target()
    # Group 1's precomputed current-law request, as a segmented national
    # run's child receives it, with a reform so the reform is calculated.
    assert {
        key: value
        for key, value in request.items()
        if key not in {"reform", "_emit_microdata"}
    } == dict(first_group_target.request)
    assert request["region_group"] == national_region_groups("us")[0]
    assert request["time_period"] == levels.prebuilt_for("us").years[0]
    assert request["_emit_microdata"] is True
    assert request["reform"]
    # Its baked inputs are looked up as a run of the request looks them up.
    assert requested_identities == [first_group_target]
    assert [params for _country, params in prebuilt_image.regions_resolved] == [request]
    assert prebuilt_image.folder_lookups == [
        (
            "us",
            prebuilt_image.group_region,
            prebuilt_image.default_dataset,
            first_group_target.year,
        )
    ]


@pytest.mark.usefixtures("snapshotted_container")
@pytest.mark.parametrize(
    "missing_input",
    [
        (PREBUILT_YEAR_FILENAME, "prebuilt year file"),
        (f"{BAKED_BASELINE_STORAGE_ID}.h5", f"{BAKED_BASELINE_STORAGE_ID}.h5"),
    ],
)
def test_the_warm_up_skips_with_a_warning_unless_the_image_holds_its_inputs(
    warm_up_module, calculation, prebuilt_image, missing_input, caplog
):
    # Without a usable prebuilt year file the dataset would be built from its
    # source, which may download it; without a baked baseline the baseline
    # would be calculated.
    missing_filename, expected_warning = missing_input
    (prebuilt_image.prebuilt_folder / missing_filename).unlink()

    with caplog.at_level(logging.WARNING, logger=warm_up_module.__name__):
        warm_up_module.warm_up_first_calculation()

    assert calculation.requests == []
    assert "Warm-up calculation skipped" in caplog.text
    assert expected_warning in caplog.text


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
    # Records the warm-up's own reads, the real group target's included. The
    # calculation and the lookups that need the model are replaced here, so
    # what they read is checked by reading their code instead.
    environment = EnvironmentReadRecorder(os.environ)
    monkeypatch.setattr(os, "environ", environment)

    with caplog.at_level(logging.WARNING, logger=warm_up_module.__name__):
        warm_up_module.warm_up_first_calculation()

    assert refusals == []
    assert caplog.records == []
    assert len(calculation.requests) == 1
    assert environment.names_read.isdisjoint(SECRET_ENVIRONMENT_NAMES)
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
    files_before = {path.name for path in calculation.output_folder.iterdir()}

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
    assert {path.name for path in calculation.output_folder.iterdir()} == files_before


@pytest.mark.usefixtures("snapshotted_container", "prebuilt_image")
def test_a_failure_before_the_calculation_only_logs_a_warning(
    warm_up_module, calculation, monkeypatch, caplog
):
    def unreadable_identity(_target):
        msg = "the bundle receipt is unreadable"
        raise OSError(msg)

    monkeypatch.setattr(
        precompute_planning, "baseline_target_identity", unreadable_identity
    )

    with caplog.at_level(logging.WARNING, logger=warm_up_module.__name__):
        warm_up_module.warm_up_first_calculation()

    assert calculation.requests == []
    assert "the bundle receipt is unreadable" in caplog.text


@pytest.mark.usefixtures("snapshotted_container")
def test_the_app_imports_and_starts_when_the_warm_up_cannot_import_its_modules(
    monkeypatch, caplog
):
    # A change elsewhere can move or rename what the warm-up imports, as the
    # move of the precompute onto the level handlers did. That must cost only
    # the warm-up, never the import of the app.
    install_fake_modal(monkeypatch)
    monkeypatch.setitem(
        sys.modules, "policyengine_simulation_executor.precompute_planning", None
    )
    for module_name in ("src.modal.snapshot_warm_up", "src.modal.app"):
        monkeypatch.delitem(sys.modules, module_name, raising=False)

    app = importlib.import_module("src.modal.app")
    monkeypatch.setattr(app, "preload_country_models", lambda: None)
    with caplog.at_level(logging.WARNING, logger="src.modal.snapshot_warm_up"):
        app.SimulationSegmentWorker().load_country_models()

    assert "Warm-up calculation failed" in caplog.text
    for module_name in ("src.modal.snapshot_warm_up", "src.modal.app"):
        sys.modules.pop(module_name, None)


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
    warm_up_module, calculation, simulation_cache, prebuilt_image
):
    prebuilt_folder = prebuilt_image.prebuilt_folder
    files_before = {path.name for path in prebuilt_folder.iterdir()}
    without_warm_up = calculation(
        dict(LATER_REQUEST), runtime=NoOpObservabilityRuntime()
    )
    _restore_initial_state(simulation_cache, prebuilt_folder, files_before)

    warm_up_module.warm_up_first_calculation()
    after_warm_up = calculation(dict(LATER_REQUEST), runtime=NoOpObservabilityRuntime())

    # The call finds no simulation the warm-up cached and no file it saved,
    # so it loads the baked baseline from the image as it would without one.
    assert len(calculation.requests) == 3
    assert after_warm_up == without_warm_up


@pytest.mark.usefixtures("snapshotted_container")
def test_the_warm_up_warns_when_it_saved_over_a_baked_file(
    warm_up_module, calculation, prebuilt_image, monkeypatch, caplog
):
    # The calculation runs a baked baseline again and saves it over the
    # image's file when it is incomplete, out of date or unreadable. The
    # rewritten file goes into the snapshot, so restored containers load it.
    baked_baseline = prebuilt_image.prebuilt_folder / f"{BAKED_BASELINE_STORAGE_ID}.h5"

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


@pytest.mark.usefixtures("prebuilt_image")
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


@pytest.mark.usefixtures("prebuilt_image")
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

    target = _first_group_target()
    [(parameter_name, values_by_period)] = warm_up_module.warm_up_request(target)[
        "reform"
    ].items()
    [(period, reform_value)] = values_by_period.items()
    period_start, _, _period_end = period.partition(".")

    assert period_start == f"{target.year}-01-01"
    # A reform equal to current law would leave nothing to calculate.
    assert system.parameters.get_child(parameter_name)(period_start) != reform_value
