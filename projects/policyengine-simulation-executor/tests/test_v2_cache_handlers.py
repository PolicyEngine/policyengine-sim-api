"""The v2 report app's cache plans, places and checks files through the handlers.

The v2 cache takes its years from the US handler settings, its year files and
baselines from the handlers' targets, and its image folders from the targets'
``prebuilt/…`` folders. Which year-file and baseline levels it covers are its
own settings (``stage12_cache.cache_levels``): it never follows the
run_simulation app's levels, so a run_simulation task cannot add files or
fixed IDs to the v2 cache by itself.

The inputs are the fixed stand-ins in ``fixtures.v2_cache_us_inputs``.
"""

from __future__ import annotations

from hashlib import sha256
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest
from fixtures.v2_cache_us_inputs import (
    BUNDLE_MANIFEST_SHA256,
    DEFAULT_DATASET,
    STATE_LEVEL_DATASET,
    enable_fixed_spm,
    install_v2_cache_us_inputs,
)
from policyengine_simulation_executor.stage12_cache import keys, precompute
from policyengine_simulation_executor.stage12_cache.store import Stage12CacheStore
from src.modal.stage12_cache_image import fetch_stage12_cache

if TYPE_CHECKING:
    from pathlib import Path

BUCKET = "bucket-handlers"
NATIONAL_FOLDER = f"prebuilt/us/{DEFAULT_DATASET}/all"


def _state_folder(state_code: str) -> str:
    return f"prebuilt/us/{STATE_LEVEL_DATASET}/{state_code}"


def _stored_bytes(path: str) -> bytes:
    return f"v2-cache-handlers:{path}".encode()


@pytest.fixture
def us_inputs(monkeypatch):
    inputs = install_v2_cache_us_inputs(monkeypatch)
    enable_fixed_spm(inputs)
    return inputs


@pytest.fixture(scope="module")
def published():
    """Plan the fixed US inputs, store each planned file and build the manifest."""
    with pytest.MonkeyPatch.context() as module_patch:
        inputs = install_v2_cache_us_inputs(module_patch)
        enable_fixed_spm(inputs)
        plan = precompute.plan_artifacts_impl(BUCKET)
        for entry in (*plan.datasets, *plan.baselines, *plan.state_datasets):
            inputs.cache_bucket.store(entry.path, _stored_bytes(entry.path))
        manifest = precompute.build_manifest(
            plan, Stage12CacheStore(BUCKET)
        ).canonical_payload()
        yield SimpleNamespace(plan=plan, inputs=inputs, manifest=manifest)


