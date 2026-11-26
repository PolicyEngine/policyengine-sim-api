"""The level handlers: which runs each covers, its targets, and what a run reads.

A handler takes a country and a dataset level and answers four questions
without building any key: which year files and baselines the precompute
builds for a year, which year file a run reads, which prebuilt folder holds
it, and whether the run's current-law baseline gets a fixed ID. The fixed
inputs come from ``fixtures.us_prebuilt_inputs``.
"""

from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace

import pytest
from fixtures.us_prebuilt_inputs import US_STATE_FIPS, install_us_prebuilt_inputs
from policyengine.core.scoping_strategy import RegionGroupStrategy, RowFilterStrategy
from policyengine_simulation_executor import national_partition
from policyengine_simulation_executor.region_resolution import RegionResolution
from policyengine_simulation_executor.simulation_runtime import DatasetSelection
from policyengine_simulation_executor.stage12_cache import (
    levels,
    levels_uk,
    levels_us,
    partition,
)
from policyengine_simulation_executor.stage12_cache.levels import (
    BaselineScope,
    DatasetLevel,
    RunScope,
    SubsetFilter,
    YearFileTarget,
)
from policyengine_simulation_executor.stage12_runtime.partition import (
    US_REGION_GROUPS,
)
from test_stage12_segmentation_foundation import FORBIDDEN_IMPORTS

US = "us"
UK = "uk"


@pytest.fixture
def inputs(monkeypatch):
    return install_us_prebuilt_inputs(monkeypatch)


@pytest.fixture
def default_dataset(inputs):
    return DatasetSelection(
        name="populace_cps",
        uri=inputs.us_bundle.dataset_uris["populace_cps"],
        is_default=True,
    )


@pytest.fixture
def state_level_dataset(inputs):
    return DatasetSelection(
        name="populace_cps_local",
        uri=inputs.us_bundle.dataset_uris["populace_cps_local"],
        is_default=False,
    )


def _state_filter(state_fips):
    return RowFilterStrategy(variable_name="state_fips", variable_value=state_fips)


def _run(country, region, dataset, *, year=2026):
    return RunScope(country=country, year=year, region=region, dataset=dataset)


def _national_target(year=2026):
    return YearFileTarget(
        country=US,
        level=DatasetLevel.NATIONAL,
        dataset="populace_cps",
        year=year,
        subset=None,
        folder="prebuilt/us/populace_cps/all",
    )


def _state_target(state_code, state_fips, year=2026):
    return YearFileTarget(
        country=US,
        level=DatasetLevel.STATE,
        dataset="populace_cps_local",
        year=year,
        subset=SubsetFilter(
            region=f"state/{state_code.lower()}",
            code=state_code,
            variable="state_fips",
            value=state_fips,
            scoping_strategy=_state_filter(state_fips),
        ),
        folder=f"prebuilt/us/populace_cps_local/{state_code}",
    )


def _group_region(group):
    return RegionResolution(
        code="region_group/" + "+".join(sorted(group)),
        scoping_strategy=RegionGroupStrategy(
            members=[_state_filter(0) for _code in group]
        ),
    )


def test_dataset_levels():
    assert [level.value for level in DatasetLevel] == [
        "national",
        "state_group",
        "state",
        "district",
        "nation",
        "local_area",
    ]


class TestCountrySettings:
    def test_us_builds_todays_files(self):
        prebuilt = levels.prebuilt_for("us")

        assert prebuilt.country == US
        assert prebuilt.years == (2026, 2027, 2025)
        assert prebuilt.year_file_levels == (DatasetLevel.NATIONAL, DatasetLevel.STATE)
        assert prebuilt.baseline_levels == (DatasetLevel.STATE_GROUP,)
        assert set(prebuilt.handlers) == {
            DatasetLevel.NATIONAL,
            DatasetLevel.STATE_GROUP,
            DatasetLevel.STATE,
            DatasetLevel.DISTRICT,
        }
        for level, handler in prebuilt.handlers.items():
            assert (handler.country, handler.level) == (US, level)

    def test_uk_builds_nothing_yet(self):
        prebuilt = levels.prebuilt_for("UK")

        assert prebuilt.country == UK
        assert prebuilt.years == ()
        assert prebuilt.year_file_levels == ()
        assert prebuilt.baseline_levels == ()
        assert set(prebuilt.handlers) == {
            DatasetLevel.NATIONAL,
            DatasetLevel.NATION,
            DatasetLevel.LOCAL_AREA,
        }

    def test_another_country_has_no_prebuilt_files(self):
        assert levels.prebuilt_for("fr") is None

    def test_handler_for(self):
        assert levels.handler_for("us", DatasetLevel.STATE).level == DatasetLevel.STATE
        with pytest.raises(KeyError):
            levels.handler_for("uk", DatasetLevel.STATE)

    def test_prebuilt_folder(self):
        assert levels.prebuilt_folder("US", "populace_cps") == (
            "prebuilt/us/populace_cps/all"
        )
        assert levels.prebuilt_folder("us", "populace_cps_local", "CA") == (
            "prebuilt/us/populace_cps_local/CA"
        )


