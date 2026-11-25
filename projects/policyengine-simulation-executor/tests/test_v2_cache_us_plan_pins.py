"""Pins of the v2 report app's US cache plan, and of the keys and store paths it uses.

Every expected value below is what ``main`` produced on 2026-11-25 (commit
6865dc2c) for the fixed inputs in ``fixtures.v2_cache_us_inputs``. Moving the
v2 cache (``stage12_cache``) onto the level handlers must leave each value
unchanged: the same years, year files, state files and region-group
baselines in the same order, with the same ``stage12-ds1``, ``stage12-sds1``
and ``stage12-bl1`` keys and store paths.

The image folders a plan entry names are pinned apart, in
``TestImageFoldersOnMain``: the handler design moves them to ``prebuilt/…``,
so those pins change in a commit of their own. Any other deliberate change to
a key, path or digest updates its pinned value in a commit of its own that
says why.

Long lists are pinned by the SHA-256 of their canonical JSON (``_digest_of``)
plus their first entries in full, so a failure names the section that moved.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from fixtures.v2_cache_us_inputs import (
    STATE_LEVEL_DATASET,
    US_STATE_FIPS,
    enable_fixed_spm,
    install_v2_cache_us_inputs,
)
from policyengine_simulation_executor.stage12_cache import keys, precompute
from policyengine_simulation_executor.stage12_cache.models import CacheBundleIdentity
from policyengine_simulation_executor.stage12_runtime.partition import (
    US_REGION_GROUPS as PARTITION_GROUPS,
)

if TYPE_CHECKING:
    from policyengine_simulation_executor.stage12_cache.models import PrecomputePlan

BUCKET = "bucket-pins"
PLANNED_YEARS = [2026, 2027, 2025]
FIXED_SPM_SELECTION = {
    "forecast_content_sha256": "a" * 64,
    "scenario": "ce_trend",
    "geography_kind": "national",
    "geography_id": None,
    "county_vintage": "2020",
    "as_of": None,
}
SPM_SUFFIX = "-spm-7396bf5f4876c42bb6cba0f9533478098edc0861cc3657bd0d60f88dbb26ac39"
# The output plan a report run's worker stores a current-law US run under.
OUTPUT_PLAN_SHA256 = "baeec697c2c3b88895014ccd3cdb62fd44b3c3c8a16e77658820f6fcf03a0c04"

# The household-balanced partition of segmented national runs, in run order.
US_REGION_GROUPS = [
    ["state/hi", "state/ia", "state/wi"],
    ["state/ak", "state/ga", "state/la"],
    ["state/al", "state/co", "state/ri"],
    ["state/mn", "state/mo", "state/nh"],
    ["state/az", "state/de", "state/me"],
    ["state/ny", "state/wv"],
    ["state/in", "state/ky", "state/md"],
    ["state/mt", "state/sc", "state/wa"],
    ["state/id", "state/or", "state/tn"],
    ["state/ar", "state/nc", "state/nd"],
    ["state/dc", "state/ks", "state/mi"],
    ["state/ct", "state/oh", "state/vt"],
    ["state/nv", "state/va", "state/wy"],
    ["state/pa", "state/ut"],
    ["state/sd", "state/tx"],
    ["state/ca"],
    ["state/il", "state/ms"],
    ["state/fl", "state/nm"],
    ["state/ne", "state/nj"],
    ["state/ma", "state/ok"],
]


def _digest_of(entries: list) -> str:
    return keys.canonical_digest({"entries": entries})


def _dataset_rows(plan: PrecomputePlan) -> list[list]:
    return [
        [entry.year, entry.digest, entry.path, entry.filename]
        for entry in plan.datasets
    ]


def _baseline_rows(plan: PrecomputePlan) -> list[list]:
    return [
        [
            entry.year,
            entry.segment_index,
            list(entry.region_codes),
            entry.digest,
            entry.path,
            entry.filename,
            entry.simulation_id,
            entry.output_plan_sha256,
        ]
        for entry in plan.baselines
    ]


def _state_dataset_rows(plan: PrecomputePlan) -> list[list]:
    """Each state file's identity, without the image folder it lands in."""
    return [
        [
            entry.year,
            entry.state_code,
            entry.region,
            entry.dataset,
            entry.digest,
            entry.path,
            entry.filename,
        ]
        for entry in plan.state_datasets
    ]


