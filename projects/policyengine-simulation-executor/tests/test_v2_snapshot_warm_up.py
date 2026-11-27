"""The warm-up calculation the v2 US workers run before their memory snapshot.

A process's first calculation for a year does one-time work that later
calculations reuse. The v2 US workers' snapshot hook calculates one segment
of a region group whose current-law baseline the v2 cache precomputes, for
the first precompute year, under a small reform, so containers restored from
the snapshot start with that work done. These tests replace the segment
calculation with a fake that touches .py's in-process simulation cache and
the prebuilt folder as a real one does, and replace the planning and the
region, dataset and prebuilt-folder lookups, which need the US model. One
test runs the real v2 calculation up to its dataset load.
"""

from __future__ import annotations

import gc
import importlib
import logging
import os
import socket
import sys
import weakref
from types import ModuleType, SimpleNamespace
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from conftest import NoOpObservabilityRuntime
from environment_reads import SECRET_ENVIRONMENT_NAMES, EnvironmentReadRecorder
from fixtures.fake_modal import install_fake_modal
from policyengine_observability.runtime import ObservabilityRuntime
from policyengine_simulation_contract.stage12_execution import (
    EntityOutputPlan,
    PlannedSimulationExecutionInput,
    ReportAggregate,
    ReportOutputRequirements,
    SimulationRole,
    Stage12OutputPlan,
)
from policyengine_simulation_executor import simulation_runtime, stage12_runtime
from policyengine_simulation_executor.stage12_cache import levels
from policyengine_simulation_executor.stage12_cache import (
    precompute_planning as v2_precompute_planning,
)
from policyengine_simulation_executor.stage12_cache.cache_levels import (
    cache_baseline_levels,
)
from policyengine_simulation_executor.stage12_cache.partition import US_REGION_GROUPS
from policyengine_simulation_executor.stage12_runtime import output_planning
from policyengine_simulation_executor.stage12_runtime.segment_contracts import (
    Stage12SegmentInput,
)
from stage12_runtime_support import bundle_provenance, planned_simulation

if TYPE_CHECKING:
    from pathlib import Path

    from policyengine.core.cache import LRUCache

# The default dataset's prebuilt folder and its 2026 year file, as the
# precompute names them.
PREBUILT_FOLDER = "prebuilt/us/populace_us_2024/all"
PREBUILT_YEAR_FILENAME = "populace_us_2024_year_2026.h5"
SNAPSHOT_TASK_ID = "ta-01SNAPSHOTTING"
US_MODEL_MODULES = ("policyengine_us", "policyengine.tax_benefit_models.us")
# The run_simulation app's own modules, which the v2 app must not need: that
# app is to be removed while the v2 app stays.
RUN_SIMULATION_APP_MODULES = (
    "src.modal.app",
    "src.modal.snapshot_warm_up",
    "src.modal.segmented_national",
    "src.modal.precompute_app",
    "src.modal._image_setup",
)
CREDENTIALS_JSON = '{"type": "service_account", "project_id": "warm-up-test"}'
# The plan the reform's stored plan resolves to here, unlike the template's.
REFORM_OUTPUT_PLAN = Stage12OutputPlan(
    country="us",
    requirements=ReportOutputRequirements(
        aggregates=tuple(ReportAggregate),
        include_cliff_impacts=False,
        labor_supply_response_active=False,
    ),
    entities=(
        EntityOutputPlan(
            entity="household",
            materialized_variables=(
                "household_id",
                "household_net_income",
                "household_tax",
            ),
        ),
        EntityOutputPlan(
            entity="person",
            materialized_variables=("age", "household_id", "person_id"),
        ),
    ),
)
# A segment the container calculates after its snapshot hook: another region
# group's reform, as a segmented national run's child receives it.
LATER_SEGMENT = {"simulation": {"role": "reform"}, "segment_index": 3}