class TestPlanThroughTheHandlers:
    """``plan_artifacts_impl`` asks the handlers what to build and where it goes."""

    def test_every_entry_names_its_country_and_prebuilt_folder(self, published):
        plan = published.plan

        assert {entry.country for entry in plan.datasets} == {"us"}
        assert {entry.country for entry in plan.baselines} == {"us"}
        assert {entry.country for entry in plan.state_datasets} == {"us"}
        assert [entry.folder for entry in plan.datasets] == [NATIONAL_FOLDER] * 3
        # A baseline sits beside the year file it was computed on, where
        # .py's Simulation.ensure() looks for it.
        assert [entry.folder for entry in plan.baselines] == [NATIONAL_FOLDER] * 60
        assert [entry.folder for entry in plan.state_datasets] == [
            _state_folder(entry.state_code) for entry in plan.state_datasets
        ]

    def test_the_years_follow_the_us_handler_settings(self, us_inputs, monkeypatch):
        from policyengine_simulation_executor.stage12_cache import levels_us

        monkeypatch.setattr(levels_us, "US_PREBUILT_YEARS", (2026,))

        plan = precompute.plan_artifacts_impl(BUCKET)

        assert plan.years == (2026,)
        assert [entry.year for entry in plan.datasets] == [2026]
        assert len(plan.baselines) == 20
        assert len(plan.state_datasets) == 51

    def test_the_baselines_follow_the_caches_own_baseline_levels(
        self, us_inputs, monkeypatch
    ):
        from policyengine_simulation_executor.stage12_cache import cache_levels

        monkeypatch.setattr(cache_levels, "CACHE_BASELINE_LEVELS", {"us": ()})

        plan = precompute.plan_artifacts_impl(BUCKET)

        assert plan.baselines == ()
        assert len(plan.datasets) == 3

    def test_the_year_files_follow_the_caches_own_year_file_levels(
        self, us_inputs, monkeypatch
    ):
        from policyengine_simulation_executor.stage12_cache import cache_levels
        from policyengine_simulation_executor.stage12_cache.levels import (
            DatasetLevel,
        )

        monkeypatch.setattr(
            cache_levels, "CACHE_YEAR_FILE_LEVELS", {"us": (DatasetLevel.NATIONAL,)}
        )

        plan = precompute.plan_artifacts_impl(BUCKET)

        assert plan.state_datasets == ()
        assert len(plan.datasets) == 3

    def test_the_cache_does_not_follow_the_run_simulation_apps_levels(
        self, us_inputs, monkeypatch
    ):
        from policyengine_simulation_executor.stage12_cache import levels_us
        from policyengine_simulation_executor.stage12_cache.levels import (
            CountryPrebuilt,
            DatasetLevel,
        )

        original = levels_us.us_prebuilt

        def with_state_baselines() -> CountryPrebuilt:
            settings = original()
            return CountryPrebuilt(
                country=settings.country,
                years=settings.years,
                year_file_levels=(DatasetLevel.NATIONAL,),
                baseline_levels=(DatasetLevel.STATE_GROUP, DatasetLevel.STATE),
                handlers=settings.handlers,
                classify=settings.classify,
                baseline_output_preparer=settings.baseline_output_preparer,
            )

        monkeypatch.setattr(levels_us, "us_prebuilt", with_state_baselines)

        plan = precompute.plan_artifacts_impl(BUCKET)

        assert len(plan.baselines) == 60
        assert len(plan.state_datasets) == 153

    def test_refuses_a_year_file_the_v2_cache_has_no_key_for(
        self, us_inputs, monkeypatch
    ):
        from policyengine_simulation_executor.stage12_cache import levels_us
        from policyengine_simulation_executor.stage12_cache.levels import (
            DatasetLevel,
            YearFileTarget,
        )

        # A whole year file of the state-level dataset: v2's dataset key
        # covers only the bundle's default dataset.
        def unsupported_targets(_handler, year):
            return [
                YearFileTarget(
                    country="us",
                    level=DatasetLevel.NATIONAL,
                    dataset=STATE_LEVEL_DATASET,
                    year=year,
                    subset=None,
                    folder=f"prebuilt/us/{STATE_LEVEL_DATASET}/all",
                )
            ]

        monkeypatch.setattr(
            levels_us.UsNationalHandler, "year_file_targets", unsupported_targets
        )

        with pytest.raises(RuntimeError, match="no v2 cache key"):
            precompute.plan_artifacts_impl(BUCKET)


class TestExpectedCacheContents:
    """The years and counts the download step and deploy check require."""

    def test_the_us_contents(self, us_inputs):
        from policyengine_simulation_executor.stage12_cache import (
            precompute_planning,
        )

        assert precompute_planning.expected_cache_contents("us") == {
            "years": [2026, 2027, 2025],
            "artifact_counts": {"dataset": 3, "baseline": 60},
        }

    def test_follow_the_settings(self, us_inputs, monkeypatch):
        from policyengine_simulation_executor.stage12_cache import (
            cache_levels,
            levels_us,
            precompute_planning,
        )

        monkeypatch.setattr(levels_us, "US_PREBUILT_YEARS", (2026, 2027))
        monkeypatch.setattr(cache_levels, "CACHE_BASELINE_LEVELS", {"us": ()})

        assert precompute_planning.expected_cache_contents("us") == {
            "years": [2026, 2027],
            "artifact_counts": {"dataset": 2, "baseline": 0},
        }

    def test_load_no_country_model(self, us_inputs, monkeypatch):
        """The deploying machine computes them without loading the US model."""
        from policyengine_simulation_executor import simulation_runtime
        from policyengine_simulation_executor.stage12_cache import (
            precompute_planning,
        )

        def refuse_to_load(_country):
            msg = "the US country model was loaded"
            raise AssertionError(msg)

        monkeypatch.setattr(simulation_runtime, "_country_module", refuse_to_load)

        contents = precompute_planning.expected_cache_contents("us")

        assert contents["artifact_counts"] == {"dataset": 3, "baseline": 60}