@pytest.fixture(scope="module")
def us_inputs():
    with pytest.MonkeyPatch.context() as module_patch:
        inputs = install_v2_cache_us_inputs(module_patch)
        enable_fixed_spm(inputs)
        yield inputs


@pytest.fixture(scope="module")
def us_plan(us_inputs):
    return precompute.plan_artifacts_impl(BUCKET)


class TestUsPlan:
    """The plan ``plan_artifacts_impl`` makes for a bundle with an SPM capability."""

    def test_plans_the_three_years_in_submission_order(self, us_plan):
        assert list(us_plan.years) == PLANNED_YEARS
        assert list(precompute.PRECOMPUTE_YEARS) == PLANNED_YEARS

    def test_plans_the_default_dataset_year_file_for_each_year(self, us_plan):
        assert _dataset_rows(us_plan) == [
            [
                2026,
                "e4a6dacf5954ccac03abfbcba50c44a3a156d351ed25e3214ae20d7e093e0dac",
                "datasets/us/e4a6dacf5954ccac03abfbcba50c44a3a156d351ed25e3214ae20d7e093e0dac/populace_us_2024_year_2026.h5",
                "populace_us_2024_year_2026.h5",
            ],
            [
                2027,
                "d7bc670e1f294b2a29edc94c2ba5e5c1faf85fedcc6d2330b2b326da3198261f",
                "datasets/us/d7bc670e1f294b2a29edc94c2ba5e5c1faf85fedcc6d2330b2b326da3198261f/populace_us_2024_year_2027.h5",
                "populace_us_2024_year_2027.h5",
            ],
            [
                2025,
                "76d63c9ad2b18986cf3bc23f7e21b73ccea3f90390757c69f08b0a9deb135a1a",
                "datasets/us/76d63c9ad2b18986cf3bc23f7e21b73ccea3f90390757c69f08b0a9deb135a1a/populace_us_2024_year_2025.h5",
                "populace_us_2024_year_2025.h5",
            ],
        ]

    def test_plans_one_baseline_per_year_and_region_group_in_order(self, us_plan):
        assert [list(group) for group in PARTITION_GROUPS] == US_REGION_GROUPS
        assert [
            [entry.year, entry.segment_index, list(entry.region_codes)]
            for entry in us_plan.baselines
        ] == [
            [year, index, group]
            for year in PLANNED_YEARS
            for index, group in enumerate(US_REGION_GROUPS)
        ]

    def test_every_baseline_is_keyed_on_the_stored_output_plan(self, us_plan):
        assert {entry.output_plan_sha256 for entry in us_plan.baselines} == {
            OUTPUT_PLAN_SHA256
        }

    def test_pins_every_baseline_identity(self, us_plan):
        rows = _baseline_rows(us_plan)
        assert rows[0] == [
            2026,
            0,
            ["state/hi", "state/ia", "state/wi"],
            "88acdd57481da78416475d199be783bb06732a81094dd3658518ad028e0502bd",
            "baselines/us/88acdd57481da78416475d199be783bb06732a81094dd3658518ad028e0502bd/"
            f"stage12-bl1-88acdd57481da784{SPM_SUFFIX}.h5",
            f"stage12-bl1-88acdd57481da784{SPM_SUFFIX}.h5",
            "stage12-bl1-88acdd57481da784",
            OUTPUT_PLAN_SHA256,
        ]
        assert rows[-1] == [
            2025,
            19,
            ["state/ma", "state/ok"],
            "f4135fce4cc7e0c580912d3d3d2614bd425276611045a8d4c2dd2df18487f2a1",
            "baselines/us/f4135fce4cc7e0c580912d3d3d2614bd425276611045a8d4c2dd2df18487f2a1/"
            f"stage12-bl1-f4135fce4cc7e0c5{SPM_SUFFIX}.h5",
            f"stage12-bl1-f4135fce4cc7e0c5{SPM_SUFFIX}.h5",
            "stage12-bl1-f4135fce4cc7e0c5",
            OUTPUT_PLAN_SHA256,
        ]
        assert _digest_of(rows) == (
            "e4975f0601fe92ff5b418fd4f9d0a9e625c5c3b2ae35c4570daf6ea554c0d1e4"
        )

    def test_plans_a_state_level_year_file_per_year_and_state(self, us_plan):
        state_codes = sorted(state_code for state_code, _fips in US_STATE_FIPS)
        assert len(us_plan.state_datasets) == 153
        assert [[entry.year, entry.state_code] for entry in us_plan.state_datasets] == [
            [year, state_code] for year in PLANNED_YEARS for state_code in state_codes
        ]
        for entry in us_plan.state_datasets:
            assert entry.region == f"state/{entry.state_code.lower()}"
            assert entry.dataset == STATE_LEVEL_DATASET
            assert entry.filename == f"{STATE_LEVEL_DATASET}_year_{entry.year}.h5"

    def test_pins_every_state_level_year_file_identity(self, us_plan):
        rows = _state_dataset_rows(us_plan)
        assert rows[0] == [
            2026,
            "AK",
            "state/ak",
            STATE_LEVEL_DATASET,
            "be2d28af31a275139be0a74a47d02582ca4f6cbfa10d956c3f3c4f227113bbbc",
            "state-datasets/us/be2d28af31a275139be0a74a47d02582ca4f6cbfa10d956c3f3c4f227113bbbc/"
            f"{STATE_LEVEL_DATASET}_year_2026.h5",
            f"{STATE_LEVEL_DATASET}_year_2026.h5",
        ]
        assert _digest_of(rows) == (
            "e3184033236f38090b3b2d0de6aaa115a10647795bd8ea5e44d16998f2da84aa"
        )

    def test_records_the_installed_bundle_and_partition(self, us_plan):
        assert us_plan.bundle == CacheBundleIdentity(
            bundle_manifest_sha256="b" * 64,
            policyengine_version="6.20.0",
            core_package_version="3.33.3",
            country_package_version="2.2.2",
            data_package_version="1.4.0",
            data_artifact_revision="rev-default",
            default_dataset="populace_us_2024",
        )
        assert us_plan.partition_sha256 == (
            "25f8ed5bbcbe25592433b8bda46d73a4597f960f79ae8058f4eef33ed1e0e615"
        )
        assert us_plan.determinism_verified is False
        assert us_plan.schema_version == 1

    def test_names_the_determinism_record_by_the_planned_baselines(self, us_plan):
        assert precompute.determinism_record(us_plan.baselines) == {
            "schema": "stage12-dv1",
            "baselines": [entry.digest for entry in us_plan.baselines],
        }
        assert precompute.determinism_record_path(us_plan.baselines) == (
            "determinism/us/"
            "63f3756f4db271f26d4ed34ac49fb10609e00b05f2630b6e7cd7acc24f68a2aa.json"
        )

    def test_selects_every_entry_of_a_cold_plan_in_plan_order(self, us_plan):
        work = precompute.select_work(us_plan)

        assert work.datasets == us_plan.datasets
        assert work.baselines == us_plan.baselines
        assert work.state_datasets == us_plan.state_datasets