class _SegmentResult:
    """Stands in for a segment's artifact descriptor."""

    def __init__(self, result: dict) -> None:
        self.result = result

    def model_dump(self, *, mode: str) -> dict:
        assert mode == "python"
        return self.result


class _PolicyEngineLikeSegmentCalculation:
    """Stands in for ``calculate_segment``, leaving what a real calculation leaves.

    A real calculation sets up Google Cloud credentials first. Its reform's
    ``ensure()`` then adds the reform to .py's in-process cache under a new
    random ID, and saves its output beside the year file, in the prebuilt
    folder. Each call returns what it found in the cache and that folder
    before it calculated, which a real call's result depends on.
    """

    def __init__(self, cache: LRUCache, output_folder: Path) -> None:
        self.cache = cache
        self.output_folder = output_folder
        self.payloads: list[dict] = []
        self.runtimes: list[object] = []
        self.credential_paths: list[str | None] = []

    def __call__(self, payload: dict, *, runtime=None) -> _SegmentResult:
        self.payloads.append(payload)
        self.runtimes.append(runtime)
        with simulation_runtime.setup_gcp_credentials():
            self.credential_paths.append(
                os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
            )
        state_seen = {
            "cached_simulations": sorted(self.cache._cache),
            "output_folder_files": sorted(
                path.name for path in self.output_folder.iterdir()
            ),
        }
        reform_storage_id = str(uuid4())
        self.cache.add(reform_storage_id, SimpleNamespace(role="reform"))
        (self.output_folder / f"{reform_storage_id}.h5").write_bytes(b"reform output")
        return _SegmentResult({"state_seen": state_seen})


class _RecordingRuntime(NoOpObservabilityRuntime):
    """A worker call's runtime that keeps the attributes set on its operation."""

    def __init__(self) -> None:
        self.attributes: dict[str, object] = {}

    def set_context(self, **attributes: object) -> None:
        self.attributes.update(attributes)

    def shutdown(self) -> None:
        return None


class _StoppedAfterDatasetLoadError(Exception):
    """Raised by a stubbed dataset load, to stop a real calculation there."""


@pytest.fixture
def warm_up_module():
    return importlib.import_module("src.modal.v2_snapshot_warm_up")


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
def planning(monkeypatch):
    """Plan the warm-up's segment without the US model, recording each step.

    The template is the precompute's planned reform side for the group, as
    ``scoped_planned_simulation`` returns it: no policy, and the group in its
    options. Returns what was planned.
    """
    planned = SimpleNamespace(templates=[], resolved_for=[])

    def scoped_planned_simulation(year, group, *, reform=False, country="us"):
        planned.templates.append(
            {"year": year, "group": tuple(group), "reform": reform, "country": country}
        )
        role = SimulationRole.REFORM if reform else SimulationRole.BASELINE
        return PlannedSimulationExecutionInput.model_validate(
            {
                **planned_simulation(role).model_dump(mode="json"),
                "policy": {},
                "year": year,
                "options": {"region_group": list(group)},
            }
        )

    def resolve_simulation_output_plan(simulation, **_options):
        planned.resolved_for.append(simulation)
        return REFORM_OUTPUT_PLAN

    monkeypatch.setattr(
        v2_precompute_planning, "scoped_planned_simulation", scoped_planned_simulation
    )
    monkeypatch.setattr(
        output_planning,
        "resolve_simulation_output_plan",
        resolve_simulation_output_plan,
    )
    return planned


