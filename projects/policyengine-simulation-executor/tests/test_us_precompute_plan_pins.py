"""Pins of the US precompute plan, and of the keys and store paths it uses.

Two kinds of pin, for the fixed inputs in ``fixtures.us_prebuilt_inputs``:

* **What is built**, recorded from ``main`` on 2026-11-25 (commit 544ff628)
  before the level handlers: the years, the 20 groups of states in order, the
  51 states sorted by code, each file's dataset, year, state and region, and
  which entries ``select_work`` picks. These must not change.
* **How it is named** — every digest, store path, simulation ID and image
  folder. The approved 1.6 · 1 design (section 2) replaced the ``ds1``,
  ``sds1`` and ``bl1`` keys with ``yf1`` and ``bl3`` and moved image files to
  ``prebuilt/``, so these values were recorded from the new code, and pin
  that layout from here on.

Long lists are pinned by the SHA-256 of their canonical JSON (``_digest_of``)
plus their first entries in full, so a failure names the section that moved.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from fixtures.us_prebuilt_inputs import (
    US_STATE_FIPS,
    enable_fixed_spm_capability,
    install_us_prebuilt_inputs,
)
from policyengine_simulation_executor import artifact_keys, precompute
from policyengine_simulation_executor.precompute_models import BundleVersionIdentity

if TYPE_CHECKING:
    from policyengine_simulation_executor.precompute_models import PrecomputePlan

BUCKET = "bucket-pins"
PLANNED_YEARS = [2026, 2027, 2025]

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
STATE_CODES = sorted(state_code for state_code, _fips in US_STATE_FIPS)

# Recorded from the new code on 2026-11-26 for the fixed inputs.
RECORDED_NATIONAL_ROWS = [
    [
        2026,
        "56982f3075d99409fc40edca6e1b9265feef6442a2c881fe2962d865db806f55",
        "year-files/us/56982f3075d99409fc40edca6e1b9265feef6442a2c881fe2962d865db806f55/populace_cps_year_2026.h5",
        "populace_cps_year_2026.h5",
        "prebuilt/us/populace_cps/all",
    ],
    [
        2027,
        "a220e74587d1eecb46e395d6a6a71423f5494f6eee298b8c5658eadaa28cd786",
        "year-files/us/a220e74587d1eecb46e395d6a6a71423f5494f6eee298b8c5658eadaa28cd786/populace_cps_year_2027.h5",
        "populace_cps_year_2027.h5",
        "prebuilt/us/populace_cps/all",
    ],
    [
        2025,
        "c7de61261d1ddd0fffe81153bb7604a1690aad7b9041b5a7aaa6be740b21eefa",
        "year-files/us/c7de61261d1ddd0fffe81153bb7604a1690aad7b9041b5a7aaa6be740b21eefa/populace_cps_year_2025.h5",
        "populace_cps_year_2025.h5",
        "prebuilt/us/populace_cps/all",
    ],
]
RECORDED_FIRST_STATE = [
    2026,
    "AK",
    "state/ak",
    "populace_cps_local",
    "305094a4f28987777a645186f82d63e241ac509df152bd91b4eb45c2cdbf8e2f",
    "year-files/us/305094a4f28987777a645186f82d63e241ac509df152bd91b4eb45c2cdbf8e2f/populace_cps_local_year_2026.h5",
    "populace_cps_local_year_2026.h5",
    "prebuilt/us/populace_cps_local/AK",
]
RECORDED_STATES_DIGEST = (
    "831e5d6d2d23002da88592cc25d6fa5b25c0e4e3530864ead465cd3726d80afe"
)
RECORDED_FIRST_BASELINE = [
    2026,
    ["state/hi", "state/ia", "state/wi"],
    "region_group/state/hi+state/ia+state/wi",
    "a58482ed86d0ff70024ed01b7a3d35ab0c4feef23d4dd01d1d5d2ff1b67aa9f5",
    "baselines/us/a58482ed86d0ff70024ed01b7a3d35ab0c4feef23d4dd01d1d5d2ff1b67aa9f5/bl3-a58482ed86d0ff70.h5",
    "bl3-a58482ed86d0ff70",
    "bl3-a58482ed86d0ff70",
    "year-files/us/56982f3075d99409fc40edca6e1b9265feef6442a2c881fe2962d865db806f55/populace_cps_year_2026.h5",
    "prebuilt/us/populace_cps/all",
]
RECORDED_BASELINES_DIGEST = (
    "39d0efc104687538bc8373996a33cbb8ff7da7beb44efc54326c93119687ccd4"
)
RECORDED_SPM_NATIONAL_ROWS = [
    [
        2026,
        "8370022a270fea66460788e1f282dbabc281a82fb1864ae9eed57ecb8be96186",
        "year-files/us/8370022a270fea66460788e1f282dbabc281a82fb1864ae9eed57ecb8be96186/populace_cps_year_2026.h5",
        "populace_cps_year_2026.h5",
        "prebuilt/us/populace_cps/all",
    ],
    [
        2027,
        "7a4fd448280a04398768d1f3860902847a17048ae65fb2f25997094f1f2841dd",
        "year-files/us/7a4fd448280a04398768d1f3860902847a17048ae65fb2f25997094f1f2841dd/populace_cps_year_2027.h5",
        "populace_cps_year_2027.h5",
        "prebuilt/us/populace_cps/all",
    ],
    [
        2025,
        "f15bd311ac06202000ca60537dcc5403f67793da52d364b45271c5a8a7744561",
        "year-files/us/f15bd311ac06202000ca60537dcc5403f67793da52d364b45271c5a8a7744561/populace_cps_year_2025.h5",
        "populace_cps_year_2025.h5",
        "prebuilt/us/populace_cps/all",
    ],
]
RECORDED_SPM_FIRST_STATE = [
    2026,
    "AK",
    "state/ak",
    "populace_cps_local",
    "52fa02df9d44edafdd94bf984bb1e44d9c03522f0d928252e0465220e06adfbe",
    "year-files/us/52fa02df9d44edafdd94bf984bb1e44d9c03522f0d928252e0465220e06adfbe/populace_cps_local_year_2026.h5",
    "populace_cps_local_year_2026.h5",
    "prebuilt/us/populace_cps_local/AK",
]
RECORDED_SPM_STATES_DIGEST = (
    "0f0ad34015581add61ba29f23bd9b1c5931ddebcc55719d7306870e5dc10ca4b"
)
RECORDED_SPM_FIRST_BASELINE = [
    2026,
    ["state/hi", "state/ia", "state/wi"],
    "region_group/state/hi+state/ia+state/wi",
    "dca23ffa8f9b5872e827326f17d1844f01a7c423a651b4fb12fce0b293874648",
    "baselines/us/dca23ffa8f9b5872e827326f17d1844f01a7c423a651b4fb12fce0b293874648/bl3-dca23ffa8f9b5872-spm-7396bf5f4876c42bb6cba0f9533478098edc0861cc3657bd0d60f88dbb26ac39.h5",
    "bl3-dca23ffa8f9b5872",
    "bl3-dca23ffa8f9b5872-spm-7396bf5f4876c42bb6cba0f9533478098edc0861cc3657bd0d60f88dbb26ac39",
    "year-files/us/8370022a270fea66460788e1f282dbabc281a82fb1864ae9eed57ecb8be96186/populace_cps_year_2026.h5",
    "prebuilt/us/populace_cps/all",
]
RECORDED_SPM_BASELINES_DIGEST = (
    "92ed00e683059d0d81c8b6678f83046fbd5bfd8f48414fe4c39f01d67bb97d53"
)


def _digest_of(entries: list) -> str:
    return artifact_keys.canonical_digest({"entries": entries})


def _national_rows(plan: PrecomputePlan) -> list[list]:
    return [
        [entry.year, entry.digest, entry.path, entry.filename, entry.folder]
        for entry in plan.year_files
        if entry.subset_code is None
    ]


def _state_rows(plan: PrecomputePlan) -> list[list]:
    return [
        [
            entry.year,
            entry.subset_code,
            entry.region,
            entry.dataset,
            entry.digest,
            entry.path,
            entry.filename,
            entry.folder,
        ]
        for entry in plan.year_files
        if entry.subset_code is not None
    ]


def _baseline_rows(plan: PrecomputePlan) -> list[list]:
    return [
        [
            entry.year,
            entry.request["region_group"],
            entry.region,
            entry.digest,
            entry.path,
            entry.simulation_id,
            entry.storage_id,
            entry.year_file_path,
            entry.folder,
        ]
        for entry in plan.baselines
    ]


@pytest.fixture
def us_inputs(monkeypatch):
    return install_us_prebuilt_inputs(monkeypatch)


@pytest.fixture
def us_plan(us_inputs):
    return precompute.plan_artifacts_impl(BUCKET, "us")


@pytest.fixture
def us_spm_plan(monkeypatch, us_inputs):
    enable_fixed_spm_capability(monkeypatch, us_inputs)
    return precompute.plan_artifacts_impl(BUCKET, "us")


class TestWhatTheUsPlanBuilds:
    """Unchanged from ``main`` before the handlers."""

    def test_the_default_dataset_year_file_for_each_year(self, us_plan):
        national = [entry for entry in us_plan.year_files if entry.subset_code is None]

        assert [(entry.year, entry.dataset) for entry in national] == [
            (year, "populace_cps") for year in PLANNED_YEARS
        ]
        assert [entry.filename for entry in national] == [
            f"populace_cps_year_{year}.h5" for year in PLANNED_YEARS
        ]

    def test_one_baseline_per_year_and_region_group_in_order(self, us_plan):
        assert [
            [entry.year, entry.request["region_group"]] for entry in us_plan.baselines
        ] == [[year, group] for year in PLANNED_YEARS for group in US_REGION_GROUPS]
        assert [entry.region for entry in us_plan.baselines] == [
            "region_group/" + "+".join(sorted(group))
            for _year in PLANNED_YEARS
            for group in US_REGION_GROUPS
        ]

    def test_a_state_level_year_file_per_year_and_state(self, us_plan):
        states = [entry for entry in us_plan.year_files if entry.subset_code]

        assert [[entry.year, entry.subset_code] for entry in states] == [
            [year, state_code] for year in PLANNED_YEARS for state_code in STATE_CODES
        ]
        for entry in states:
            assert entry.region == f"state/{entry.subset_code.lower()}"
            assert entry.dataset == "populace_cps_local"
            assert entry.filename == f"populace_cps_local_year_{entry.year}.h5"

    def test_records_the_bundle_version_identity(self, us_plan):
        assert us_plan.receipt == BundleVersionIdentity(
            policyengine_version="4.22.0",
            policyengine_core_version="3.33.3",
            model_version="9.9.9",
            data_version="1.2.3",
            data_artifact_revision="rev-abc",
            default_dataset="populace_cps",
        )


class TestUsPlanNamesWithoutSpm:
    """``yf1`` and ``bl3`` names for a bundle without an SPM capability."""

    def test_national_year_files(self, us_plan):
        assert _national_rows(us_plan) == RECORDED_NATIONAL_ROWS

    def test_every_baseline_identity(self, us_plan):
        rows = _baseline_rows(us_plan)

        assert rows[0] == RECORDED_FIRST_BASELINE
        assert _digest_of(rows) == RECORDED_BASELINES_DIGEST

    def test_every_state_level_year_file_identity(self, us_plan):
        rows = _state_rows(us_plan)

        assert rows[0] == RECORDED_FIRST_STATE
        assert _digest_of(rows) == RECORDED_STATES_DIGEST


class TestUsPlanNamesWithSpm:
    """With an SPM capability, as production's bundle has, every key carries it.

    Each baseline file is named by its simulation ID plus the SPM digest.
    """

    def test_national_year_files(self, us_spm_plan):
        assert _national_rows(us_spm_plan) == RECORDED_SPM_NATIONAL_ROWS

    def test_every_baseline_identity(self, us_spm_plan):
        rows = _baseline_rows(us_spm_plan)

        assert rows[0] == RECORDED_SPM_FIRST_BASELINE
        assert _digest_of(rows) == RECORDED_SPM_BASELINES_DIGEST

    def test_every_state_level_year_file_identity(self, us_spm_plan):
        rows = _state_rows(us_spm_plan)

        assert rows[0] == RECORDED_SPM_FIRST_STATE
        assert _digest_of(rows) == RECORDED_SPM_STATES_DIGEST


class TestUsWorkSelection:
    """Which plan entries a run computes, from the store's presence checks."""

    @pytest.fixture
    def plan_with_some_objects_stored(self, us_inputs):
        first_plan = precompute.plan_artifacts_impl(BUCKET, "us")
        first_state = next(
            entry for entry in first_plan.year_files if entry.subset_code
        )
        stored_paths = [
            first_plan.year_files[0].path,
            first_plan.baselines[0].path,
            first_state.path,
        ]
        us_inputs.gcs_client.objects.update(dict.fromkeys(stored_paths, b"stored"))
        return precompute.plan_artifacts_impl(BUCKET, "us")

    def test_an_empty_store_selects_every_entry(self, us_plan):
        work = precompute.select_work(us_plan, force=False)

        assert len([entry for entry in work.year_files if not entry.subset_code]) == 3
        assert len(work.baselines) == 60
        assert len([entry for entry in work.year_files if entry.subset_code]) == 153

    def test_stored_objects_are_not_selected(self, plan_with_some_objects_stored):
        plan = plan_with_some_objects_stored
        work = precompute.select_work(plan, force=False)
        stored = [entry for entry in plan.year_files if entry.exists]

        assert [(entry.year, entry.subset_code) for entry in stored] == [
            (2026, None),
            (2026, "AK"),
        ]
        assert [
            entry.year for entry in work.year_files if entry.subset_code is None
        ] == [2027, 2025]
        assert work.baselines == plan.baselines[1:]
        assert len([entry for entry in work.year_files if entry.subset_code]) == 152

    def test_force_selects_stored_objects_too(self, plan_with_some_objects_stored):
        plan = plan_with_some_objects_stored
        work = precompute.select_work(plan, force=True)

        assert work.year_files == plan.year_files
        assert work.baselines == plan.baselines