@pytest.mark.usefixtures("inputs")
class TestLevelOfARun:
    def test_us_runs(self, default_dataset, state_level_dataset):
        prebuilt = levels.prebuilt_for("us")
        group = _group_region(["state/ca"])

        assert prebuilt.level_of(_run(US, group, default_dataset)) == (
            DatasetLevel.STATE_GROUP
        )
        assert prebuilt.level_of(
            _run(US, RegionResolution(code="us"), default_dataset)
        ) == (DatasetLevel.NATIONAL)
        assert prebuilt.level_of(
            _run(US, RegionResolution(code="state/ca"), state_level_dataset)
        ) == (DatasetLevel.STATE)
        assert prebuilt.level_of(
            _run(
                US,
                RegionResolution(code="congressional_district/CA-01"),
                state_level_dataset,
            )
        ) == (DatasetLevel.DISTRICT)
        assert (
            prebuilt.level_of(
                _run(US, RegionResolution(code="place/CA-44000"), default_dataset)
            )
            is None
        )

    def test_uk_runs(self, monkeypatch):
        from policyengine_simulation_executor import uk_local_authority_metadata

        monkeypatch.setattr(
            uk_local_authority_metadata,
            "routed_uk_area_dataset_name",
            lambda: "microcosm_dense",
        )
        prebuilt = levels.prebuilt_for("uk")
        uk_default = DatasetSelection("enhanced_frs", "hf://uk", is_default=True)
        dense = DatasetSelection("microcosm_dense", "hf://dense", is_default=False)

        assert prebuilt.level_of(_run(UK, RegionResolution(code="uk"), dense)) == (
            DatasetLevel.LOCAL_AREA
        )
        assert prebuilt.level_of(_run(UK, RegionResolution(code="uk"), uk_default)) == (
            DatasetLevel.NATIONAL
        )
        assert prebuilt.level_of(
            _run(UK, RegionResolution(code="country/wales"), uk_default)
        ) == (DatasetLevel.NATION)
        assert (
            prebuilt.level_of(
                _run(UK, RegionResolution(code="constituency/E1"), uk_default)
            )
            is None
        )

    def test_a_uk_group_of_nations_is_a_national_run(self):
        uk_default = DatasetSelection("enhanced_frs", "hf://uk", is_default=True)
        group = RegionResolution(
            code="region_group/country/england+country/wales",
            scoping_strategy=RegionGroupStrategy(
                members=[
                    RowFilterStrategy(variable_name="region", variable_value="ENGLAND"),
                    RowFilterStrategy(variable_name="region", variable_value="WALES"),
                ]
            ),
        )
        run = _run(UK, group, uk_default)
        handler = levels.handler_for("uk", DatasetLevel.NATIONAL)

        assert levels.prebuilt_for("uk").level_of(run) == DatasetLevel.NATIONAL
        assert handler.baseline_scope(run) == BaselineScope(
            region=group.code, scope_key=group.scoping_strategy.cache_key
        )
        assert handler.year_file_for(run).subset is None

    def test_an_unrouted_uk_dataset_has_no_level(self, monkeypatch):
        from policyengine_simulation_executor import uk_local_authority_metadata

        def not_routed():
            msg = "the bundle routes no area regions"
            raise ValueError(msg)

        monkeypatch.setattr(
            uk_local_authority_metadata, "routed_uk_area_dataset_name", not_routed
        )
        other = DatasetSelection("microcosm_dense", "hf://dense", is_default=False)

        assert (
            levels.prebuilt_for("uk").level_of(
                _run(UK, RegionResolution(code="uk"), other)
            )
            is None
        )

    def test_a_failing_classification_means_no_level(
        self, monkeypatch, default_dataset
    ):
        def failing_classification(_run):
            msg = "registry unavailable"
            raise RuntimeError(msg)

        monkeypatch.setattr(levels_us, "us_level_of", failing_classification)

        assert (
            levels.prebuilt_for("us").level_of(
                _run(US, RegionResolution(code="us"), default_dataset)
            )
            is None
        )