@pytest.fixture
def prebuilt_image(tmp_path, monkeypatch):
    """Create the image's prebuilt folder, and answer a run's lookups as in the image.

    The region and dataset resolution and ``levels.prebuilt_dataset_folder``,
    which the warm-up's check and the calculation both call, record what they
    are asked. The folder lookup answers as the real one does: the folder
    when it holds the year file, and None otherwise, which is also its answer
    for a year file .py wouldn't reuse.
    """
    data_folder = tmp_path / "data"
    prebuilt_folder = data_folder / PREBUILT_FOLDER
    prebuilt_folder.mkdir(parents=True)
    (prebuilt_folder / PREBUILT_YEAR_FILENAME).write_bytes(b"prebuilt year file")
    monkeypatch.setenv("POLICYENGINE_DATA_FOLDER", str(data_folder))
    group_region = SimpleNamespace(
        code="region_group/state/hi+state/ia+state/wi",
        scoping_strategy="group row filter",
        state_code=None,
        dataset_reference=None,
    )
    # The dataset the template's provenance names, so a real calculation's
    # provenance check passes.
    named_dataset = bundle_provenance().dataset
    default_dataset = simulation_runtime.DatasetSelection(
        named_dataset.identity, named_dataset.uri, is_default=True
    )
    image = SimpleNamespace(
        data_folder=data_folder,
        prebuilt_folder=prebuilt_folder,
        group_region=group_region,
        default_dataset=default_dataset,
        regions_resolved=[],
        dataset_selections=[],
        folder_lookups=[],
    )

    def resolve_region(*, country_module, country, params):
        image.regions_resolved.append((country, dict(params)))
        return group_region

    def resolve_dataset_selection(params, *, region_resolution):
        image.dataset_selections.append((dict(params), region_resolution))
        return default_dataset

    def prebuilt_folder_lookup(run, data_folder):
        image.folder_lookups.append((run, data_folder))
        if not (prebuilt_folder / PREBUILT_YEAR_FILENAME).is_file():
            return None
        return levels.PrebuiltFolder(folder=str(prebuilt_folder), dataset_source=None)

    monkeypatch.setattr(simulation_runtime, "_country_module", lambda country: country)
    monkeypatch.setattr(simulation_runtime, "resolve_region", resolve_region)
    monkeypatch.setattr(
        simulation_runtime, "_resolve_dataset_selection", resolve_dataset_selection
    )
    monkeypatch.setattr(levels, "prebuilt_dataset_folder", prebuilt_folder_lookup)
    return image


@pytest.fixture
def simulation_cache(monkeypatch):
    """Start .py's in-process simulation cache empty, as in a new container."""
    from policyengine.core import simulation as policyengine_simulation
    from policyengine.core.cache import LRUCache

    cache = LRUCache(max_size=100)
    monkeypatch.setattr(policyengine_simulation, "_cache", cache)
    return cache


@pytest.fixture
def calculation(monkeypatch, simulation_cache, prebuilt_image, planning):
    """Replace the segment calculation with a .py-like fake."""
    fake_calculation = _PolicyEngineLikeSegmentCalculation(
        simulation_cache, prebuilt_image.prebuilt_folder
    )
    monkeypatch.setattr(stage12_runtime, "calculate_segment", fake_calculation)
    return fake_calculation


@pytest.fixture
def credentials_in_environment(monkeypatch):
    """Give the container the Google Cloud credentials Secret, as Modal does."""
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS_JSON", CREDENTIALS_JSON)


@pytest.fixture
def v2_app(monkeypatch):
    """Import the v2 app afresh, as a new container does, without the real preload."""
    install_fake_modal(monkeypatch)
    sys.modules.pop("src.modal.v2_images", None)
    sys.modules.pop("src.modal.v2_app", None)
    app = importlib.import_module("src.modal.v2_app")
    monkeypatch.setattr(app, "preload_country_models", lambda _countries: None)
    monkeypatch.setattr(app.modal, "current_function_call_id", lambda: "fc-call")
    yield app
    sys.modules.pop("src.modal.v2_images", None)
    sys.modules.pop("src.modal.v2_app", None)


def _first_precompute_year() -> int:
    prebuilt = levels.prebuilt_for("us")
    assert prebuilt is not None
    return prebuilt.years[0]


def _first_precomputed_group() -> tuple[str, ...]:
    """Return the first region group whose baseline the v2 cache precomputes."""
    prebuilt = levels.prebuilt_for("us")
    assert prebuilt is not None
    [baseline_level] = cache_baseline_levels("us")
    [first_target, *_other_targets] = prebuilt.handlers[
        baseline_level
    ].baseline_targets(_first_precompute_year())
    return tuple(first_target.request["region_group"])


