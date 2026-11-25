"""Pins of how the v2 report app's US worker looks its cache files up at run time.

Every expected value below is what ``main`` produced on 2026-11-25 (commit
6865dc2c) for the fixed inputs in ``fixtures.v2_cache_us_inputs``. Two
lookups are pinned, each through the worker function that uses it, so the
pins hold however the lookup is implemented:

* which runs ``build_stage12_simulation`` gives a fixed cache identity (a
  ``Stage12CachedSimulation``, whose saved file the worker image may hold)
  and which a random one (a plain ``Simulation``). Moving the v2 cache onto
  the level handlers must leave every outcome unchanged; in particular a US
  national run without a region group, a single state, a district and a
  UK-wide run get no v2 cache identity, though the run_simulation app gives
  some of them one;
* which folder ``calculate_simulation_frames`` loads each run's dataset from
  (``TestDatasetFoldersOnMain``). The handler design moves the baked folders
  to ``prebuilt/…``, so those pins change in a commit of their own.
"""

from __future__ import annotations

from contextlib import nullcontext
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest
from fixtures.v2_cache_us_inputs import (
    DEFAULT_DATASET,
    STATE_LEVEL_DATASET,
    enable_fixed_spm,
    install_v2_cache_us_inputs,
)
from policyengine_simulation_contract.stage12_execution import (
    GeographySelection,
    SimulationRole,
)
from policyengine_simulation_executor import simulation_runtime
from policyengine_simulation_executor.stage12_cache import keys, precompute
from policyengine_simulation_executor.stage12_cache.precompute_planning import (
    _planned_pair,
    scoped_planned_simulation,
)
from policyengine_simulation_executor.stage12_runtime import (
    simulation as stage12_simulation,
)
from policyengine_simulation_executor.stage12_runtime.partition import US_REGION_GROUPS
from policyengine_simulation_executor.stage12_runtime.worker_inputs import (
    simulation_runtime_params,
)

if TYPE_CHECKING:
    from pathlib import Path

BUCKET = "bucket-pins"
FIXED_SPM_SELECTION = {
    "forecast_content_sha256": "a" * 64,
    "scenario": "ce_trend",
    "geography_kind": "national",
    "geography_id": None,
    "county_vintage": "2020",
    "as_of": None,
}
SPM_SUFFIX = "-spm-7396bf5f4876c42bb6cba0f9533478098edc0861cc3657bd0d60f88dbb26ac39"


class _RecordedCachedSimulation:
    """Stands in for ``Stage12CachedSimulation``: records what it was built with."""

    kind = "cached"

    def __init__(self, **keyword_arguments):
        self.arguments = keyword_arguments
        self.output_plan = None

    def configure_stage12_cache(self, output_plan):
        self.output_plan = output_plan


def _recorded_plain_simulation(**keyword_arguments):
    return SimpleNamespace(kind="random", arguments=keyword_arguments)


@pytest.fixture(scope="module")
def us_inputs():
    with pytest.MonkeyPatch.context() as module_patch:
        inputs = install_v2_cache_us_inputs(module_patch)
        enable_fixed_spm(inputs)
        yield inputs


@pytest.fixture(scope="module")
def us_plan(us_inputs):
    return precompute.plan_artifacts_impl(BUCKET)


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


def _baseline_for(region: str, year: int = 2026):
    """Plan the current-law US baseline for one region, with no region group."""
    baseline, _reform = _planned_pair(year)
    return baseline.model_copy(
        update={"geography": GeographySelection(country="us", region=region)}
    )


def _grouped_baseline(group: list[str], year: int = 2026):
    """Plan the current-law US baseline scoped to ``group``, a planned one or not."""
    return scoped_planned_simulation(year, tuple(group))