def test_every_manifest_entry_records_its_folder(published):
    folders = {
        (item["type"], item["folder"]) for item in published.manifest["artifacts"]
    }

    assert ("dataset", NATIONAL_FOLDER) in folders
    assert ("baseline", NATIONAL_FOLDER) in folders
    assert ("state_dataset", _state_folder("CA")) in folders
    assert all(item["folder"] for item in published.manifest["artifacts"])
    assert published.manifest["schema"] == "stage12-mf1"


@pytest.fixture
def image(published, monkeypatch, tmp_path):
    """Give a test an image data folder and a download into it, as deployed."""
    data_folder = tmp_path / "data"
    monkeypatch.setenv("POLICYENGINE_DATA_FOLDER", str(data_folder))
    monkeypatch.setenv("STAGE12_BUNDLE_MANIFEST_SHA256", BUNDLE_MANIFEST_SHA256)
    expected = {
        "years": [2026, 2027, 2025],
        "artifact_counts": {"dataset": 3, "baseline": 60},
    }

    def fetch(manifest: dict, expected_contents: dict = expected) -> None:
        monkeypatch.setenv(
            "STAGE12_CACHE_MANIFEST_DIGEST", keys.canonical_digest(manifest)
        )
        fetch_stage12_cache(
            BUCKET,
            manifest,
            expected_contents,
            client=published.inputs.cache_bucket,
        )

    return SimpleNamespace(data_folder=data_folder, fetch=fetch)


def _files_under(folder: Path) -> list[Path]:
    return sorted(path for path in folder.rglob("*") if path.is_file())


def _with_artifact_folder(manifest: dict, kind: str, folder: object) -> dict:
    artifacts = list(manifest["artifacts"])
    index = next(
        position for position, item in enumerate(artifacts) if item["type"] == kind
    )
    changed = dict(artifacts[index])
    if folder is None:
        changed.pop("folder", None)
    else:
        changed["folder"] = folder
    artifacts[index] = changed
    return {**manifest, "artifacts": artifacts}


class TestDownloadStep:
    """``fetch_stage12_cache`` places each file in the folder its entry names."""

    def test_places_every_file_in_its_prebuilt_folder(self, published, image):
        image.fetch(published.manifest)

        for entry in (*published.plan.datasets, *published.plan.baselines):
            destination = image.data_folder / NATIONAL_FOLDER / entry.filename
            assert destination.read_bytes() == _stored_bytes(entry.path)
        for entry in published.plan.state_datasets:
            destination = (
                image.data_folder / _state_folder(entry.state_code) / entry.filename
            )
            assert destination.read_bytes() == _stored_bytes(entry.path)
        assert not list(image.data_folder.glob("*.h5"))

    @pytest.mark.parametrize(
        ("kind", "folder"),
        [
            pytest.param("dataset", None, id="year-file-without-a-folder"),
            pytest.param("baseline", "", id="baseline-with-an-empty-folder"),
            pytest.param("dataset", "datasets/us", id="outside-prebuilt"),
            pytest.param(
                "baseline", f"prebuilt/uk/{DEFAULT_DATASET}/all", id="another-country"
            ),
            pytest.param("state_dataset", "prebuilt/us", id="no-dataset-folder"),
            pytest.param(
                "state_dataset", "prebuilt/us/../../escape", id="leaves-the-folder"
            ),
        ],
    )
    def test_refuses_an_unsafe_or_missing_folder_before_downloading(
        self, published, image, kind, folder
    ):
        with pytest.raises(
            RuntimeError, match=r"^Stage 12 cache artifact metadata is invalid$"
        ):
            image.fetch(_with_artifact_folder(published.manifest, kind, folder))

        assert not image.data_folder.exists() or _files_under(image.data_folder) == []

    def test_checks_the_years_and_counts_it_is_given(self, published, image):
        one_year = {
            "years": [2026],
            "artifact_counts": {"dataset": 1, "baseline": 20},
        }

        with pytest.raises(
            RuntimeError, match=r"^Stage 12 cache manifest has an unexpected year set$"
        ):
            image.fetch(published.manifest, one_year)

        one_year_manifest = {
            **published.manifest,
            "years": [2026],
            "artifacts": [
                item for item in published.manifest["artifacts"] if item["year"] == 2026
            ],
        }
        image.fetch(one_year_manifest, one_year)

        assert len(_files_under(image.data_folder / NATIONAL_FOLDER)) == 21

    @pytest.mark.parametrize(
        "expected_contents",
        [
            pytest.param(None, id="none"),
            pytest.param({}, id="empty"),
            pytest.param({"years": [2026, 2027, 2025]}, id="no-counts"),
            pytest.param(
                {"years": [], "artifact_counts": {"dataset": 3, "baseline": 60}},
                id="no-years",
            ),
            pytest.param(
                {
                    "years": [2026, 2027, 2025],
                    "artifact_counts": {"dataset": "3", "baseline": 60},
                },
                id="count-not-an-integer",
            ),
            pytest.param(
                {"years": [2026, 2027, 2025], "artifact_counts": {"dataset": 3}},
                id="a-kind-missing",
            ),
        ],
    )
    def test_refuses_a_deployment_without_usable_expected_contents(
        self, published, image, expected_contents
    ):
        with pytest.raises(
            RuntimeError, match=r"^Stage 12 cache deployment has no expected contents$"
        ):
            image.fetch(published.manifest, expected_contents)

        assert not image.data_folder.exists() or _files_under(image.data_folder) == []

    def test_refuses_counts_other_than_the_ones_it_is_given(self, published, image):
        fewer_baselines = {
            "years": [2026, 2027, 2025],
            "artifact_counts": {"dataset": 3, "baseline": 59},
        }

        with pytest.raises(
            RuntimeError,
            match=r"^Stage 12 cache manifest has an unexpected artifact count$",
        ):
            image.fetch(published.manifest, fewer_baselines)