class TestStateTargets:
    """The US states whose requests load a non-default dataset."""

    def test_targets_every_state_by_code_with_its_row_filter(self, us_inputs):
        targets = precompute.state_dataset_targets()

        assert [
            [
                target.region,
                target.state_code,
                target.state_fips,
                target.filter_variable,
                target.dataset,
            ]
            for target in targets
        ] == [
            [
                f"state/{state_code.lower()}",
                state_code,
                state_fips,
                "state_fips",
                STATE_LEVEL_DATASET,
            ]
            for state_code, state_fips in sorted(US_STATE_FIPS)
        ]
        assert all(target.scoping_strategy is not None for target in targets)


def test_a_plan_marks_what_the_bucket_already_holds(monkeypatch):
    """Each entry records its own presence; a stored record marks the plan verified."""
    inputs = install_v2_cache_us_inputs(monkeypatch)
    enable_fixed_spm(inputs)
    cold = precompute.plan_artifacts_impl(BUCKET)
    stored_paths = [
        cold.datasets[1].path,
        cold.baselines[21].path,
        cold.state_datasets[100].path,
        precompute.determinism_record_path(cold.baselines),
    ]
    for path in stored_paths:
        inputs.cache_bucket.store(path, b"stored")

    warm = precompute.plan_artifacts_impl(BUCKET)
    work = precompute.select_work(warm)

    assert [entry.exists for entry in warm.datasets] == [False, True, False]
    assert [index for index, entry in enumerate(warm.baselines) if entry.exists] == [21]
    assert [
        index for index, entry in enumerate(warm.state_datasets) if entry.exists
    ] == [100]
    assert warm.determinism_verified is True
    assert len(work.datasets) == 2
    assert cold.baselines[21] not in work.baselines
    assert len(work.baselines) == 59
    assert len(work.state_datasets) == 152


