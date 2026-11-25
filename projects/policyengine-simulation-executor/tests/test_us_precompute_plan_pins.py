"""Pins of the US precompute plan, and of the artifact keys and store paths it uses.

Every expected value below is what ``main`` produced on 2026-11-25 (commit
544ff628) for the fixed inputs in ``fixtures.us_prebuilt_inputs``. Moving the
US precompute onto handlers that take a country and a dataset level must leave
each value unchanged. A deliberate change to a key, path or digest updates its
pinned value in a commit of its own that says why.

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


def _digest_of(entries: list) -> str:
    return artifact_keys.canonical_digest({"entries": entries})


def _dataset_rows(plan: PrecomputePlan) -> list[list]:
    return [
        [entry.year, entry.digest, entry.path, entry.filename]
        for entry in plan.datasets
    ]


def _baseline_rows(plan: PrecomputePlan) -> list[list]:
    return [
        [
            entry.year,
            entry.group,
            entry.region,
            entry.digest,
            entry.path,
            entry.simulation_id,
        ]
        for entry in plan.baselines
    ]


def _state_dataset_rows(plan: PrecomputePlan) -> list[list]:
    return [
        [
            entry.year,
            entry.state_code,
            entry.region,
            entry.dataset,
            entry.digest,
            entry.path,
            entry.filename,
            entry.folder,
        ]
        for entry in plan.state_datasets
    ]


@pytest.fixture
def us_inputs(monkeypatch):
    return install_us_prebuilt_inputs(monkeypatch)


@pytest.fixture
def us_plan(us_inputs):
    return precompute.plan_artifacts_impl(BUCKET)


@pytest.fixture
def us_spm_plan(monkeypatch, us_inputs):
    enable_fixed_spm_capability(monkeypatch, us_inputs)
    return precompute.plan_artifacts_impl(BUCKET)


class TestUsPlanWithoutSpm:
    """The plan for a bundle without an SPM capability: ``ds1``, ``sds1``, ``bl1``."""

    def test_plans_the_default_dataset_year_file_for_each_year(self, us_plan):
        assert _dataset_rows(us_plan) == [
            [
                2026,
                "183ffe49d74669b653fb408b0b7298128ba7c6e990f4479487712a5d79553be3",
                "datasets/us/183ffe49d74669b653fb408b0b7298128ba7c6e990f4479487712a5d79553be3/populace_cps_year_2026.h5",
                "populace_cps_year_2026.h5",
            ],
            [
                2027,
                "64a0c6a3a3b389dcad8b7033b6cfe1e7c8fbe9dfc742f09d0fc48950cfb002b6",
                "datasets/us/64a0c6a3a3b389dcad8b7033b6cfe1e7c8fbe9dfc742f09d0fc48950cfb002b6/populace_cps_year_2027.h5",
                "populace_cps_year_2027.h5",
            ],
            [
                2025,
                "bfcf4b0f6e5481542d78cd49419e18de88afa84c0acb265446457f7e73179f6a",
                "datasets/us/bfcf4b0f6e5481542d78cd49419e18de88afa84c0acb265446457f7e73179f6a/populace_cps_year_2025.h5",
                "populace_cps_year_2025.h5",
            ],
        ]

    def test_plans_one_baseline_per_year_and_region_group_in_order(self, us_plan):
        assert [[entry.year, entry.group] for entry in us_plan.baselines] == [
            [year, group] for year in PLANNED_YEARS for group in US_REGION_GROUPS
        ]
        assert [entry.region for entry in us_plan.baselines] == [
            "region_group/" + "+".join(sorted(group))
            for _year in PLANNED_YEARS
            for group in US_REGION_GROUPS
        ]

    def test_pins_every_baseline_identity(self, us_plan):
        rows = _baseline_rows(us_plan)
        assert rows[0] == [
            2026,
            ["state/hi", "state/ia", "state/wi"],
            "region_group/state/hi+state/ia+state/wi",
            "10aba908c2d8c18bc763f689f7603a6f364064c54b3ee4fb2081905d6e702c86",
            "baselines/us/10aba908c2d8c18bc763f689f7603a6f364064c54b3ee4fb2081905d6e702c86/bl1-10aba908c2d8c18b.h5",
            "bl1-10aba908c2d8c18b",
        ]
        assert _digest_of(rows) == (
            "0b510eba0f56fdd108c73ae6545be4b14ba28753b5623f23e502d9c8a10edd5c"
        )

    def test_plans_a_state_level_year_file_per_year_and_state(self, us_plan):
        state_codes = sorted(state_code for state_code, _fips in US_STATE_FIPS)
        assert [[entry.year, entry.state_code] for entry in us_plan.state_datasets] == [
            [year, state_code] for year in PLANNED_YEARS for state_code in state_codes
        ]
        for entry in us_plan.state_datasets:
            assert entry.region == f"state/{entry.state_code.lower()}"
            assert entry.dataset == "populace_cps_local"
            assert entry.filename == f"populace_cps_local_year_{entry.year}.h5"
            assert (
                entry.folder == f"state-datasets/populace_cps_local/{entry.state_code}"
            )

    def test_pins_every_state_level_year_file_identity(self, us_plan):
        rows = _state_dataset_rows(us_plan)
        assert rows[0] == [
            2026,
            "AK",
            "state/ak",
            "populace_cps_local",
            "246bd136a125bea87374f88fd02b16a346e1dbae88d673685c79d68e16f7347c",
            "state-datasets/us/246bd136a125bea87374f88fd02b16a346e1dbae88d673685c79d68e16f7347c/populace_cps_local_year_2026.h5",
            "populace_cps_local_year_2026.h5",
            "state-datasets/populace_cps_local/AK",
        ]
        assert _digest_of(rows) == (
            "e0642f25d7cc258caf163096fea9b3e0f2a44a1aaee0acf30d435793e7fe0cd8"
        )

    def test_records_the_bundle_version_identity(self, us_plan):
        assert us_plan.receipt == BundleVersionIdentity(
            policyengine_version="4.22.0",
            model_version="9.9.9",
            data_version="1.2.3",
            data_artifact_revision="rev-abc",
            default_dataset="populace_cps",
        )


class TestUsPlanWithSpm:
    """The plan when the bundle offers an SPM capability, as production's does.

    Dataset and baseline keys become ``ds2-spm`` and ``bl2-spm`` digests, and
    each baseline file is named by its simulation ID plus the SPM digest.
    """

    def test_plans_the_default_dataset_year_file_for_each_year(self, us_spm_plan):
        assert _dataset_rows(us_spm_plan) == [
            [
                2026,
                "5e8ac31209649187b505f4996f3507a3ca26f57bce0ef958b9b2246c6c64eceb",
                "datasets/us/5e8ac31209649187b505f4996f3507a3ca26f57bce0ef958b9b2246c6c64eceb/populace_cps_year_2026.h5",
                "populace_cps_year_2026.h5",
            ],
            [
                2027,
                "9f693cffbfd2fa982ed8234f25727c0356c472484922f53c19c628ce939404fc",
                "datasets/us/9f693cffbfd2fa982ed8234f25727c0356c472484922f53c19c628ce939404fc/populace_cps_year_2027.h5",
                "populace_cps_year_2027.h5",
            ],
            [
                2025,
                "361a8966964a290d9ed2a0155d2d800a75e4f11794797ca2751425e487a08f84",
                "datasets/us/361a8966964a290d9ed2a0155d2d800a75e4f11794797ca2751425e487a08f84/populace_cps_year_2025.h5",
                "populace_cps_year_2025.h5",
            ],
        ]

    def test_pins_every_baseline_identity(self, us_spm_plan):
        rows = _baseline_rows(us_spm_plan)
        assert rows[0] == [
            2026,
            ["state/hi", "state/ia", "state/wi"],
            "region_group/state/hi+state/ia+state/wi",
            "04d4a3a1079cb94018d7c6553d4ba30f7264dc9715e34d2554b55d9269140e13",
            "baselines/us/04d4a3a1079cb94018d7c6553d4ba30f7264dc9715e34d2554b55d9269140e13/"
            "bl1-04d4a3a1079cb940-spm-7396bf5f4876c42bb6cba0f9533478098edc0861cc3657bd0d60f88dbb26ac39.h5",
            "bl1-04d4a3a1079cb940",
        ]
        assert _digest_of(rows) == (
            "cb46ba36c198f162a4b954e115a18ba5b7ff1ce9bc83168fcdcadf1a48762243"
        )

    def test_pins_every_state_level_year_file_identity(self, us_spm_plan):
        rows = _state_dataset_rows(us_spm_plan)
        assert rows[0] == [
            2026,
            "AK",
            "state/ak",
            "populace_cps_local",
            "2917c5320f62e864add4b61b9e810c5e0b7fb9477f412b2211ac25c5963452d4",
            "state-datasets/us/2917c5320f62e864add4b61b9e810c5e0b7fb9477f412b2211ac25c5963452d4/populace_cps_local_year_2026.h5",
            "populace_cps_local_year_2026.h5",
            "state-datasets/populace_cps_local/AK",
        ]
        assert _digest_of(rows) == (
            "ee8a4eea06e4ac5fb57829945eb2400c00975b99237cc8ade9c2343c887f3686"
        )


class TestUsWorkSelection:
    """Which plan entries a run computes, from the store's presence checks."""

    @pytest.fixture
    def plan_with_some_objects_stored(self, us_inputs):
        stored_paths = [
            "datasets/us/183ffe49d74669b653fb408b0b7298128ba7c6e990f4479487712a5d79553be3/populace_cps_year_2026.h5",
            "baselines/us/10aba908c2d8c18bc763f689f7603a6f364064c54b3ee4fb2081905d6e702c86/bl1-10aba908c2d8c18b.h5",
            "state-datasets/us/246bd136a125bea87374f88fd02b16a346e1dbae88d673685c79d68e16f7347c/populace_cps_local_year_2026.h5",
        ]
        us_inputs.gcs_client.objects.update(dict.fromkeys(stored_paths, b"stored"))
        return precompute.plan_artifacts_impl(BUCKET)

    def test_an_empty_store_selects_every_entry(self, us_plan):
        work = precompute.select_work(us_plan, force=False)

        assert len(work.datasets) == 3
        assert len(work.baselines) == 60
        assert len(work.state_datasets) == 153

    def test_stored_objects_are_not_selected(self, plan_with_some_objects_stored):
        plan = plan_with_some_objects_stored
        work = precompute.select_work(plan, force=False)

        assert [entry.year for entry in work.datasets] == [2027, 2025]
        assert work.baselines == plan.baselines[1:]
        assert work.state_datasets == plan.state_datasets[1:]

    def test_force_selects_stored_objects_too(self, plan_with_some_objects_stored):
        plan = plan_with_some_objects_stored
        work = precompute.select_work(plan, force=True)

        assert work.datasets == plan.datasets
        assert work.baselines == plan.baselines
        assert work.state_datasets == plan.state_datasets


