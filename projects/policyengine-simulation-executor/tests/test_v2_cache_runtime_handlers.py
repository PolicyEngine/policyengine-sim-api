"""The v2 report app's worker and precompute builders go through the level handlers.

At run time the worker takes each run's prebuilt folder from the handlers
(``levels.prebuilt_dataset_folder``) and its baseline's scope key from the
handler's ``baseline_scope``. A fixed cache ID still needs a current-law
baseline whose region group lists only states, and a level the v2 cache
precomputes baselines for (``cache_levels``). The precompute writes year
files through the handlers' ``write_year_files``.

The inputs are the fixed stand-ins in ``fixtures.v2_cache_us_inputs``.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from fixtures.v2_cache_us_inputs import (
    DEFAULT_DATASET,
    STATE_LEVEL_DATASET,
    enable_fixed_spm,
    install_v2_cache_us_inputs,
)
from policyengine_simulation_executor import simulation_runtime
from policyengine_simulation_executor.stage12_cache import (
    precompute,
    precompute_baselines,
    precompute_datasets,
)
from policyengine_simulation_executor.stage12_cache.precompute_planning import (
    scoped_planned_simulation,
)
from policyengine_simulation_executor.stage12_runtime import (
    simulation as stage12_simulation,
)
from policyengine_simulation_executor.stage12_runtime.partition import US_REGION_GROUPS
from policyengine_simulation_executor.stage12_runtime.worker_inputs import (
    simulation_runtime_params,
)

BUCKET = "bucket-runtime-handlers"
NATIONAL_FOLDER = f"prebuilt/us/{DEFAULT_DATASET}/all"
FIRST_GROUP = tuple(US_REGION_GROUPS[0])


@pytest.fixture
def us_inputs(monkeypatch, tmp_path):
    inputs = install_v2_cache_us_inputs(monkeypatch)
    enable_fixed_spm(inputs)
    inputs.data_folder = tmp_path / "data"
    inputs.data_folder.mkdir()
    monkeypatch.setenv("POLICYENGINE_DATA_FOLDER", str(inputs.data_folder))
    return inputs


class _RecordedCachedSimulation:
    kind = "cached"

    def __init__(self, **keyword_arguments):
        self.arguments = keyword_arguments

    def configure_stage12_cache(self, _output_plan):
        return None


def _recorded_plain_simulation(**keyword_arguments):
    return SimpleNamespace(kind="random", arguments=keyword_arguments)


@pytest.fixture
def recorded_build(us_inputs, monkeypatch):
    """Build a planned simulation as the worker does, recording what is built."""
    monkeypatch.setattr(
        stage12_simulation, "Stage12CachedSimulation", _RecordedCachedSimulation
    )
    monkeypatch.setattr(stage12_simulation, "Simulation", _recorded_plain_simulation)

    def build(simulation):
        params = simulation_runtime_params(simulation)
        country = simulation.geography.country
        country_module = simulation_runtime._country_module(country)
        region = simulation_runtime.resolve_region(
            country_module=country_module, country=country, params=params
        )
        selection = simulation_runtime._resolve_dataset_selection(
            params, region_resolution=region
        )
        return stage12_simulation.build_stage12_simulation(
            params,
            resolved=stage12_simulation.ResolvedSimulationInputs(
                country_module=country_module,
                region=region,
                dataset_selection=selection,
                dataset=object(),
            ),
            policy=None,
            execution=simulation,
        )

    return build


class TestFixedIdsThroughTheHandlers:
    """``qualifying_cache_identity`` asks the handlers for the level and scope."""

    def test_the_scope_key_comes_from_the_handler(self, recorded_build, monkeypatch):
        from policyengine_simulation_executor.stage12_cache import keys, levels_us
        from policyengine_simulation_executor.stage12_cache.levels import (
            BaselineScope,
        )
        from policyengine_simulation_executor.stage12_cache.precompute_planning import (
            baseline_identity,
        )

        _simulation, planned = baseline_identity(2026, FIRST_GROUP)
        monkeypatch.setattr(
            levels_us.UsStateGroupHandler,
            "baseline_scope",
            lambda _handler, run: BaselineScope(
                region=run.region.code, scope_key="scope-from-the-handler"
            ),
        )

        built = recorded_build(scoped_planned_simulation(2026, FIRST_GROUP))

        expected = keys.Stage12BaselineCacheIdentity(
            dataset=planned.dataset,
            region_codes=FIRST_GROUP,
            scope_key="scope-from-the-handler",
            output_plan_sha256=planned.output_plan_sha256,
            spm=planned.spm,
        )
        assert built.kind == "cached"
        assert built.arguments["id"] == expected.simulation_id

    def test_a_handler_without_a_scope_gives_a_random_id(
        self, recorded_build, monkeypatch
    ):
        from policyengine_simulation_executor.stage12_cache import levels_us

        monkeypatch.setattr(
            levels_us.UsStateGroupHandler, "baseline_scope", lambda _handler, _run: None
        )

        built = recorded_build(scoped_planned_simulation(2026, FIRST_GROUP))

        assert built.kind == "random"

    def test_a_failing_scope_lookup_gives_a_random_id(
        self, recorded_build, monkeypatch
    ):
        from policyengine_simulation_executor.stage12_cache import levels_us

        def fail(_handler, _run):
            msg = "scope lookup failed"
            raise RuntimeError(msg)

        monkeypatch.setattr(levels_us.UsStateGroupHandler, "baseline_scope", fail)

        built = recorded_build(scoped_planned_simulation(2026, FIRST_GROUP))

        assert built.kind == "random"

    def test_failing_handler_settings_give_a_random_id(
        self, recorded_build, monkeypatch
    ):
        from policyengine_simulation_executor.stage12_cache import levels_us

        def fail():
            msg = "the US handler settings failed to load"
            raise RuntimeError(msg)

        monkeypatch.setattr(levels_us, "us_prebuilt", fail)

        built = recorded_build(scoped_planned_simulation(2026, FIRST_GROUP))

        assert built.kind == "random"

    def test_a_level_the_cache_does_not_precompute_gives_a_random_id(
        self, recorded_build, monkeypatch
    ):
        from policyengine_simulation_executor.stage12_cache import cache_levels

        monkeypatch.setattr(cache_levels, "CACHE_BASELINE_LEVELS", {"us": ()})

        built = recorded_build(scoped_planned_simulation(2026, FIRST_GROUP))

        assert built.kind == "random"

    def test_a_cached_level_still_needs_a_states_only_group(
        self, recorded_build, monkeypatch
    ):
        from policyengine_simulation_contract.stage12_execution import (
            GeographySelection,
        )
        from policyengine_simulation_executor.stage12_cache import cache_levels
        from policyengine_simulation_executor.stage12_cache.levels import (
            DatasetLevel,
        )
        from policyengine_simulation_executor.stage12_cache.precompute_planning import (
            _planned_pair,
        )

        monkeypatch.setattr(
            cache_levels,
            "CACHE_BASELINE_LEVELS",
            {"us": (DatasetLevel.STATE_GROUP, DatasetLevel.NATIONAL)},
        )
        baseline, _reform = _planned_pair(2026)
        national = baseline.model_copy(
            update={"geography": GeographySelection(country="us", region="us")}
        )

        assert recorded_build(national).kind == "random"


class _StoppedAfterLoadingError(Exception):
    """Raised by the stubbed simulation build once the dataset has loaded."""


@pytest.fixture
def recorded_load(us_inputs, monkeypatch):
    """Run a calculation up to its dataset load; return the selection and folder."""
    from contextlib import nullcontext

    loads = []

    def load_dataset(_params, *, selection, country_module, data_folder=None):
        loads.append((selection.name, data_folder))
        return object()

    def stop_after_loading(*_args, **_kwargs):
        raise _StoppedAfterLoadingError

    monkeypatch.setattr(stage12_simulation, "_require_installed_bundle", lambda _: None)
    monkeypatch.setattr(simulation_runtime, "setup_gcp_credentials", nullcontext)
    monkeypatch.setattr(simulation_runtime, "_load_dataset", load_dataset)
    monkeypatch.setattr(
        stage12_simulation, "detect_uk_run_dataset_metadata", lambda *_a, **_k: None
    )
    monkeypatch.setattr(
        stage12_simulation, "build_stage12_simulation", stop_after_loading
    )

    def load(simulation):
        loads.clear()
        with pytest.raises(_StoppedAfterLoadingError):
            stage12_simulation.calculate_simulation_frames(simulation)
        (loaded,) = loads
        return loaded

    return load


def test_the_worker_takes_each_runs_folder_from_the_handlers(
    us_inputs, recorded_load, monkeypatch
):
    from policyengine_simulation_executor.stage12_cache import levels

    asked = []

    def prebuilt_dataset_folder(run, data_folder):
        asked.append((run.country, run.year, run.region.code, run.dataset.name))
        return levels.PrebuiltFolder(
            folder=f"{data_folder}/{NATIONAL_FOLDER}", dataset_source=None
        )

    monkeypatch.setattr(levels, "prebuilt_dataset_folder", prebuilt_dataset_folder)

    loaded = recorded_load(scoped_planned_simulation(2026, FIRST_GROUP))

    assert loaded == (
        DEFAULT_DATASET,
        f"{us_inputs.data_folder}/{NATIONAL_FOLDER}",
    )
    assert asked == [
        (
            "us",
            2026,
            "region_group/" + "+".join(sorted(FIRST_GROUP)),
            DEFAULT_DATASET,
        )
    ]


class TestBuildersThroughTheHandlers:
    """The precompute writes year files through the handlers, then uploads them."""

    def test_the_deleted_copies_are_gone(self):
        from policyengine_simulation_executor.stage12_cache import (
            precompute_planning,
            state_datasets,
        )

        for name in (
            "_checked_state_targets",
            "_registered_district_geoids",
            "_require_districts_within_their_states",
        ):
            assert not hasattr(precompute_datasets, name), name
        for name in ("StateDatasetTarget", "state_dataset_targets", "PRECOMPUTE_YEARS"):
            assert not hasattr(precompute_planning, name), name
        for name in (
            "baked_state_data_folder",
            "state_dataset_folder",
            "state_dataset_relative_folder",
        ):
            assert not hasattr(state_datasets, name), name
        assert callable(state_datasets.year_file_reuse_problem)

    def test_the_state_build_writes_through_the_state_handler(
        self, us_inputs, monkeypatch, tmp_path
    ):
        from policyengine_simulation_executor.stage12_cache import levels_us
        from policyengine_simulation_executor.stage12_cache.levels import (
            year_file_name,
        )

        plan = precompute.plan_artifacts_impl(BUCKET)
        entries = [
            entry
            for entry in plan.state_datasets
            if entry.year == 2026 and entry.state_code in {"UT", "WY"}
        ]
        writes = []

        def write_year_files(_handler, year, targets, work_folder):
            writes.append((year, [target.subset.code for target in targets]))
            written = {}
            for target in targets:
                path = Path(work_folder) / target.folder / year_file_name(target)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(f"rows of {target.subset.code}".encode())
                written[target] = path
            return written

        monkeypatch.setattr(
            levels_us.UsStateHandler, "write_year_files", write_year_files
        )

        results = precompute.build_state_datasets_impl(
            BUCKET, 2026, entries, work_folder=tmp_path / "work"
        )

        assert writes == [(2026, ["UT", "WY"])]
        assert [result.state_code for result in results] == ["UT", "WY"]
        assert us_inputs.cache_bucket.objects[entries[0].path] == b"rows of UT"
        assert us_inputs.cache_bucket.objects[entries[1].path] == b"rows of WY"

    def test_the_national_build_leaves_no_work_folder_behind(
        self, us_inputs, monkeypatch
    ):
        from policyengine_simulation_executor.stage12_cache import levels_us
        from policyengine_simulation_executor.stage12_cache.levels import (
            year_file_name,
        )

        plan = precompute.plan_artifacts_impl(BUCKET)
        work_folders = []

        def write_year_files(_handler, _year, targets, work_folder):
            work_folders.append(Path(work_folder))
            (target,) = targets
            path = us_inputs.data_folder / year_file_name(target)
            path.write_bytes(b"national year file")
            return {target: path}

        monkeypatch.setattr(
            levels_us.UsNationalHandler, "write_year_files", write_year_files
        )

        precompute.build_dataset_impl(BUCKET, plan.datasets[0])

        (work_folder,) = work_folders
        assert not work_folder.exists()

    def test_the_national_build_writes_through_the_national_handler(
        self, us_inputs, monkeypatch
    ):
        from policyengine_simulation_executor.stage12_cache import levels_us
        from policyengine_simulation_executor.stage12_cache.levels import (
            year_file_name,
        )

        plan = precompute.plan_artifacts_impl(BUCKET)
        entry = plan.datasets[0]
        writes = []

        def write_year_files(_handler, year, targets, _work_folder):
            writes.append((year, [target.folder for target in targets]))
            (target,) = targets
            path = us_inputs.data_folder / year_file_name(target)
            path.write_bytes(b"national year file")
            return {target: path}

        monkeypatch.setattr(
            levels_us.UsNationalHandler, "write_year_files", write_year_files
        )

        result = precompute.build_dataset_impl(BUCKET, entry)

        assert writes == [(2026, [NATIONAL_FOLDER])]
        assert result.uploaded is True
        assert us_inputs.cache_bucket.objects[entry.path] == b"national year file"

    def test_a_baseline_is_computed_beside_its_year_file_in_the_prebuilt_folder(
        self, us_inputs, monkeypatch
    ):
        plan = precompute.plan_artifacts_impl(BUCKET)
        year_file = plan.datasets[0]
        baseline = plan.baselines[0]
        us_inputs.cache_bucket.store(year_file.path, b"national year file")
        folder = us_inputs.data_folder / NATIONAL_FOLDER
        seen = []

        def calculate(simulation):
            seen.append(sorted(path.name for path in folder.glob("*.h5")))
            (folder / baseline.filename).write_bytes(b"baseline output")
            return SimpleNamespace(frames={}, cache_outcome="miss")

        monkeypatch.setattr(
            precompute_baselines.stage12_simulation,
            "calculate_simulation_frames",
            calculate,
        )

        result = precompute.compute_baseline_impl(BUCKET, baseline)

        assert seen == [[year_file.filename]]
        assert (folder / year_file.filename).read_bytes() == b"national year file"
        assert result.uploaded is True
        assert us_inputs.cache_bucket.objects[baseline.path] == b"baseline output"
        assert not list(us_inputs.data_folder.glob("*.h5"))

    def test_the_baseline_calculation_reads_the_downloaded_prebuilt_year_file(
        self, us_inputs, monkeypatch, tmp_path
    ):
        """The real prebuilt-folder lookup, during the precompute, finds the file."""
        from contextlib import nullcontext

        from fixtures.state_dataset_sources import write_year_file

        plan = precompute.plan_artifacts_impl(BUCKET)
        year_file = plan.datasets[0]
        baseline = plan.baselines[0]
        stamped = write_year_file(tmp_path, year=2026, dataset=DEFAULT_DATASET)
        us_inputs.cache_bucket.store(year_file.path, stamped.read_bytes())
        loads = []

        def load_dataset(_params, *, selection, country_module, data_folder=None):
            loads.append(data_folder)
            raise _StoppedAfterLoadingError

        monkeypatch.setattr(simulation_runtime, "setup_gcp_credentials", nullcontext)
        monkeypatch.setattr(simulation_runtime, "_load_dataset", load_dataset)

        with pytest.raises(_StoppedAfterLoadingError):
            precompute.compute_baseline_impl(BUCKET, baseline)

        assert loads == [str(us_inputs.data_folder / baseline.folder)]

    def test_the_state_folder_of_a_plan_entry_is_the_handlers(self, us_inputs):
        plan = precompute.plan_artifacts_impl(BUCKET)
        utah = next(
            entry
            for entry in plan.state_datasets
            if entry.year == 2026 and entry.state_code == "UT"
        )

        assert utah.folder == f"prebuilt/us/{STATE_LEVEL_DATASET}/UT"
