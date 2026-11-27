"""Pins of the v2 report app's US image download step and its deploy-time check.

Every expected value below is what ``main`` produced on 2026-11-25 (commit
6865dc2c) for the fixed inputs in ``fixtures.v2_cache_us_inputs``, with each
planned file stored in a fake bucket as the bytes ``v2-cache-pin:<path>``.
The manifest is the one the precompute publishes for that plan, so these
pins run the plan, the manifest, ``fetch_stage12_cache`` (the image build's
download step) and ``_check_installed_cache`` (the deployed worker's check)
end to end.

What the download step accepts and refuses must stay unchanged when the v2
cache moves onto the level handlers: exactly 3 year files and 60 baselines,
the years 2026, 2027 and 2025 in that order, and each file's SHA-256 and
size. Since 2026-11-27 the step receives those years and counts from the
deploying machine (``EXPECTED_CONTENTS`` here) instead of holding them as
constants; what it accepts and refuses is unchanged. Where it places the
files is pinned apart, in ``TestImageFolders``: they moved to the handlers'
``prebuilt/…`` folders that day, in a commit of their own.
"""

from __future__ import annotations

import json
from hashlib import sha256
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest
from fixtures.v2_cache_us_inputs import (
    BUNDLE_MANIFEST_SHA256,
    STATE_LEVEL_DATASET,
    enable_fixed_spm,
    install_v2_cache_us_inputs,
)
from policyengine_simulation_executor.stage12_cache import keys, precompute
from policyengine_simulation_executor.stage12_cache.store import Stage12CacheStore
from policyengine_simulation_executor.stage12_runtime.partition import US_REGION_GROUPS
from src.modal.stage12_cache_image import fetch_stage12_cache

if TYPE_CHECKING:
    from pathlib import Path

BUCKET = "bucket-pins"
# What the deploying machine passes the download step for the US cache: the
# years and the year-file and baseline counts ``main`` held as constants.
EXPECTED_CONTENTS = {
    "years": [2026, 2027, 2025],
    "artifact_counts": {"dataset": 3, "baseline": 60},
}
MANIFEST_COPY = ".stage12-cache-manifest.json"
NATIONAL_FOLDER = "prebuilt/us/populace_us_2024/all"


def _stored_bytes(path: str) -> bytes:
    return f"v2-cache-pin:{path}".encode()


def _canonical_bytes(payload: dict) -> bytes:
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")


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


@pytest.fixture
def image(published, monkeypatch, tmp_path):
    """Give a test an image data folder and a download into it, as deployed."""
    data_folder = tmp_path / "data"
    monkeypatch.setenv("POLICYENGINE_DATA_FOLDER", str(data_folder))
    monkeypatch.setenv("STAGE12_BUNDLE_MANIFEST_SHA256", BUNDLE_MANIFEST_SHA256)

    def fetch(manifest: dict) -> None:
        monkeypatch.setenv(
            "STAGE12_CACHE_MANIFEST_DIGEST", keys.canonical_digest(manifest)
        )
        fetch_stage12_cache(
            BUCKET,
            manifest,
            EXPECTED_CONTENTS,
            client=published.inputs.cache_bucket,
        )

    return SimpleNamespace(data_folder=data_folder, fetch=fetch)


def _files_under(folder: Path) -> list[Path]:
    return sorted(path for path in folder.rglob("*") if path.is_file())


def test_downloads_the_published_manifest_of_the_plan(published, image):
    image.fetch(published.manifest)

    h5_files = [
        path for path in _files_under(image.data_folder) if path.suffix == ".h5"
    ]
    assert len(h5_files) == 3 + 60 + 153
    copy = (image.data_folder / MANIFEST_COPY).read_bytes()
    assert copy == _canonical_bytes(published.manifest)
    assert sha256(copy).hexdigest() == keys.canonical_digest(published.manifest)


def _with_years(manifest: dict, years: list[int]) -> dict:
    return {**manifest, "years": years}


def _with_an_extra_artifact(manifest: dict, kind: str) -> dict:
    template = next(item for item in manifest["artifacts"] if item["type"] == kind)
    extra = {**template, "filename": f"extra-{template['filename']}", "year": 2028}
    return {**manifest, "artifacts": [*manifest["artifacts"], extra]}


def _without_the_first_artifact(manifest: dict, kind: str) -> dict:
    first = next(item for item in manifest["artifacts"] if item["type"] == kind)
    return {
        **manifest,
        "artifacts": [item for item in manifest["artifacts"] if item is not first],
    }