def _restore_initial_state(cache: LRUCache, folder: Path, files_before: set) -> None:
    """Put the cache and the folder back as they were when the container started."""
    cache.clear()
    for path in folder.iterdir():
        if path.name not in files_before:
            path.unlink()


def test_the_v2_snapshot_hook_runs_the_warm_up_after_the_preload_in_every_worker(
    v2_app, monkeypatch
):
    steps: list[tuple[str, tuple[str, ...]]] = []
    monkeypatch.setattr(
        v2_app,
        "preload_country_models",
        lambda countries: steps.append(("preload", tuple(countries))),
    )
    monkeypatch.setattr(
        v2_app,
        "warm_up_first_us_calculation",
        lambda countries: steps.append(("warm-up", tuple(countries))),
    )

    for worker_class_name in (
        "SimulationSegmentWorkerUS",
        "SimulationWorkerUS",
        "SimulationWorkerUK",
    ):
        worker = getattr(v2_app, worker_class_name)()
        worker.load_country_models()
        # Restored containers run only the snapshot=False hook.
        worker.reseed_random_after_restore()

    assert steps == [
        ("preload", ("us",)),
        ("warm-up", ("us",)),
        ("preload", ("us",)),
        ("warm-up", ("us",)),
        ("preload", ("uk",)),
        ("warm-up", ("uk",)),
    ]


@pytest.mark.usefixtures("snapshotted_container")
def test_only_the_us_workers_calculate_a_warm_up(warm_up_module, calculation):
    # The UK image holds no prebuilt UK year file yet, so a UK warm-up would
    # build its dataset before the snapshot.
    warm_up_module.warm_up_first_us_calculation(("uk",))

    assert calculation.payloads == []

    warm_up_module.warm_up_first_us_calculation(("us",))

    assert len(calculation.payloads) == 1


@pytest.mark.usefixtures("prebuilt_image")
@pytest.mark.parametrize("snapshot_flag", [None, "0", ""])
def test_a_container_modal_takes_no_snapshot_from_skips_the_warm_up(
    warm_up_module, calculation, monkeypatch, snapshot_flag
):
    # Modal runs the snapshot hook in every container that isn't restored,
    # but takes a snapshot only where its worker set this flag. Elsewhere the
    # warm-up would delay the first call by more than it saves.
    if snapshot_flag is None:
        monkeypatch.delenv("MODAL_ENABLE_SNAP_RESTORE", raising=False)
    else:
        monkeypatch.setenv("MODAL_ENABLE_SNAP_RESTORE", snapshot_flag)
    for module_name in US_MODEL_MODULES:
        monkeypatch.setitem(
            sys.modules,
            module_name,
            sys.modules.get(module_name) or ModuleType(module_name),
        )

    warm_up_module.warm_up_first_us_calculation(("us",))

    assert calculation.payloads == []