@pytest.mark.usefixtures("inputs")
class TestTargets:
    def test_national_year_file(self):
        handler = levels.handler_for("us", DatasetLevel.NATIONAL)

        assert handler.year_file_targets(2027) == [_national_target(2027)]
        assert handler.baseline_targets(2027) == []
        assert handler.source_file_targets() == []

    def test_state_year_files_sorted_by_state_code(self):
        handler = levels.handler_for("us", DatasetLevel.STATE)

        targets = handler.year_file_targets(2026)

        assert targets == [
            _state_target(state_code, state_fips)
            for state_code, state_fips in sorted(US_STATE_FIPS)
        ]
        assert handler.baseline_targets(2026) == []

    def test_states_on_the_default_dataset_get_no_state_file(self, inputs):
        for region in inputs.regions:
            if region.state_code != "WY":
                region.dataset_path = inputs.us_bundle.dataset_uris["populace_cps"]

        targets = levels.handler_for("us", DatasetLevel.STATE).year_file_targets(2026)

        assert [target.subset.code for target in targets] == ["WY"]

    def test_a_state_without_a_row_filter_is_refused(self, inputs):
        inputs.regions[0].scoping_strategy = None

        with pytest.raises(RuntimeError, match="state/al"):
            levels.handler_for("us", DatasetLevel.STATE).year_file_targets(2026)

    def test_group_baselines(self):
        handler = levels.handler_for("us", DatasetLevel.STATE_GROUP)

        targets = handler.baseline_targets(2025)

        assert handler.year_file_targets(2025) == []
        assert [target.request for target in targets] == [
            {
                "country": "us",
                "scope": "macro",
                "region_group": list(group),
                "time_period": 2025,
            }
            for group in US_REGION_GROUPS
        ]
        assert {target.year_file for target in targets} == {_national_target(2025)}
        assert [target.label for target in targets[:2]] == [
            "group 1 of 20",
            "group 2 of 20",
        ]
        assert {(target.country, target.level, target.year) for target in targets} == {
            (US, DatasetLevel.STATE_GROUP, 2025)
        }

    def test_districts_and_uk_levels_build_nothing(self):
        for country, level in [
            ("us", DatasetLevel.DISTRICT),
            ("uk", DatasetLevel.NATIONAL),
            ("uk", DatasetLevel.NATION),
            ("uk", DatasetLevel.LOCAL_AREA),
        ]:
            handler = levels.handler_for(country, level)
            assert handler.year_file_targets(2026) == []
            assert handler.baseline_targets(2026) == []
            assert handler.source_file_targets() == []

    def test_national_partition_is_stage12s(self):
        assert national_partition.national_region_groups("us") == [
            list(group) for group in US_REGION_GROUPS
        ]
        assert national_partition.national_region_groups("uk") is None


