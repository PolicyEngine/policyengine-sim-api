"""Tests for building the precompute's national and per-state datasets."""

from pathlib import Path
from types import SimpleNamespace

import pytest
from policyengine_simulation_executor import precompute, precompute_datasets
from policyengine_simulation_executor.precompute_models import (
    DatasetPlanEntry,
    StateDatasetPlanEntry,
)


class TestBuildDatasetImpl:
    @pytest.fixture
    def dataset_stubs(self, monkeypatch, tmp_path):
        from policyengine_simulation_executor import (
            artifact_keys,
            artifact_store,
            release_bundle,
        )
        from policyengine_simulation_executor import simulation_runtime as sr

        state = SimpleNamespace(
            folder=tmp_path,
            uploads=[],
            ensure_calls=[],
            write_file=True,
            identity=SimpleNamespace(
                digest="ds-2026",
                store_path="datasets/us/ds-2026/populace_year_2026.h5",
                filename="populace_year_2026.h5",
            ),
        )

        class FakeStore:
            def __init__(self, bucket):
                self.bucket = bucket

            def upload_file(self, path, local_path):
                state.uploads.append((path, str(local_path)))
                return True

        def fake_ensure_datasets(*, datasets, years, data_folder):
            state.ensure_calls.append((datasets, years, data_folder))
            if state.write_file:
                (tmp_path / state.identity.filename).write_bytes(b"h5-bytes")

        monkeypatch.setattr(
            artifact_keys,
            "collect_dataset_identity",
            lambda _country, _year: state.identity,
        )
        monkeypatch.setattr(artifact_store, "ArtifactStore", FakeStore)
        monkeypatch.setattr(sr, "resolve_data_folder", lambda: str(tmp_path))
        monkeypatch.setattr(
            sr,
            "_country_module",
            lambda _country: SimpleNamespace(ensure_datasets=fake_ensure_datasets),
        )
        monkeypatch.setattr(
            release_bundle,
            "get_country_release_bundle",
            lambda _country: SimpleNamespace(default_dataset="populace_cps"),
        )
        return state

    def _entry(self, path="datasets/us/ds-2026/populace_year_2026.h5"):
        return DatasetPlanEntry(
            year=2026,
            digest="ds-2026",
            path=path,
            filename="populace_year_2026.h5",
            exists=False,
        )

    def test_builds_and_uploads_under_the_planned_key(self, dataset_stubs):
        result = precompute.build_dataset_impl("bucket-x", self._entry())
        assert dataset_stubs.ensure_calls == [
            (["populace_cps"], [2026], str(dataset_stubs.folder))
        ]
        assert dataset_stubs.uploads == [
            (
                "datasets/us/ds-2026/populace_year_2026.h5",
                str(dataset_stubs.folder / "populace_year_2026.h5"),
            )
        ]
        assert result.year == 2026
        assert result.uploaded is True
        assert result.size_bytes == len(b"h5-bytes")

    def test_refuses_to_upload_under_a_mismatched_key(self, dataset_stubs):
        with pytest.raises(RuntimeError, match="refusing to upload"):
            precompute.build_dataset_impl(
                "bucket-x", self._entry(path="datasets/us/OTHER/file.h5")
            )
        assert dataset_stubs.uploads == []

    def test_fails_loudly_when_no_file_is_produced(self, dataset_stubs):
        dataset_stubs.write_file = False
        with pytest.raises(RuntimeError, match="produced no file"):
            precompute.build_dataset_impl("bucket-x", self._entry())
        assert dataset_stubs.uploads == []