@pytest.mark.usefixtures("snapshotted_container")
def test_the_warm_up_calculates_one_reform_segment_of_the_first_precomputed_group(
    warm_up_module, calculation, planning, prebuilt_image, caplog
):
    with caplog.at_level(logging.WARNING, logger=warm_up_module.__name__):
        warm_up_module.warm_up_first_us_calculation(("us",))

    assert caplog.records == []
    [payload] = calculation.payloads
    segment = Stage12SegmentInput.model_validate(payload)
    first_year = _first_precompute_year()
    first_group = _first_precomputed_group()
    # A segmented national run's first child: the first of the 20 groups.
    assert first_group == US_REGION_GROUPS[0]
    assert segment.segment_index == 0
    assert segment.region_codes == first_group
    # The reform side, under a small reform, so it calculates rather than
    # loading a precomputed baseline.
    assert segment.simulation.role is SimulationRole.REFORM
    assert segment.simulation.year == first_year
    assert segment.simulation.policy == {
        warm_up_module.WARM_UP_REFORM_PARAMETER: {
            f"{first_year}-01-01.2100-12-31": warm_up_module.WARM_UP_REFORM_VALUE
        }
    }
    # Planned from the precompute's own reform side for the group, then
    # given the plan a report run's reform child with this policy stores.
    assert planning.templates == [
        {"year": first_year, "group": first_group, "reform": True, "country": "us"}
    ]
    [resolved_for] = planning.resolved_for
    assert resolved_for.role is SimulationRole.REFORM
    assert resolved_for.policy == segment.simulation.policy
    assert segment.simulation.output_plan == REFORM_OUTPUT_PLAN
    # Its year file is looked up as a run of the segment looks it up.
    [(country, params)] = prebuilt_image.regions_resolved
    assert country == "us"
    assert params["region_group"] == list(first_group)
    assert params["time_period"] == str(first_year)
    [(run, data_folder)] = prebuilt_image.folder_lookups
    assert (run.country, run.region, run.dataset, run.year) == (
        "us",
        prebuilt_image.group_region,
        prebuilt_image.default_dataset,
        first_year,
    )
    assert data_folder == str(prebuilt_image.data_folder)
    # No runtime, so no span or operation record.
    assert calculation.runtimes == [None]


@pytest.mark.usefixtures("snapshotted_container")
def test_the_warm_up_skips_with_a_warning_unless_the_image_holds_the_year_file(
    warm_up_module, calculation, prebuilt_image, caplog
):
    # Without a usable prebuilt year file the dataset would be built from its
    # source, which may download it.
    (prebuilt_image.prebuilt_folder / PREBUILT_YEAR_FILENAME).unlink()

    with caplog.at_level(logging.WARNING, logger=warm_up_module.__name__):
        warm_up_module.warm_up_first_us_calculation(("us",))

    assert calculation.payloads == []
    assert "Warm-up calculation skipped" in caplog.text
    assert "prebuilt year file" in caplog.text


@pytest.mark.usefixtures("prebuilt_image")
def test_the_warm_up_skips_with_a_warning_when_the_us_models_were_not_preloaded(
    warm_up_module, calculation, monkeypatch, caplog
):
    # Importing them again could fetch the data release manifest again.
    monkeypatch.setenv("MODAL_ENABLE_SNAP_RESTORE", "1")
    for module_name in US_MODEL_MODULES:
        monkeypatch.delitem(sys.modules, module_name, raising=False)

    with caplog.at_level(logging.WARNING, logger=warm_up_module.__name__):
        warm_up_module.warm_up_first_us_calculation(("us",))

    assert calculation.payloads == []
    assert "Warm-up calculation skipped" in caplog.text
    assert "policyengine.tax_benefit_models.us" in caplog.text