@pytest.mark.usefixtures("inputs")
class TestWhatARunReads:
    def test_year_file_of_each_us_level(self, default_dataset, state_level_dataset):
        utah = RegionResolution(
            code="state/ut", scoping_strategy=_state_filter(49), state_code="UT"
        )
        utah_district = RegionResolution(
            code="congressional_district/UT-01",
            scoping_strategy=RowFilterStrategy(
                variable_name="congressional_district_geoid", variable_value=4901
            ),
            state_code="UT",
        )

        def year_file(level, region, dataset):
            return levels.handler_for("us", level).year_file_for(
                _run(US, region, dataset)
            )

        assert year_file(
            DatasetLevel.NATIONAL, RegionResolution(code="us"), default_dataset
        ) == (_national_target())
        assert year_file(
            DatasetLevel.STATE_GROUP, _group_region(["state/ca"]), default_dataset
        ) == (_national_target())
        assert year_file(DatasetLevel.STATE, utah, state_level_dataset) == (
            _state_target("UT", 49)
        )
        assert year_file(DatasetLevel.DISTRICT, utah_district, state_level_dataset) == (
            _state_target("UT", 49)
        )
        assert year_file(DatasetLevel.STATE, utah, default_dataset) == (
            _national_target()
        )

    def test_baseline_scope_of_each_level(
        self, default_dataset, state_level_dataset, monkeypatch
    ):
        group = _group_region(["state/ca", "state/wv"])
        utah = RegionResolution(
            code="state/ut", scoping_strategy=_state_filter(49), state_code="UT"
        )
        uk_default = DatasetSelection("enhanced_frs", "hf://uk", is_default=True)

        def scope(country, level, region, dataset):
            return levels.handler_for(country, level).baseline_scope(
                _run(country, region, dataset)
            )

        assert scope(
            "us", DatasetLevel.NATIONAL, RegionResolution(code="us"), default_dataset
        ) == BaselineScope(region="national", scope_key=None)
        assert scope(
            "us", DatasetLevel.STATE_GROUP, group, default_dataset
        ) == BaselineScope(
            region=group.code, scope_key=group.scoping_strategy.cache_key
        )
        assert scope("us", DatasetLevel.STATE_GROUP, group, state_level_dataset) is None
        assert scope("us", DatasetLevel.STATE, utah, state_level_dataset) is None
        assert scope("us", DatasetLevel.DISTRICT, utah, state_level_dataset) is None
        assert scope(
            "uk", DatasetLevel.NATIONAL, RegionResolution(code="uk"), uk_default
        ) == BaselineScope(region="national", scope_key=None)
        assert (
            scope(
                "uk",
                DatasetLevel.NATION,
                RegionResolution(code="country/wales"),
                uk_default,
            )
            is None
        )
        assert (
            scope(
                "uk",
                DatasetLevel.LOCAL_AREA,
                RegionResolution(code="uk"),
                DatasetSelection("microcosm_dense", "hf://dense", is_default=False),
            )
            is None
        )


@pytest.fixture
def prebuilt_data_folder(inputs, tmp_path, monkeypatch):
    """Return a data folder holding the national and Utah 2026 year files."""
    national = tmp_path / "prebuilt" / "us" / "populace_cps" / "all"
    utah = tmp_path / "prebuilt" / "us" / "populace_cps_local" / "UT"
    for folder, filename in [
        (national, "populace_cps_year_2026.h5"),
        (utah, "populace_cps_local_year_2026.h5"),
    ]:
        folder.mkdir(parents=True)
        (folder / filename).write_bytes(b"year file")
    reuse_checks = SimpleNamespace(problem=None, checked=[])

    def year_file_reuse_problem(path, dataset):
        reuse_checks.checked.append((Path(path), dataset))
        if isinstance(reuse_checks.problem, Exception):
            raise reuse_checks.problem
        return reuse_checks.problem

    monkeypatch.setattr(levels_us, "year_file_reuse_problem", year_file_reuse_problem)
    return SimpleNamespace(
        root=str(tmp_path), national=national, utah=utah, reuse_checks=reuse_checks
    )