class TestBuildStateDatasetsImpl:
    """Building, checking and uploading one year's per-state datasets."""

    YEAR = 2026

    @pytest.fixture
    def state_build(self, monkeypatch, tmp_path):
        from fixtures import state_dataset_sources as sources
        from policyengine.core.scoping_strategy import RowFilterStrategy
        from policyengine.tax_benefit_models import us as us_module
        from policyengine_simulation_executor import (
            artifact_keys,
            artifact_store,
        )
        from policyengine_simulation_executor import simulation_runtime as sr

        state = SimpleNamespace(
            uploads=[],
            existing=set(),
            ensure_calls=[],
            entity_frames=sources.source_entity_frames(),
            work_folder=tmp_path / "work",
        )

        def target(state_code, fips):
            return precompute.StateDatasetTarget(
                region=f"state/{state_code.lower()}",
                state_code=state_code,
                state_fips=fips,
                filter_variable="state_fips",
                dataset=sources.REGIONAL_DATASET,
                scoping_strategy=RowFilterStrategy(
                    variable_name="state_fips", variable_value=fips
                ),
            )

        state.targets = [
            target("UT", sources.UTAH_FIPS),
            target("WY", sources.WYOMING_FIPS),
        ]
        monkeypatch.setattr(
            precompute_datasets, "state_dataset_targets", lambda: list(state.targets)
        )

        def fake_state_identity(
            _country, year, *, dataset, state_code, state_fips, filter_variable
        ):
            digest = f"sd-{year}-{state_code}-{state_fips}-{filter_variable}"
            return SimpleNamespace(
                digest=digest,
                store_path=f"state-datasets/us/{digest}/{dataset}_year_{year}.h5",
                filename=f"{dataset}_year_{year}.h5",
            )

        monkeypatch.setattr(
            artifact_keys, "collect_state_dataset_identity", fake_state_identity
        )

        class FakeStore:
            def __init__(self, bucket):
                self.bucket = bucket

            def upload_file(self, path, local_path):
                state.uploads.append((path, Path(local_path)))
                return path not in state.existing

        monkeypatch.setattr(artifact_store, "ArtifactStore", FakeStore)

        def fake_ensure_datasets(*, datasets, years, data_folder):
            state.ensure_calls.append((datasets, years, data_folder))
            folder = Path(data_folder)
            folder.mkdir(parents=True, exist_ok=True)
            for year in years:
                sources.write_year_file(
                    folder, year=year, entity_frames=state.entity_frames
                )
            return us_module.load_datasets(
                datasets=datasets, years=years, data_folder=data_folder
            )

        districts = [
            SimpleNamespace(
                scoping_strategy=RowFilterStrategy(
                    variable_name="congressional_district_geoid",
                    variable_value=geoid,
                )
            )
            for geoid in (4901, 4902, 4903, 4904, 5600)
        ]
        monkeypatch.setattr(
            sr,
            "_country_module",
            lambda _country: SimpleNamespace(
                ensure_datasets=fake_ensure_datasets,
                load_datasets=us_module.load_datasets,
                model=SimpleNamespace(
                    region_registry=SimpleNamespace(
                        get_by_type=lambda kind: (
                            districts if kind == "congressional_district" else []
                        )
                    )
                ),
            ),
        )
        state.sources = sources
        state.entry = lambda state_code, fips: self._entry(
            fake_state_identity, state_code, fips
        )
        return state

    def _entry(self, identity_for, state_code, fips):
        from fixtures.state_dataset_sources import REGIONAL_DATASET

        identity = identity_for(
            "us",
            self.YEAR,
            dataset=REGIONAL_DATASET,
            state_code=state_code,
            state_fips=fips,
            filter_variable="state_fips",
        )
        return StateDatasetPlanEntry(
            year=self.YEAR,
            state_code=state_code,
            region=f"state/{state_code.lower()}",
            dataset=REGIONAL_DATASET,
            digest=identity.digest,
            path=identity.store_path,
            filename=identity.filename,
            folder=f"state-datasets/{REGIONAL_DATASET}/{state_code}",
            exists=False,
        )

    def _build(self, state_build, entries):
        return precompute.build_state_datasets_impl(
            "bucket-x", self.YEAR, entries, work_folder=state_build.work_folder
        )

    def _both_states(self, state_build):
        return [
            state_build.entry("UT", state_build.sources.UTAH_FIPS),
            state_build.entry("WY", state_build.sources.WYOMING_FIPS),
        ]

    def test_writes_each_state_file_with_only_its_households(self, state_build):
        sources = state_build.sources
        results = self._build(state_build, self._both_states(state_build))

        files = dict(state_build.uploads)
        utah, wyoming = (files[entry.path] for entry in self._both_states(state_build))
        assert sources.stored_ids(utah, "household") == [1, 2]
        assert sources.stored_ids(utah, "person") == [1, 2, 3]
        assert sources.stored_ids(utah, "tax_unit") == [1, 2]
        assert sources.stored_ids(utah, "marital_unit") == [1, 2, 3]
        assert sources.stored_ids(wyoming, "household") == [3]
        assert sources.stored_ids(wyoming, "person") == [4]
        assert sources.stored_ids(wyoming, "spm_unit") == [3]
        assert [(result.state_code, result.year) for result in results] == [
            ("UT", 2026),
            ("WY", 2026),
        ]

    def test_keeps_every_weight_unchanged(self, state_build):
        sources = state_build.sources
        self._build(state_build, self._both_states(state_build))

        utah = state_build.uploads[0][1]
        assert sources.stored_weights(utah, "household") == [10.0, 20.0]
        assert sources.stored_weights(utah, "person") == [10.0, 11.0, 20.0]
        assert sources.stored_weights(utah, "marital_unit") == [10.0, 11.0, 20.0]

    def test_state_files_pass_dotpy_reuse_checks(self, state_build):
        from policyengine.tax_benefit_models.us.datasets import (
            _expected_legacy_spm_profile,
        )
        from policyengine.tax_benefit_models.us.legacy_inputs import (
            read_year_file_stamp,
            year_file_stamp,
            year_file_stamp_problem,
        )
        from policyengine.tax_benefit_models.us.legacy_spm.record import (
            year_file_problem,
        )

        self._build(state_build, self._both_states(state_build))

        for _path, local in state_build.uploads:
            assert read_year_file_stamp(local) == year_file_stamp()
            assert year_file_stamp_problem(local) is None
            assert (
                year_file_problem(
                    local,
                    _expected_legacy_spm_profile(state_build.sources.REGIONAL_DATASET),
                )
                is None
            )

    def test_dotpy_reuses_a_state_file_without_rebuilding(
        self, state_build, monkeypatch
    ):
        from policyengine.tax_benefit_models.us import datasets as us_datasets

        self._build(state_build, self._both_states(state_build))

        def refuse_to_rebuild(**_kwargs):
            msg = "ensure_datasets rebuilt a baked state file"
            raise AssertionError(msg)

        monkeypatch.setattr(us_datasets, "create_datasets", refuse_to_rebuild)
        utah_folder = state_build.uploads[0][1].parent
        loaded = us_datasets.ensure_datasets(
            datasets=[state_build.sources.REGIONAL_DATASET],
            years=[self.YEAR],
            data_folder=str(utah_folder),
        )

        (dataset,) = loaded.values()
        assert sorted(dataset.data.household["household_id"]) == [1, 2]

    def test_each_state_file_holds_exactly_the_rows_its_filter_keeps(self, state_build):
        from policyengine.tax_benefit_models.us.model import US_GROUP_ENTITIES

        entries = self._both_states(state_build)
        self._build(state_build, entries)
        stored = dict(state_build.uploads)

        for target, entry in zip(state_build.targets, entries, strict=True):
            expected = target.scoping_strategy.apply(
                entity_data=state_build.sources.source_entity_frames(),
                group_entities=US_GROUP_ENTITIES,
                year=self.YEAR,
            )
            for entity, frame in expected.items():
                assert state_build.sources.stored_ids(stored[entry.path], entity) == [
                    int(value) for value in frame[f"{entity}_id"]
                ], (target.state_code, entity)

    def test_prepares_the_full_country_year_file_once(self, state_build):
        self._build(state_build, self._both_states(state_build))

        assert len(state_build.ensure_calls) == 1
        datasets, years, folder = state_build.ensure_calls[0]
        assert datasets == [state_build.sources.REGIONAL_DATASET]
        assert years == [self.YEAR]
        assert Path(folder).is_relative_to(state_build.work_folder)

    def test_uploads_each_file_under_its_planned_path(self, state_build):
        entries = self._both_states(state_build)
        state_build.existing.add(entries[1].path)

        results = self._build(state_build, entries)

        assert [path for path, _local in state_build.uploads] == [
            entry.path for entry in entries
        ]
        assert [local.name for _path, local in state_build.uploads] == [
            entry.filename for entry in entries
        ]
        assert [result.uploaded for result in results] == [True, False]
        assert [result.path for result in results] == [entry.path for entry in entries]
        assert all(result.size_bytes > 0 for result in results)
        assert 0 <= results[0].build_seconds <= results[1].build_seconds

    def test_refuses_a_district_that_lies_outside_its_state(self, state_build):
        state_build.entity_frames = state_build.sources.source_entity_frames(
            wyoming_district_geoid=4903
        )

        with pytest.raises(RuntimeError, match="congressional district"):
            self._build(state_build, self._both_states(state_build))
        assert state_build.uploads == []

    def test_a_household_without_a_district_code_does_not_stop_the_build(
        self, state_build
    ):
        state_build.entity_frames = state_build.sources.source_entity_frames(
            wyoming_district_geoid=0
        )

        results = self._build(state_build, self._both_states(state_build))

        assert [result.state_code for result in results] == ["UT", "WY"]

    def test_refuses_a_target_without_a_row_filter(self, state_build):
        from dataclasses import replace

        state_build.targets[0] = replace(state_build.targets[0], scoping_strategy=None)

        with pytest.raises(RuntimeError, match="no row filter"):
            self._build(state_build, self._both_states(state_build))
        assert state_build.uploads == []
        assert state_build.ensure_calls == []

    def test_refuses_a_state_with_no_households(self, state_build):
        from policyengine.core.scoping_strategy import RowFilterStrategy

        state_build.targets.append(
            precompute.StateDatasetTarget(
                region="state/id",
                state_code="ID",
                state_fips=16,
                filter_variable="state_fips",
                dataset=state_build.sources.REGIONAL_DATASET,
                scoping_strategy=RowFilterStrategy(
                    variable_name="state_fips", variable_value=16
                ),
            )
        )

        with pytest.raises(ValueError, match="No households"):
            self._build(state_build, [state_build.entry("ID", 16)])
        assert state_build.uploads == []

    def test_refuses_a_plan_entry_whose_key_differs_in_the_container(self, state_build):
        entry = state_build.entry("UT", state_build.sources.UTAH_FIPS)
        mismatched = entry.model_copy(update={"path": "state-datasets/us/OTHER/x.h5"})

        with pytest.raises(RuntimeError, match="refusing to build"):
            self._build(state_build, [mismatched])
        assert state_build.ensure_calls == []

    def test_refuses_a_state_the_container_does_not_target(self, state_build):
        state_build.targets = state_build.targets[:1]

        with pytest.raises(RuntimeError, match="state/wy"):
            self._build(state_build, self._both_states(state_build))
        assert state_build.ensure_calls == []

    def test_refuses_entries_for_another_year(self, state_build):
        entry = state_build.entry("UT", state_build.sources.UTAH_FIPS)

        with pytest.raises(ValueError, match="2027"):
            precompute.build_state_datasets_impl(
                "bucket-x",
                2027,
                [entry],
                work_folder=state_build.work_folder,
            )