def test_the_worker_image_build_passes_the_expected_contents(monkeypatch):
    from policyengine_simulation_executor.stage12_cache import precompute_planning
    from test_stage12_modal_app import _load

    module = _load(monkeypatch)

    (fetch,) = [
        call for call in module.us_worker_image.calls if call[0] == "run_function"
    ]
    _bucket, _manifest, expected = fetch[2]["args"]
    assert expected == precompute_planning.expected_cache_contents("us")


class TestDeployTimeCheck:
    """``_check_installed_cache`` reads the files where the download step put them."""

    def test_reads_its_probe_baseline_from_the_national_prebuilt_folder(
        self, published, image, monkeypatch
    ):
        from policyengine_simulation_executor import stage12_worker_validation

        image.fetch(published.manifest)
        checked = []
        original_check = stage12_worker_validation._check_dataset_access

        def check_dataset_access(path, expected_sha256):
            checked.append(path)
            original_check(path, expected_sha256)

        monkeypatch.setattr(
            stage12_worker_validation, "_check_dataset_access", check_dataset_access
        )
        monkeypatch.setattr(
            stage12_worker_validation,
            "calculate_simulation_frames",
            lambda _simulation: SimpleNamespace(cache_outcome="hit"),
        )

        stage12_worker_validation._check_installed_cache(
            keys.canonical_digest(published.manifest)
        )

        assert checked == [
            str(
                image.data_folder
                / NATIONAL_FOLDER
                / published.plan.baselines[0].filename
            )
        ]

    def test_refuses_a_state_file_missing_from_its_prebuilt_folder(
        self, published, image
    ):
        from policyengine_simulation_executor import stage12_worker_validation

        image.fetch(published.manifest)
        california = published.plan.state_datasets[4]
        assert california.state_code == "CA"
        (image.data_folder / _state_folder("CA") / california.filename).unlink()

        with pytest.raises(RuntimeError, match="state dataset file is missing"):
            stage12_worker_validation._check_installed_cache(
                sha256(
                    (image.data_folder / ".stage12-cache-manifest.json").read_bytes()
                ).hexdigest()
            )

    def test_counts_the_state_files_from_the_handlers(self, us_inputs, monkeypatch):
        from policyengine_simulation_executor import stage12_worker_validation
        from policyengine_simulation_executor.stage12_cache import levels_us

        monkeypatch.setattr(levels_us, "US_PREBUILT_YEARS", (2026,))

        assert stage12_worker_validation.expected_state_dataset_count() == 51