class TestKeysAndStorePaths:
    """Key schema labels and the store layout every path follows."""

    def test_key_schema_labels(self):
        assert artifact_keys.YEAR_FILE_KEY_SCHEMA == "yf1"
        assert artifact_keys.BASELINE_KEY_SCHEMA == "bl3"
        assert artifact_keys.SOURCE_FILE_KEY_SCHEMA == "sf1"
        assert precompute.MANIFEST_SCHEMA == "mf2"

    def test_artifact_store_paths(self):
        assert (
            artifact_keys.year_file_artifact_path(
                "US", "y" * 64, "populace_cps_year_2026.h5"
            )
            == f"year-files/us/{'y' * 64}/populace_cps_year_2026.h5"
        )
        assert (
            artifact_keys.baseline_artifact_path("US", "b" * 64, "bl3-0123456789abcdef")
            == f"baselines/us/{'b' * 64}/bl3-0123456789abcdef.h5"
        )
        assert (
            artifact_keys.source_file_artifact_path("UK", "s" * 64, "dense.h5")
            == f"sources/uk/{'s' * 64}/dense.h5"
        )

    def test_manifest_and_deployed_marker_paths(self):
        assert artifact_keys.manifest_path("m" * 64) == f"manifests/{'m' * 64}.json"
        assert artifact_keys.deployed_marker_path("prod") == "deployed/prod.json"
        assert artifact_keys.deployed_marker_path("beta") == "deployed/beta.json"
