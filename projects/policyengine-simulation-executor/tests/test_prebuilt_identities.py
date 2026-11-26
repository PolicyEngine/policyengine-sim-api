"""Golden tests of the run_simulation app's prebuilt-file identities.

Every year file has one ``yf1`` identity, whatever its dataset level, and
every baseline one ``bl3`` identity nesting its year file's digest. The
expected digests are SHA-256 hashes of the payloads in the 1.6 · 1 design,
section 2.2, computed from those payloads alone. A change to any of them
rotates every stored key, so it must be deliberate.
"""

from __future__ import annotations

import pytest
from fixtures.identity_stubs import install_identity_stubs
from fixtures.wrapper_spm import wrapper_storage_id
from policyengine.core.scoping_strategy import RowFilterStrategy
from policyengine_simulation_executor import artifact_keys
from policyengine_simulation_executor.stage12_cache.levels import (
    BaselineScope,
    DatasetLevel,
    SourceFileTarget,
    SubsetFilter,
    YearFileTarget,
)

PACKAGES = {
    "policyengine": "4.22.0",
    "policyengine-core": "3.33.3",
    "policyengine-us": "9.9.9",
}
NATIONAL_PAYLOAD = {
    "schema": "yf1",
    "country": "us",
    "dataset": "populace_cps",
    "dataset_sha256": "d00dfeed" * 8,
    "data_version": "1.2.3",
    "data_artifact_revision": "rev-abc",
    "certification_fingerprint": "fp-123",
    "year": 2026,
    "subset": None,
    "packages": PACKAGES,
    "spm": None,
}
NATIONAL_DIGEST = "56982f3075d99409fc40edca6e1b9265feef6442a2c881fe2962d865db806f55"
UTAH_DIGEST = "9dea291f0277414d0ef6703007e3ca4213cb685b2d693388a37f114fa90b19f6"
NATIONAL_BASELINE_DIGEST = (
    "48380a609a4e26de1ed026e587f1feb69797c06f35de82e024d3c8d10f9bac1e"
)
CALIFORNIA_GROUP_BASELINE_DIGEST = (
    "ef3ab6b4dce1d62a796e1410f82c985c73133b69358aacac50eb046f2c5af4da"
)
SOURCE_FILE_DIGEST = "1994a44b4e38bc9f862602a4614ee509e81442e4cd4f3e973083dbcf36ff9b47"


@pytest.fixture
def identity_sources(monkeypatch):
    return install_identity_stubs(monkeypatch)


def _national_target(year=2026):
    return YearFileTarget(
        country="us",
        level=DatasetLevel.NATIONAL,
        dataset="populace_cps",
        year=year,
        subset=None,
        folder="prebuilt/us/populace_cps/all",
    )


def _utah_target():
    return YearFileTarget(
        country="us",
        level=DatasetLevel.STATE,
        dataset="populace_cps_local",
        year=2026,
        subset=SubsetFilter(
            region="state/ut",
            code="UT",
            variable="state_fips",
            value=49,
            scoping_strategy=RowFilterStrategy(
                variable_name="state_fips", variable_value=49
            ),
        ),
        folder="prebuilt/us/populace_cps_local/UT",
    )


def test_schema_labels():
    assert artifact_keys.YEAR_FILE_KEY_SCHEMA == "yf1"
    assert artifact_keys.BASELINE_KEY_SCHEMA == "bl3"
    assert artifact_keys.SOURCE_FILE_KEY_SCHEMA == "sf1"


@pytest.mark.usefixtures("identity_sources")
class TestYearFileIdentity:
    def test_whole_dataset_payload_and_digest(self):
        identity = artifact_keys.year_file_identity(_national_target())

        assert identity.payload() == NATIONAL_PAYLOAD
        assert identity.digest == NATIONAL_DIGEST
        assert identity.filename == "populace_cps_year_2026.h5"
        assert identity.folder == "prebuilt/us/populace_cps/all"
        assert identity.store_path == (
            f"year-files/us/{NATIONAL_DIGEST}/populace_cps_year_2026.h5"
        )

    def test_subset_payload_and_digest(self):
        identity = artifact_keys.year_file_identity(_utah_target())

        assert identity.payload() == {
            **NATIONAL_PAYLOAD,
            "dataset": "populace_cps_local",
            "dataset_sha256": "c0ffee00" * 8,
            "subset": {"region": "state/ut", "variable": "state_fips", "value": 49},
        }
        assert identity.digest == UTAH_DIGEST
        assert identity.filename == "populace_cps_local_year_2026.h5"
        assert identity.store_path == (
            f"year-files/us/{UTAH_DIGEST}/populace_cps_local_year_2026.h5"
        )

    def test_a_dataset_without_a_certified_hash_keys_a_null(self, identity_sources):
        identity_sources.release_datasets.pop("populace_cps")

        identity = artifact_keys.year_file_identity(_national_target())

        assert identity.payload()["dataset_sha256"] is None
        assert identity.digest != NATIONAL_DIGEST

    @pytest.mark.parametrize(
        ("field", "replacement"),
        [
            ("data_version", "1.2.4"),
            ("data_artifact_revision", "rev-other"),
            ("model_version", "9.9.10"),
            ("policyengine_version", "4.22.1"),
        ],
    )
    def test_bundle_versions_rotate_the_digest(
        self, identity_sources, field, replacement
    ):
        setattr(identity_sources.bundle, field, replacement)

        assert artifact_keys.year_file_identity(_national_target()).digest != (
            NATIONAL_DIGEST
        )

    def test_the_core_version_rotates_the_digest(self, identity_sources):
        identity_sources.core_version = "3.34.0"

        identity = artifact_keys.year_file_identity(_national_target())

        assert identity.payload()["packages"]["policyengine-core"] == "3.34.0"
        assert identity.digest != NATIONAL_DIGEST

    def test_the_year_rotates_the_digest(self):
        assert artifact_keys.year_file_identity(_national_target(2027)).digest != (
            NATIONAL_DIGEST
        )