@pytest.mark.parametrize(
    ("change", "message"),
    [
        pytest.param(
            lambda manifest: _with_years(manifest, [2025, 2026, 2027]),
            "^Stage 12 cache manifest has an unexpected year set$",
            id="same-years-in-another-order",
        ),
        pytest.param(
            lambda manifest: _with_years(manifest, [2026, 2027, 2025, 2028]),
            "^Stage 12 cache manifest has an unexpected year set$",
            id="a-fourth-year",
        ),
        pytest.param(
            lambda manifest: _with_an_extra_artifact(manifest, "dataset"),
            "^Stage 12 cache manifest has an unexpected artifact count$",
            id="four-year-files",
        ),
        pytest.param(
            lambda manifest: _without_the_first_artifact(manifest, "dataset"),
            "^Stage 12 cache manifest has an unexpected artifact count$",
            id="two-year-files",
        ),
        pytest.param(
            lambda manifest: _with_an_extra_artifact(manifest, "baseline"),
            "^Stage 12 cache manifest has an unexpected artifact count$",
            id="sixty-one-baselines",
        ),
    ],
)
def test_refuses_a_manifest_with_other_counts_or_years_before_downloading(
    published, image, change, message
):
    with pytest.raises(RuntimeError, match=message):
        image.fetch(change(published.manifest))

    assert not image.data_folder.exists() or _files_under(image.data_folder) == []


def test_refuses_a_file_of_another_size_and_leaves_none_of_it(published, image):
    baseline = published.plan.baselines[0]
    bucket = published.inputs.cache_bucket
    original = bucket.objects[baseline.path]
    bucket.objects[baseline.path] = original + b"-longer"
    try:
        with pytest.raises(
            RuntimeError, match=r"^Stage 12 cache artifact size differs$"
        ):
            image.fetch(published.manifest)
    finally:
        bucket.objects[baseline.path] = original

    assert not [
        path
        for path in _files_under(image.data_folder)
        if path.name.startswith(baseline.filename)
    ]
    assert not (image.data_folder / MANIFEST_COPY).exists()


class TestDeployTimeCheck:
    """``_check_installed_cache``: what a deployed US worker requires of its image."""

    def test_expects_the_planned_counts_and_years(self, published):
        from policyengine_simulation_executor import stage12_worker_validation
        from policyengine_simulation_executor.stage12_cache import (
            precompute_planning,
        )

        # main held these as STAGE12_CACHE_ARTIFACT_COUNT (63) and
        # STAGE12_CACHE_PROBE_YEAR (2026).
        assert precompute_planning.expected_cache_contents("us") == (EXPECTED_CONTENTS)
        assert stage12_worker_validation.expected_state_dataset_count() == 153

    def test_accepts_the_downloaded_cache_and_reports_its_release(
        self, published, image, monkeypatch
    ):
        from policyengine_simulation_executor import stage12_worker_validation

        image.fetch(published.manifest)
        probes = []

        def calculate(simulation):
            probes.append(simulation)
            return SimpleNamespace(cache_outcome="hit")

        monkeypatch.setattr(
            stage12_worker_validation, "calculate_simulation_frames", calculate
        )
        digest = keys.canonical_digest(published.manifest)

        summary = stage12_worker_validation._check_installed_cache(digest)

        assert summary == {
            "manifest_sha256": digest,
            "partition_sha256": (
                "25f8ed5bbcbe25592433b8bda46d73a4597f960f79ae8058f4eef33ed1e0e615"
            ),
            "years": [2026, 2027, 2025],
            "segment_count": 20,
            "dataset_count": 3,
            "baseline_count": 60,
        }
        (probe,) = probes
        assert probe.year == 2026
        assert probe.options == {"region_group": list(US_REGION_GROUPS[0])}


class TestImageFolders:
    """Where the download step and the deploy-time check find each file.

    Until 2026-11-27 (``main`` at 6865dc2c) year files and baselines landed in
    the data folder itself and state files in ``state-datasets/<dataset>/
    <state>``. Every file now lands in its handler's ``prebuilt/…`` folder.
    """

    def test_year_files_and_baselines_land_in_the_national_folder(
        self, published, image
    ):
        image.fetch(published.manifest)

        for entry in (*published.plan.datasets, *published.plan.baselines):
            destination = image.data_folder / NATIONAL_FOLDER / entry.filename
            assert destination.read_bytes() == _stored_bytes(entry.path)
        assert not list(image.data_folder.glob("*.h5"))

    def test_state_files_land_in_their_state_folders(self, published, image):
        image.fetch(published.manifest)

        for entry in published.plan.state_datasets:
            destination = (
                image.data_folder
                / "prebuilt"
                / "us"
                / STATE_LEVEL_DATASET
                / entry.state_code
                / entry.filename
            )
            assert destination.read_bytes() == _stored_bytes(entry.path)

    def test_the_deploy_check_reads_its_probe_baseline_from_the_national_folder(
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