class TestKeys:
    """The ``stage12-ds1``, ``stage12-sds1`` and ``stage12-bl1`` keys and paths."""

    def test_schema_labels(self):
        assert keys.DATASET_SCHEMA == "stage12-ds1"
        assert keys.STATE_DATASET_SCHEMA == "stage12-sds1"
        assert keys.BASELINE_SCHEMA == "stage12-bl1"
        assert keys.CURRENT_LAW_POLICY == "current-law"

    def test_year_file_key(self, us_inputs, us_plan):
        identity = keys.collect_dataset_identity(
            us_inputs.stage12_bundle, country="us", year=2026
        )

        assert identity.payload == {
            "schema": "stage12-ds1",
            "country": "us",
            "dataset": "populace_us_2024",
            "year": 2026,
            "source_sha256": "1" * 64,
            "data_artifact_revision": "rev-default",
            "data_package_version": "1.4.0",
            "country_package_version": "2.2.2",
            "core_package_version": "3.33.3",
            "policyengine_version": "6.20.0",
            "bundle_manifest_sha256": "b" * 64,
        }
        assert identity.digest == us_plan.datasets[0].digest
        assert identity.store_path == (
            f"datasets/us/{identity.digest}/populace_us_2024_year_2026.h5"
        )

    def test_state_file_key(self, us_inputs):
        identity = keys.collect_state_dataset_identity(
            us_inputs.stage12_bundle,
            country="us",
            year=2026,
            dataset=STATE_LEVEL_DATASET,
            state_code="ca",
            state_fips=6,
            filter_variable="state_fips",
        )

        assert identity.payload == {
            "schema": "stage12-sds1",
            "country": "us",
            "dataset": STATE_LEVEL_DATASET,
            "year": 2026,
            "source_sha256": "2" * 64,
            "data_artifact_revision": "rev-local",
            "data_package_version": "1.4.0",
            "country_package_version": "2.2.2",
            "core_package_version": "3.33.3",
            "policyengine_version": "6.20.0",
            "bundle_manifest_sha256": "b" * 64,
            "state_code": "CA",
            "state_fips": 6,
            "filter_variable": "state_fips",
        }
        assert identity.digest == (
            "b22f793c5604bf27d05e39be7d37ffab7d7142c908106241889ddcdc2ac5a83e"
        )
        assert identity.store_path == (
            f"state-datasets/us/{identity.digest}/{STATE_LEVEL_DATASET}_year_2026.h5"
        )

    def test_region_group_baseline_key(self, us_inputs, us_plan):
        from policyengine_simulation_executor.stage12_cache import precompute_planning

        _simulation, identity = precompute_planning.baseline_identity(
            2026, tuple(US_REGION_GROUPS[0])
        )

        assert identity.payload == {
            "schema": "stage12-bl1",
            "country": "us",
            "dataset_digest": us_plan.datasets[0].digest,
            "region_codes": ["state/hi", "state/ia", "state/wi"],
            "scope_key": (
                "region_group:row_filter:state_fips=15"
                "|row_filter:state_fips=19|row_filter:state_fips=55"
            ),
            "output_plan_sha256": OUTPUT_PLAN_SHA256,
            "spm": FIXED_SPM_SELECTION,
            "policy": "current-law",
        }
        assert identity.digest == us_plan.baselines[0].digest
        assert identity.simulation_id == "stage12-bl1-88acdd57481da784"
        # .py names a US baseline's file by its ID and its SPM selection.
        assert identity.storage_id == f"stage12-bl1-88acdd57481da784{SPM_SUFFIX}"
        assert f"-spm-{keys.canonical_digest(FIXED_SPM_SELECTION)}" == SPM_SUFFIX
        assert identity.store_path == (
            f"baselines/us/{identity.digest}/{identity.storage_id}.h5"
        )


class TestImageFoldersOnMain:
    """Where ``main`` places each planned file in the worker image.

    The handler design moves these folders to ``prebuilt/…``; these pins
    change in that commit, and only there.
    """

    def test_state_files_land_in_their_state_folders(self, us_plan):
        assert [entry.folder for entry in us_plan.state_datasets] == [
            f"state-datasets/{STATE_LEVEL_DATASET}/{entry.state_code}"
            for entry in us_plan.state_datasets
        ]

    def test_year_files_and_baselines_name_no_folder(self, us_plan):
        # They land in the image data folder itself (see the download pins).
        assert all(
            "folder" not in type(entry).model_fields
            for entry in (*us_plan.datasets, *us_plan.baselines)
        )