@pytest.mark.usefixtures(
    "snapshotted_container", "planning", "credentials_in_environment"
)
def test_the_real_v2_calculation_reads_no_secret_and_writes_no_credentials(
    warm_up_module, prebuilt_image, monkeypatch, caplog
):
    from policyengine_simulation_executor.stage12_runtime import (
        simulation as v2_simulation,
    )

    dataset_loads: list[dict[str, str | None]] = []

    def load_dataset(_params, *, selection, country_module, data_folder=None):
        dataset_loads.append(
            {
                "credentials_path": os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"),
                "data_folder": data_folder,
            }
        )
        raise _StoppedAfterDatasetLoadError

    connection_attempts: list[object] = []

    def refuse_connection(_socket, address):
        connection_attempts.append(address)
        msg = "nothing may connect before the snapshot"
        raise OSError(msg)

    monkeypatch.setattr(v2_simulation, "_require_installed_bundle", lambda _input: None)
    monkeypatch.setattr(simulation_runtime, "_load_dataset", load_dataset)
    monkeypatch.setattr(socket.socket, "connect", refuse_connection)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse_connection)
    environment = EnvironmentReadRecorder(os.environ)
    monkeypatch.setattr(os, "environ", environment)

    with caplog.at_level(logging.WARNING, logger=warm_up_module.__name__):
        warm_up_module.warm_up_first_us_calculation(("us",))

    # The real calculate_segment and calculate_simulation_frames ran up to
    # the dataset load, from the image's prebuilt folder, with no credential
    # file, no Secret read and no connection.
    assert dataset_loads == [
        {"credentials_path": None, "data_folder": str(prebuilt_image.prebuilt_folder)}
    ]
    assert environment.names_read.isdisjoint(SECRET_ENVIRONMENT_NAMES)
    assert "GOOGLE_APPLICATION_CREDENTIALS" not in environment.environment
    assert connection_attempts == []
    assert "Warm-up calculation failed" in caplog.text
    # The warm-up's check asked exactly what the calculation then asked, so
    # the folder it found is the one the calculation reads.
    warm_up_region, calculation_region = prebuilt_image.regions_resolved
    assert warm_up_region == calculation_region
    warm_up_selection, calculation_selection = prebuilt_image.dataset_selections
    assert warm_up_selection == calculation_selection
    warm_up_lookup, calculation_lookup = prebuilt_image.folder_lookups
    assert warm_up_lookup == calculation_lookup


@pytest.mark.usefixtures("snapshotted_container", "credentials_in_environment")
def test_the_warm_up_sets_up_no_credentials_and_calls_still_do(
    warm_up_module, calculation, monkeypatch
):
    def failing_calculation(payload, **options):
        calculation(payload, **options)
        msg = "the reform parameter is not in this model version"
        raise ValueError(msg)

    monkeypatch.setattr(stage12_runtime, "calculate_segment", failing_calculation)

    warm_up_module.warm_up_first_us_calculation(("us",))

    assert calculation.credential_paths == [None]
    # A worker call after the warm-up, even a failed one, writes them.
    with simulation_runtime.setup_gcp_credentials():
        assert os.environ.get("GOOGLE_APPLICATION_CREDENTIALS") is not None


