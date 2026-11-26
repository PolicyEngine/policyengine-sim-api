"""The per-country precompute plan through the level handlers, and its mf2 manifest.

``plan_artifacts_impl(bucket, country)`` asks each level handler of the
country's settings for its year files and baselines, keys each with the
run_simulation app's identities, and checks the store for each. The US plan
lists the same files as before the handlers: 3 national year files, 153 state
year files and 60 group baselines. The manifest lists every planned file with
the SHA-256 and size the store recorded for it.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter

import pytest
from fixtures.us_prebuilt_inputs import US_STATE_FIPS, install_us_prebuilt_inputs
from policyengine_simulation_executor import artifact_keys, precompute
from policyengine_simulation_executor import simulation_runtime as runtime
from policyengine_simulation_executor.artifact_store import ArtifactStore
from policyengine_simulation_executor.baseline_artifacts import (
    BaselineIdentityRequest,
    deterministic_baseline_id,
)
from policyengine_simulation_executor.precompute_models import (
    BaselinePlanEntry,
    BundleVersionIdentity,
    PrecomputePlan,
    YearFilePlanEntry,
)
from policyengine_simulation_executor.stage12_cache.levels import DatasetLevel
from policyengine_simulation_executor.stage12_runtime.partition import (
    US_REGION_GROUPS,
)

BUCKET = "bucket-plan"
YEARS = [2026, 2027, 2025]
STATE_CODES = sorted(state_code for state_code, _fips in US_STATE_FIPS)


@pytest.fixture
def inputs(monkeypatch):
    return install_us_prebuilt_inputs(monkeypatch)


@pytest.fixture
def us_plan(inputs):
    return precompute.plan_artifacts_impl(BUCKET, "us")


class TestUsPlan:
    def test_year_files_per_year_national_first_then_states(self, us_plan):
        assert us_plan.country == "us"
        assert [
            (entry.year, entry.level, entry.subset_code) for entry in us_plan.year_files
        ] == [
            row
            for year in YEARS
            for row in [
                (year, DatasetLevel.NATIONAL, None),
                *[(year, DatasetLevel.STATE, code) for code in STATE_CODES],
            ]
        ]
        assert us_plan.source_files == []

    def test_every_file_planned_before_the_handlers_is_planned_once(self, us_plan):
        before_the_handlers = Counter(
            [("populace_cps", year, None, None) for year in YEARS]
            + [
                ("populace_cps_local", year, code, f"state/{code.lower()}")
                for year in YEARS
                for code in STATE_CODES
            ]
        )

        assert (
            Counter(
                (entry.dataset, entry.year, entry.subset_code, entry.region)
                for entry in us_plan.year_files
            )
            == before_the_handlers
        )

    def test_year_file_entries_carry_their_identity(self, us_plan):
        national, utah = (
            us_plan.year_files[0],
            next(entry for entry in us_plan.year_files if entry.subset_code == "UT"),
        )

        for entry in (national, utah):
            assert entry.country == "us"
            assert entry.path == (
                f"year-files/us/{entry.digest}/{entry.dataset}_year_{entry.year}.h5"
            )
            assert entry.filename == f"{entry.dataset}_year_{entry.year}.h5"
            assert entry.exists is False
        assert national.folder == "prebuilt/us/populace_cps/all"
        assert national.region is None
        assert utah.folder == "prebuilt/us/populace_cps_local/UT"
        assert utah.region == "state/ut"

    def test_group_baselines_in_order(self, us_plan):
        national_by_year = {
            entry.year: entry
            for entry in us_plan.year_files
            if entry.level == DatasetLevel.NATIONAL
        }

        assert [
            (entry.year, entry.request["region_group"]) for entry in us_plan.baselines
        ] == [(year, list(group)) for year in YEARS for group in US_REGION_GROUPS]
        for entry in us_plan.baselines:
            national = national_by_year[entry.year]
            assert entry.level == DatasetLevel.STATE_GROUP
            assert entry.region == "region_group/" + "+".join(
                sorted(entry.request["region_group"])
            )
            assert entry.request["scope"] == "macro"
            assert entry.year_file_path == national.path
            assert entry.year_file_filename == national.filename
            assert entry.folder == national.folder
            assert entry.simulation_id.startswith("bl3-")
            assert entry.path == (f"baselines/us/{entry.digest}/{entry.storage_id}.h5")
        assert us_plan.baselines[15].label == "group 16 of 20"

    def test_the_planned_id_is_the_id_a_run_gets(self, us_plan):
        entry = us_plan.baselines[15]
        params = dict(entry.request)
        region = runtime.resolve_region(
            country_module=runtime._country_module("us"), country="us", params=params
        )
        dataset = runtime._resolve_dataset_selection(params, region_resolution=region)

        assert (
            deterministic_baseline_id(
                BaselineIdentityRequest(
                    params,
                    country="us",
                    dataset=dataset,
                    policy=None,
                    region_code=region.code,
                    scoping_strategy=region.scoping_strategy,
                    year=entry.year,
                )
            )
            == entry.simulation_id
        )

    def test_receipt(self, us_plan):
        assert us_plan.receipt == BundleVersionIdentity(
            policyengine_version="4.22.0",
            policyengine_core_version="3.33.3",
            model_version="9.9.9",
            data_version="1.2.3",
            data_artifact_revision="rev-abc",
            default_dataset="populace_cps",
        )

    def test_stored_files_are_marked_and_not_selected(self, inputs):
        first_plan = precompute.plan_artifacts_impl(BUCKET, "us")
        stored = [first_plan.year_files[0].path, first_plan.baselines[0].path]
        inputs.gcs_client.objects.update(dict.fromkeys(stored, b"stored"))

        plan = precompute.plan_artifacts_impl(BUCKET, "us")
        work = precompute.select_work(plan, force=False)

        assert [entry.exists for entry in plan.year_files[:2]] == [True, False]
        assert work.year_files == plan.year_files[1:]
        assert work.baselines == plan.baselines[1:]
        assert precompute.select_work(plan, force=True).year_files == plan.year_files

    def test_a_baseline_without_a_fixed_id_stops_the_plan(self, inputs, monkeypatch):
        from policyengine_simulation_executor import baseline_artifacts

        monkeypatch.setattr(
            baseline_artifacts,
            "qualifying_baseline_identity",
            lambda _request: None,
        )

        with pytest.raises(RuntimeError, match="diverged"):
            precompute.plan_artifacts_impl(BUCKET, "us")


@pytest.mark.usefixtures("inputs")
def test_the_uk_plan_is_empty():
    plan = precompute.plan_artifacts_impl(BUCKET, "uk")

    assert plan.country == "uk"
    assert (plan.year_files, plan.baselines, plan.source_files) == ([], [], [])
    assert plan.receipt.default_dataset == "enhanced_frs"
    assert plan.receipt.model_version == "2.0.0"


@pytest.mark.usefixtures("inputs")
def test_a_country_without_settings_is_refused():
    with pytest.raises(ValueError, match="fr"):
        precompute.plan_artifacts_impl(BUCKET, "fr")


SMALL_MANIFEST_JSON = """\
{
  "artifacts": [
    {
      "digest": "national-digest",
      "filename": "populace_cps_year_2026.h5",
      "folder": "prebuilt/us/populace_cps/all",
      "kind": "year_file",
      "level": "national",
      "path": "year-files/us/national-digest/populace_cps_year_2026.h5",
      "region": null,
      "sha256": "1111111111111111111111111111111111111111111111111111111111111111",
      "size_bytes": 100,
      "year": 2026
    },
    {
      "digest": "utah-digest",
      "filename": "populace_cps_local_year_2026.h5",
      "folder": "prebuilt/us/populace_cps_local/UT",
      "kind": "year_file",
      "level": "state",
      "path": "year-files/us/utah-digest/populace_cps_local_year_2026.h5",
      "region": "state/ut",
      "sha256": "2222222222222222222222222222222222222222222222222222222222222222",
      "size_bytes": 200,
      "year": 2026
    },
    {
      "digest": "group-digest",
      "filename": "bl3-0123456789abcdef.h5",
      "folder": "prebuilt/us/populace_cps/all",
      "kind": "baseline",
      "level": "state_group",
      "path": "baselines/us/group-digest/bl3-0123456789abcdef.h5",
      "region": "region_group/state/ca",
      "sha256": "3333333333333333333333333333333333333333333333333333333333333333",
      "size_bytes": 300,
      "year": 2026
    }
  ],
  "country": "us",
  "receipt": {
    "data_artifact_revision": "rev-abc",
    "data_version": "1.2.3",
    "default_dataset": "populace_cps",
    "model_version": "9.9.9",
    "policyengine_core_version": "3.33.3",
    "policyengine_version": "4.22.0"
  },
  "schema": "mf2"
}"""
SMALL_MANIFEST_DIGEST = (
    "17d18b695fc75de7224cec15f59e68e3390218f6b62d3718faf9c72913cb4e4b"
)


def _small_plan() -> PrecomputePlan:
    national_path = "year-files/us/national-digest/populace_cps_year_2026.h5"
    return PrecomputePlan(
        country="us",
        year_files=[
            YearFilePlanEntry(
                country="us",
                level=DatasetLevel.NATIONAL,
                year=2026,
                dataset="populace_cps",
                region=None,
                subset_code=None,
                digest="national-digest",
                path=national_path,
                filename="populace_cps_year_2026.h5",
                folder="prebuilt/us/populace_cps/all",
                exists=True,
            ),
            YearFilePlanEntry(
                country="us",
                level=DatasetLevel.STATE,
                year=2026,
                dataset="populace_cps_local",
                region="state/ut",
                subset_code="UT",
                digest="utah-digest",
                path="year-files/us/utah-digest/populace_cps_local_year_2026.h5",
                filename="populace_cps_local_year_2026.h5",
                folder="prebuilt/us/populace_cps_local/UT",
                exists=False,
            ),
        ],
        baselines=[
            BaselinePlanEntry(
                country="us",
                level=DatasetLevel.STATE_GROUP,
                year=2026,
                region="region_group/state/ca",
                label="group 16 of 20",
                request={
                    "country": "us",
                    "scope": "macro",
                    "region_group": ["state/ca"],
                    "time_period": 2026,
                },
                year_file_path=national_path,
                year_file_filename="populace_cps_year_2026.h5",
                folder="prebuilt/us/populace_cps/all",
                digest="group-digest",
                path="baselines/us/group-digest/bl3-0123456789abcdef.h5",
                simulation_id="bl3-0123456789abcdef",
                storage_id="bl3-0123456789abcdef",
                exists=False,
            )
        ],
        receipt=BundleVersionIdentity(
            policyengine_version="4.22.0",
            policyengine_core_version="3.33.3",
            model_version="9.9.9",
            data_version="1.2.3",
            data_artifact_revision="rev-abc",
            default_dataset="populace_cps",
        ),
    )


def _store_with_metadata(inputs, path, sha256, size_bytes):
    inputs.gcs_client.objects[path] = b"x" * size_bytes
    inputs.gcs_client.object_metadata[path] = {
        "sha256": sha256,
        "size_bytes": str(size_bytes),
    }


class TestManifest:
    def test_published_text_and_digest(self, inputs):
        plan = _small_plan()
        for entry, (fill, size) in zip(
            [*plan.year_files, *plan.baselines],
            [("1", 100), ("2", 200), ("3", 300)],
            strict=True,
        ):
            _store_with_metadata(inputs, entry.path, fill * 64, size)

        digest = precompute.publish_manifest_impl(BUCKET, plan)

        assert digest == SMALL_MANIFEST_DIGEST
        _bucket, path, data, keyword_arguments = inputs.gcs_client.uploads[-1]
        assert path == f"manifests/{SMALL_MANIFEST_DIGEST}.json"
        assert data.decode() == SMALL_MANIFEST_JSON
        assert keyword_arguments == {
            "content_type": "application/json",
            "if_generation_match": 0,
        }

    def test_an_object_without_recorded_metadata_is_hashed(self, inputs):
        plan = _small_plan()
        for entry in [*plan.year_files, *plan.baselines]:
            inputs.gcs_client.objects[entry.path] = entry.path.encode()

        digest = precompute.publish_manifest_impl(BUCKET, plan)

        manifest = json.loads(inputs.gcs_client.objects[f"manifests/{digest}.json"])
        first = manifest["artifacts"][0]
        assert first["sha256"] == hashlib.sha256(first["path"].encode()).hexdigest()
        assert first["size_bytes"] == len(first["path"])

    def test_a_missing_object_stops_the_publish(self, inputs):
        with pytest.raises(FileNotFoundError):
            precompute.publish_manifest_impl(BUCKET, _small_plan())
        assert inputs.gcs_client.uploads == []

    def test_the_us_manifest_lists_every_planned_file(self, inputs, us_plan):
        for entry in [*us_plan.year_files, *us_plan.baselines]:
            _store_with_metadata(inputs, entry.path, "f" * 64, 10)

        digest = precompute.publish_manifest_impl(BUCKET, us_plan)

        manifest = json.loads(inputs.gcs_client.objects[f"manifests/{digest}.json"])
        assert manifest["schema"] == "mf2"
        assert manifest["country"] == "us"
        assert manifest["receipt"] == us_plan.receipt.model_dump()
        assert [artifact["kind"] for artifact in manifest["artifacts"]] == (
            ["year_file"] * 156 + ["baseline"] * 60
        )
        assert [artifact["path"] for artifact in manifest["artifacts"]] == [
            entry.path for entry in [*us_plan.year_files, *us_plan.baselines]
        ]
        assert digest == artifact_keys.canonical_digest(manifest)

    def test_upload_records_the_files_hash_and_size(self, inputs, tmp_path):
        local_file = tmp_path / "year.h5"
        local_file.write_bytes(b"year file bytes")

        assert ArtifactStore(BUCKET).upload_file("year-files/us/d/f.h5", local_file)

        assert inputs.gcs_client.object_metadata["year-files/us/d/f.h5"] == {
            "sha256": hashlib.sha256(b"year file bytes").hexdigest(),
            "size_bytes": str(len(b"year file bytes")),
        }

    def test_a_source_file_entry_carries_its_sf1_digest(self, inputs):
        from policyengine_simulation_executor.precompute_models import (
            SourceFilePlanEntry,
        )

        plan = _small_plan().model_copy(
            update={
                "source_files": [
                    SourceFilePlanEntry(
                        country="us",
                        level=DatasetLevel.NATIONAL,
                        dataset="populace_cps_local",
                        digest="sf1-digest",
                        sha256="ab" * 32,
                        path=f"sources/us/{'ab' * 32}/populace_cps_local.h5",
                        filename="populace_cps_local.h5",
                        folder="prebuilt/us/populace_cps_local/all",
                        exists=True,
                    )
                ]
            }
        )
        for entry in [*plan.year_files, *plan.baselines, *plan.source_files]:
            _store_with_metadata(inputs, entry.path, "4" * 64, 400)

        digest = precompute.publish_manifest_impl(BUCKET, plan)

        manifest = json.loads(inputs.gcs_client.objects[f"manifests/{digest}.json"])
        source = manifest["artifacts"][-1]
        assert (source["kind"], source["digest"], source["sha256"], source["year"]) == (
            "source_file",
            "sf1-digest",
            "4" * 64,
            None,
        )

    def test_planned_source_files_are_keyed_by_their_sf1_identity(
        self, inputs, monkeypatch
    ):
        from policyengine_simulation_executor.stage12_cache import levels_us
        from policyengine_simulation_executor.stage12_cache.levels import (
            SourceFileTarget,
        )

        target = SourceFileTarget(
            country="us",
            level=DatasetLevel.NATIONAL,
            dataset="populace_cps_local",
            folder="prebuilt/us/populace_cps_local/all",
        )
        monkeypatch.setattr(
            levels_us.UsNationalHandler,
            "source_file_targets",
            lambda _handler: [target],
        )

        (entry,) = precompute.plan_artifacts_impl(BUCKET, "us").source_files

        identity = artifact_keys.source_file_identity(target)
        assert (entry.digest, entry.sha256, entry.path) == (
            identity.digest,
            identity.dataset_sha256,
            identity.store_path,
        )

    def test_the_digest_output_line(self):
        assert precompute.manifest_digest_line("abc") == "MANIFEST_DIGEST=abc"