class TestCacheIdentities:
    """Which runs get a fixed v2 cache identity."""

    def test_every_planned_group_baseline_reads_its_planned_file(
        self, us_plan, recorded_build
    ):
        for entry in us_plan.baselines:
            built = recorded_build(
                scoped_planned_simulation(entry.year, tuple(entry.region_codes))
            )

            assert built.kind == "cached", entry.region_codes
            assert built.arguments["id"] == entry.simulation_id
            assert built.arguments["spm"] == FIXED_SPM_SELECTION
            # .py saves and loads the file as the ID plus the SPM digest.
            assert entry.filename == (
                f"{built.arguments['id']}"
                f"-spm-{keys.canonical_digest(built.arguments['spm'])}.h5"
            )
            assert built.output_plan is not None

    def test_a_state_group_outside_the_partition_gets_a_fixed_identity(
        self, recorded_build
    ):
        built = recorded_build(_grouped_baseline(["state/ca", "state/ny"]))

        assert built.kind == "cached"
        assert built.arguments["id"] == "stage12-bl1-c0445de8790068d7"

    def test_a_planned_group_in_a_year_without_files_gets_a_fixed_identity(
        self, recorded_build
    ):
        built = recorded_build(_grouped_baseline(list(US_REGION_GROUPS[0]), year=2028))

        assert built.kind == "cached"
        assert built.arguments["id"] == "stage12-bl1-10b3e998a57c0266"

    @pytest.mark.parametrize(
        "run",
        [
            pytest.param(
                lambda: scoped_planned_simulation(
                    2026, tuple(US_REGION_GROUPS[0]), reform=True
                ),
                id="reform-side-of-a-planned-group",
            ),
            pytest.param(
                lambda: scoped_planned_simulation(
                    2026, tuple(US_REGION_GROUPS[0])
                ).model_copy(update={"policy": {"gov.irs.credits.ctc.amount": 1}}),
                id="planned-group-with-a-policy",
            ),
            pytest.param(
                lambda: scoped_planned_simulation(
                    2026, tuple(US_REGION_GROUPS[0])
                ).model_copy(update={"role": SimulationRole.REFORM}),
                id="planned-group-in-the-reform-role",
            ),
            pytest.param(lambda: _baseline_for("us"), id="us-national-without-a-group"),
            pytest.param(lambda: _baseline_for("state/ca"), id="single-state"),
            pytest.param(
                lambda: _baseline_for("congressional_district/UT01"), id="district"
            ),
            pytest.param(
                lambda: _grouped_baseline(["state/ca", "congressional_district/UT01"]),
                id="group-with-a-district",
            ),
            pytest.param(
                lambda: _baseline_for("us").model_copy(
                    update={"geography": GeographySelection(country="uk", region="uk")}
                ),
                id="uk-wide",
            ),
        ],
    )
    def test_other_runs_get_a_random_identity(self, recorded_build, run):
        built = recorded_build(run())

        assert built.kind == "random"
        assert "id" not in built.arguments


class _StoppedAfterLoadingError(Exception):
    """Raised by the stubbed simulation build once the dataset has loaded."""


@pytest.fixture
def recorded_load(us_inputs, monkeypatch, tmp_path):
    """Run a calculation up to its dataset load; return the selection and folder."""
    data_folder = tmp_path / "data"
    data_folder.mkdir()
    monkeypatch.setenv("POLICYENGINE_DATA_FOLDER", str(data_folder))
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

    return SimpleNamespace(load=load, data_folder=data_folder)


def _bake_state_file(data_folder: Path, state_code: str, year: int, *, stamp=True):
    """Put a state's year file where ``main``'s worker image holds it."""
    from fixtures.state_dataset_sources import write_year_file

    folder = data_folder / "state-datasets" / STATE_LEVEL_DATASET / state_code
    folder.mkdir(parents=True, exist_ok=True)
    write_year_file(folder, year=year, stamp=stamp)
    return folder


class TestDatasetFoldersOnMain:
    """Which folder ``main``'s worker loads each run's dataset from.

    None means the loader's own default: the image data folder for the
    default dataset, where ``main`` bakes the national year files and
    baselines, and a temporary folder for any other. The handler design
    moves the baked folders to ``prebuilt/…``; these pins change in that
    commit, and only there.
    """

    def test_national_and_group_runs_load_the_default_dataset_unbaked(
        self, recorded_load
    ):
        _bake_state_file(recorded_load.data_folder, "CA", 2026)

        assert recorded_load.load(_baseline_for("us")) == (DEFAULT_DATASET, None)
        assert recorded_load.load(
            scoped_planned_simulation(2026, tuple(US_REGION_GROUPS[0]))
        ) == (DEFAULT_DATASET, None)

    def test_a_state_with_a_baked_file_loads_its_state_folder(self, recorded_load):
        folder = _bake_state_file(recorded_load.data_folder, "CA", 2026)

        assert recorded_load.load(_baseline_for("state/ca")) == (
            STATE_LEVEL_DATASET,
            str(folder),
        )
        assert folder == (
            recorded_load.data_folder
            / "state-datasets"
            / "populace_us_2024_acs_local"
            / "CA"
        )

    def test_a_district_loads_its_states_folder(self, recorded_load):
        utah = _bake_state_file(recorded_load.data_folder, "UT", 2026)
        california = _bake_state_file(recorded_load.data_folder, "CA", 2026)

        assert recorded_load.load(_baseline_for("congressional_district/UT01")) == (
            STATE_LEVEL_DATASET,
            str(utah),
        )
        assert recorded_load.load(_baseline_for("congressional_district/CA12")) == (
            STATE_LEVEL_DATASET,
            str(california),
        )

    def test_a_state_or_year_without_a_usable_baked_file_loads_on_demand(
        self, recorded_load
    ):
        _bake_state_file(recorded_load.data_folder, "CA", 2026)
        _bake_state_file(recorded_load.data_folder, "UT", 2026, stamp=False)

        assert recorded_load.load(_baseline_for("state/wy")) == (
            STATE_LEVEL_DATASET,
            None,
        )
        assert recorded_load.load(_baseline_for("state/ca", year=2028)) == (
            STATE_LEVEL_DATASET,
            None,
        )
        # .py would rebuild an unstamped year file, so it is not read.
        assert recorded_load.load(_baseline_for("state/ut")) == (
            STATE_LEVEL_DATASET,
            None,
        )