@pytest.mark.usefixtures("snapshotted_container")
def test_the_warm_up_sends_no_telemetry_and_the_first_call_is_still_number_1(
    v2_app, calculation, monkeypatch, caplog
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

    monkeypatch.setattr(v2_app, "init_process_observability", refuse_observability)
    monkeypatch.setattr(ObservabilityRuntime, "__init__", refuse_observability)

    worker = v2_app.SimulationSegmentWorkerUS()
    with caplog.at_level(logging.WARNING, logger="src.modal.v2_snapshot_warm_up"):
        worker.load_country_models()

    assert refusals == []
    assert caplog.records == []
    assert calculation.runtimes == [None]

    call_runtime = _RecordingRuntime()
    monkeypatch.setattr(
        v2_app, "init_process_observability", lambda **_identity: call_runtime
    )

    worker.run_single_simulation_segment_us(dict(LATER_SEGMENT))

    assert call_runtime.attributes["container_start"] == "took_snapshot"
    assert call_runtime.attributes["container_call_number"] == 1


@pytest.mark.usefixtures("snapshotted_container")
def test_a_failed_warm_up_only_logs_a_warning_and_removes_what_it_left(
    warm_up_module, calculation, monkeypatch, caplog
):
    files_before = {path.name for path in calculation.output_folder.iterdir()}

    def failing_calculation(payload, **options):
        calculation(payload, **options)
        msg = "the reform parameter is not in this model version"
        raise ValueError(msg)

    monkeypatch.setattr(stage12_runtime, "calculate_segment", failing_calculation)

    with caplog.at_level(logging.WARNING, logger=warm_up_module.__name__):
        warm_up_module.warm_up_first_us_calculation(("us",))

    assert "the reform parameter is not in this model version" in caplog.text
    assert len(calculation.cache) == 0
    assert {path.name for path in calculation.output_folder.iterdir()} == files_before


@pytest.mark.usefixtures("snapshotted_container", "prebuilt_image")
def test_a_failure_before_the_calculation_only_logs_a_warning(
    warm_up_module, calculation, monkeypatch, caplog
):
    def unreadable_bundle(*_args, **_kwargs):
        msg = "the bundle receipt is unreadable"
        raise OSError(msg)

    monkeypatch.setattr(
        v2_precompute_planning, "scoped_planned_simulation", unreadable_bundle
    )

    with caplog.at_level(logging.WARNING, logger=warm_up_module.__name__):
        warm_up_module.warm_up_first_us_calculation(("us",))

    assert calculation.payloads == []
    assert "the bundle receipt is unreadable" in caplog.text


@pytest.mark.usefixtures("snapshotted_container")
def test_the_v2_app_imports_and_starts_when_the_warm_up_cannot_import_its_helpers(
    monkeypatch, caplog
):
    # A change elsewhere can move or rename what the warm-up imports. That
    # must cost only the warm-up, never the import of the app.
    install_fake_modal(monkeypatch)
    monkeypatch.delattr(v2_precompute_planning, "scoped_planned_simulation")
    for module_name in (
        "src.modal.v2_snapshot_warm_up",
        "src.modal.v2_images",
        "src.modal.v2_app",
    ):
        monkeypatch.delitem(sys.modules, module_name, raising=False)

    app = importlib.import_module("src.modal.v2_app")
    monkeypatch.setattr(app, "preload_country_models", lambda _countries: None)
    with caplog.at_level(logging.WARNING, logger="src.modal.v2_snapshot_warm_up"):
        app.SimulationSegmentWorkerUS().load_country_models()

    assert "Warm-up calculation failed" in caplog.text
    for module_name in ("src.modal.v2_images", "src.modal.v2_app"):
        sys.modules.pop(module_name, None)


@pytest.mark.usefixtures("snapshotted_container")
def test_a_failed_warm_up_never_stops_the_container_starting(
    v2_app, calculation, monkeypatch
):
    attempts: list[str] = []

    def failing_calculation(_payload, **_options):
        attempts.append("calculated")
        msg = "out of memory"
        raise MemoryError(msg)

    monkeypatch.setattr(stage12_runtime, "calculate_segment", failing_calculation)

    worker = v2_app.SimulationSegmentWorkerUS()
    worker.load_country_models()

    assert attempts == ["calculated"]
    monkeypatch.setattr(stage12_runtime, "calculate_segment", calculation)
    monkeypatch.setattr(
        v2_app,
        "init_process_observability",
        lambda **_identity: _RecordingRuntime(),
    )
    assert worker.run_single_simulation_segment_us(dict(LATER_SEGMENT)) == {
        "state_seen": {
            "cached_simulations": [],
            "output_folder_files": [PREBUILT_YEAR_FILENAME],
        }
    }


@pytest.mark.usefixtures("snapshotted_container")
def test_a_call_after_the_warm_up_finds_no_simulation_or_file_the_warm_up_added(
    warm_up_module, calculation, simulation_cache, prebuilt_image
):
    prebuilt_folder = prebuilt_image.prebuilt_folder
    files_before = {path.name for path in prebuilt_folder.iterdir()}
    without_warm_up = calculation(dict(LATER_SEGMENT)).result
    _restore_initial_state(simulation_cache, prebuilt_folder, files_before)

    warm_up_module.warm_up_first_us_calculation(("us",))
    after_warm_up = calculation(dict(LATER_SEGMENT)).result

    # The call finds no simulation the warm-up cached and no file it saved,
    # so it calculates from the image's files as it would without one.
    assert len(calculation.payloads) == 3
    assert after_warm_up == without_warm_up


@pytest.mark.usefixtures("snapshotted_container")
def test_the_v2_app_and_its_warm_up_need_no_module_of_the_run_simulation_app(
    calculation, monkeypatch
):
    install_fake_modal(monkeypatch)
    for run_simulation_module in RUN_SIMULATION_APP_MODULES:
        # Importing a module set to None raises ImportError.
        monkeypatch.setitem(sys.modules, run_simulation_module, None)
    for module_name in (
        "src.modal.v2_snapshot_warm_up",
        "src.modal.v2_images",
        "src.modal.v2_app",
    ):
        monkeypatch.delitem(sys.modules, module_name, raising=False)

    app = importlib.import_module("src.modal.v2_app")
    monkeypatch.setattr(app, "preload_country_models", lambda _countries: None)
    app.SimulationSegmentWorkerUS().load_country_models()

    assert len(calculation.payloads) == 1
    for module_name in ("src.modal.v2_images", "src.modal.v2_app"):
        sys.modules.pop(module_name, None)


def test_the_warm_up_reform_changes_a_parameter_of_the_installed_us_model(
    warm_up_module,
):
    # policyengine-us's own parameter tree, about 12 s to load; .py's US model
    # would also read the data release manifest from Hugging Face.
    from policyengine_us.system import system

    period_start = f"{_first_precompute_year()}-01-01"
    current_law_value = system.parameters.get_child(
        warm_up_module.WARM_UP_REFORM_PARAMETER
    )(period_start)

    # A reform equal to current law would calculate current law again.
    assert current_law_value != warm_up_module.WARM_UP_REFORM_VALUE


class _CyclicCalculationObject:
    """Refers to itself, as policyengine-core's simulation objects do."""

    def __init__(self) -> None:
        self.itself = self


@pytest.mark.usefixtures("snapshotted_container")
def test_a_failed_warm_up_frees_its_calculation_before_the_snapshot(
    warm_up_module, calculation, monkeypatch
):
    # Left allocated, a failed calculation's objects would go into the
    # snapshot, and every restored container would start with them.
    calculation_objects: list[weakref.ref] = []
    warnings: list[str] = []

    def failing_calculation(_payload, **_options):
        microsimulation = _CyclicCalculationObject()
        calculation_objects.append(weakref.ref(microsimulation))
        msg = "the planned column household_tax is missing"
        raise ValueError(msg)

    def record_warning(message, *args, **_options):
        # Only the message: a log record keeps the traceback, and with it
        # the failed calculation's frames.
        warnings.append(message % args)

    monkeypatch.setattr(stage12_runtime, "calculate_segment", failing_calculation)
    monkeypatch.setattr(warm_up_module.logger, "warning", record_warning)
    # Only the warm-up's own collection may free the objects.
    automatic_collection_was_on = gc.isenabled()
    gc.disable()
    try:
        warm_up_module.warm_up_first_us_calculation(("us",))
    finally:
        if automatic_collection_was_on:
            gc.enable()

    [calculation_object] = calculation_objects
    assert calculation_object() is None
    [warning] = warnings
    assert "Warm-up calculation failed" in warning


@pytest.mark.usefixtures("snapshotted_container")
def test_the_warm_up_leaves_no_memory_warning_marked_as_given(
    warm_up_module, calculation, monkeypatch
):
    from policyengine.core import cache as policyengine_cache

    # .py's cache warns once per process when its memory passes 8, 16 and
    # 32 GB, and remembers each warning given.
    warnings_given: set[int] = set()
    monkeypatch.setattr(policyengine_cache, "_warned_thresholds", warnings_given)
    nine_gigabytes = SimpleNamespace(rss=9 * 1024**3)
    monkeypatch.setattr(
        policyengine_cache.psutil,
        "Process",
        lambda: SimpleNamespace(memory_info=lambda: nine_gigabytes),
    )

    warm_up_module.warm_up_first_us_calculation(("us",))

    # The warm-up's calculation passed 8 GB, but a restored container's own
    # calls must still get the warning when they pass it.
    assert len(calculation.payloads) == 1
    assert warnings_given == set()