class TestKeysAndStorePaths:
    """Key schema labels and the store layout every artifact path follows."""

    def test_key_schema_labels(self):
        assert artifact_keys.DATASET_KEY_SCHEMA == "ds1"
        assert artifact_keys.STATE_DATASET_KEY_SCHEMA == "sds1"
        assert artifact_keys.BASELINE_KEY_SCHEMA == "bl1"
        assert precompute.MANIFEST_SCHEMA == "mf1"

    def test_artifact_store_paths(self):
        assert (
            artifact_keys.dataset_artifact_path("US", "d" * 64, "populace_cps", 2026)
            == f"datasets/us/{'d' * 64}/populace_cps_year_2026.h5"
        )
        assert (
            artifact_keys.state_dataset_artifact_path(
                "US", "s" * 64, "populace_cps_local", 2026
            )
            == f"state-datasets/us/{'s' * 64}/populace_cps_local_year_2026.h5"
        )
        assert (
            artifact_keys.baseline_artifact_path("US", "b" * 64, "bl1-0123456789abcdef")
            == f"baselines/us/{'b' * 64}/bl1-0123456789abcdef.h5"
        )

    def test_manifest_and_deployed_marker_paths(self):
        assert artifact_keys.manifest_path("m" * 64) == f"manifests/{'m' * 64}.json"
        assert artifact_keys.deployed_marker_path("prod") == "deployed/prod.json"
        assert artifact_keys.deployed_marker_path("beta") == "deployed/beta.json"