class TestPrebuiltDatasetFolder:
    def _lookup(self, region, dataset, data_folder, *, country=US, year=2026):
        return levels.prebuilt_dataset_folder(
            _run(country, region, dataset, year=year), data_folder
        )

    def test_a_national_run_reads_the_prebuilt_national_folder(
        self, prebuilt_data_folder, default_dataset
    ):
        found = self._lookup(
            RegionResolution(code="us"), default_dataset, prebuilt_data_folder.root
        )

        assert found.folder == str(prebuilt_data_folder.national)
        assert found.dataset_source is None
        assert prebuilt_data_folder.reuse_checks.checked == [
            (
                prebuilt_data_folder.national / "populace_cps_year_2026.h5",
                "populace_cps",
            )
        ]

    def test_a_group_run_reads_the_prebuilt_national_folder(
        self, prebuilt_data_folder, default_dataset
    ):
        found = self._lookup(
            _group_region(["state/ca"]), default_dataset, prebuilt_data_folder.root
        )

        assert found.folder == str(prebuilt_data_folder.national)

    def test_state_and_district_runs_read_their_states_folder(
        self, prebuilt_data_folder, state_level_dataset
    ):
        for region in [
            RegionResolution(
                code="state/ut", scoping_strategy=_state_filter(49), state_code="UT"
            ),
            RegionResolution(
                code="congressional_district/UT-01",
                scoping_strategy=RowFilterStrategy(
                    variable_name="congressional_district_geoid", variable_value=4901
                ),
                state_code="UT",
            ),
        ]:
            found = self._lookup(region, state_level_dataset, prebuilt_data_folder.root)

            assert found.folder == str(prebuilt_data_folder.utah)
            assert found.dataset_source == "baked_state"

    def test_runs_without_a_prebuilt_file_read_nothing_prebuilt(
        self, prebuilt_data_folder, default_dataset, state_level_dataset
    ):
        wyoming = RegionResolution(
            code="state/wy", scoping_strategy=_state_filter(56), state_code="WY"
        )

        assert (
            self._lookup(
                RegionResolution(code="us"),
                default_dataset,
                prebuilt_data_folder.root,
                year=2028,
            )
            is None
        )
        assert (
            self._lookup(wyoming, state_level_dataset, prebuilt_data_folder.root)
            is None
        )
        assert (
            self._lookup(
                RegionResolution(code="place/CA-44000"),
                default_dataset,
                prebuilt_data_folder.root,
            )
            is None
        )
        assert (
            self._lookup(
                RegionResolution(code="uk"),
                DatasetSelection("enhanced_frs", "hf://uk", is_default=True),
                prebuilt_data_folder.root,
                country=UK,
            )
            is None
        )

    def test_settings_that_fail_to_load_read_nothing_prebuilt(
        self, prebuilt_data_folder, default_dataset, monkeypatch
    ):
        def failing_settings():
            msg = "No module named 'policyengine_stage12_persistence'"
            raise ModuleNotFoundError(msg)

        monkeypatch.setattr(levels_us, "us_prebuilt", failing_settings)

        assert (
            self._lookup(
                RegionResolution(code="us"), default_dataset, prebuilt_data_folder.root
            )
            is None
        )

    @pytest.mark.parametrize(
        "problem", ["year-file stamp is missing", ImportError("moved helper")]
    )
    def test_a_file_that_fails_the_reuse_checks_is_not_read(
        self, prebuilt_data_folder, default_dataset, problem
    ):
        prebuilt_data_folder.reuse_checks.problem = problem

        assert (
            self._lookup(
                RegionResolution(code="us"), default_dataset, prebuilt_data_folder.root
            )
            is None
        )


@pytest.mark.usefixtures("inputs")
class TestWritingWholeDatasetYearFiles:
    def test_national_year_file_is_prepared_in_the_data_folder(
        self, inputs, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("POLICYENGINE_DATA_FOLDER", str(tmp_path))
        calls = []

        def ensure_datasets(**keyword_arguments):
            calls.append(keyword_arguments)
            (tmp_path / "populace_cps_year_2026.h5").write_bytes(b"year file")

        inputs.us_module.ensure_datasets = ensure_datasets
        target = _national_target()

        written = levels.handler_for("us", DatasetLevel.NATIONAL).write_year_files(
            2026, [target], tmp_path / "work"
        )

        assert calls == [
            {
                "datasets": ["populace_cps"],
                "years": [2026],
                "data_folder": str(tmp_path),
            }
        ]
        assert written == {target: tmp_path / "populace_cps_year_2026.h5"}

    def test_a_missing_year_file_fails(self, inputs, tmp_path, monkeypatch):
        monkeypatch.setenv("POLICYENGINE_DATA_FOLDER", str(tmp_path))
        inputs.us_module.ensure_datasets = lambda **_keyword_arguments: None

        with pytest.raises(RuntimeError, match="no file"):
            levels.handler_for("us", DatasetLevel.NATIONAL).write_year_files(
                2026, [_national_target()], tmp_path / "work"
            )


def test_year_file_name(inputs):
    assert levels.year_file_name(_state_target("UT", 49)) == (
        "populace_cps_local_year_2026.h5"
    )


def test_handler_modules_import_no_run_simulation_feature_module():
    for module in (levels, levels_us, levels_uk, partition):
        tree = ast.parse(Path(module.__file__).read_text())
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add(node.module or "")
        assert not imported & FORBIDDEN_IMPORTS, module.__name__