@pytest.mark.usefixtures("identity_sources")
class TestBaselineIdentity:
    def test_national_payload_digest_and_names(self):
        year_file = artifact_keys.year_file_identity(_national_target())

        identity = artifact_keys.baseline_identity(
            year_file, BaselineScope(region="national", scope_key=None)
        )

        assert identity.payload() == {
            "schema": "bl3",
            "year_file": NATIONAL_DIGEST,
            "region": "national",
            "scope_key": None,
            "policy": "current-law",
            "spm": None,
        }
        assert identity.digest == NATIONAL_BASELINE_DIGEST
        assert identity.simulation_id == f"bl3-{NATIONAL_BASELINE_DIGEST[:16]}"
        assert identity.storage_id == identity.simulation_id
        assert identity.store_path == (
            f"baselines/us/{NATIONAL_BASELINE_DIGEST}/{identity.storage_id}.h5"
        )
        assert identity.country == "us"
        assert identity.year_file == year_file

    def test_region_group_payload_and_digest(self):
        year_file = artifact_keys.year_file_identity(_national_target())

        identity = artifact_keys.baseline_identity(
            year_file,
            BaselineScope(
                region="region_group/state/ca",
                scope_key="region_group:row_filter:state_fips=6",
            ),
        )

        assert identity.digest == CALIFORNIA_GROUP_BASELINE_DIGEST

    def test_a_year_file_change_rotates_the_baseline(self, identity_sources):
        identity_sources.core_version = "3.34.0"
        year_file = artifact_keys.year_file_identity(_national_target())

        identity = artifact_keys.baseline_identity(
            year_file, BaselineScope(region="national", scope_key=None)
        )

        assert identity.payload()["year_file"] == year_file.digest
        assert identity.digest != NATIONAL_BASELINE_DIGEST

    def test_an_spm_selection_names_the_storage_id(self):
        selection = {
            "forecast_content_sha256": "a" * 64,
            "scenario": "ce_trend",
            "geography_kind": "national",
            "geography_id": None,
            "county_vintage": "2020",
            "as_of": None,
        }
        year_file = artifact_keys.year_file_identity(_national_target())

        identity = artifact_keys.BaselineIdentity(
            year_file=year_file, region="national", scope_key=None, spm=selection
        )

        assert identity.payload()["spm"] == selection
        assert identity.simulation_id.startswith("bl3-")
        assert identity.storage_id == wrapper_storage_id(
            identity.simulation_id, selection
        )
        assert identity.store_path.endswith(f"/{identity.storage_id}.h5")


def test_source_file_payload_digest_and_path():
    identity = artifact_keys.SourceFileIdentity(
        country="uk",
        dataset="microcosm_uk_2024_25_dense",
        dataset_sha256="ab" * 32,
        filename="microcosm_uk_2024_25_dense.h5",
        folder="prebuilt/uk/microcosm_uk_2024_25_dense/all",
    )

    assert identity.payload() == {
        "schema": "sf1",
        "country": "uk",
        "dataset": "microcosm_uk_2024_25_dense",
        "dataset_sha256": "ab" * 32,
    }
    assert identity.digest == SOURCE_FILE_DIGEST
    assert identity.store_path == (
        f"sources/uk/{'ab' * 32}/microcosm_uk_2024_25_dense.h5"
    )


def test_a_source_file_needs_a_certified_hash(monkeypatch):
    install_identity_stubs(monkeypatch)

    with pytest.raises(ValueError, match="certified"):
        artifact_keys.source_file_identity(
            SourceFileTarget(
                country="us",
                level=DatasetLevel.NATIONAL,
                dataset="not_in_the_release",
                folder="prebuilt/us/not_in_the_release/all",
            )
        )


def test_manifest_and_deployed_marker_paths():
    assert artifact_keys.manifest_path("m" * 64) == f"manifests/{'m' * 64}.json"
    assert artifact_keys.deployed_marker_path("prod") == "deployed/prod.json"
