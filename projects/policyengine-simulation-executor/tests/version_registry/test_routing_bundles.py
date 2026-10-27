"""Tests for the bundle manifest a deployed app's routes record."""

from __future__ import annotations

import pytest
from src.modal.routing_publisher import bundles as routing_bundles
from src.modal.utils import update_version_registry as registry


class _StubForecast:
    years = (2026,)

    def entry(self, year, *, scenario, as_of):
        return {"year": year, "scenario": scenario, "as_of": as_of}


@pytest.mark.usefixtures("fake_bundle_metadata")
def test_bundle_manifest_records_policyengine_6_spm_capability(
    monkeypatch,
):
    from policyengine.bundle import get_current_bundle
    from policyengine_simulation_contract.spm import SPM_CONTRACT_VERSION
    from policyengine_simulation_executor import spm as executor_spm

    monkeypatch.setattr(
        executor_spm,
        "_forecast",
        lambda _expected_sha256: _StubForecast(),
    )

    bundle = get_current_bundle()
    metadata = registry.build_bundle_manifest_metadata(
        app_name="policyengine-simulation-py6-1-2",
        policyengine_version=bundle["policyengine_version"],
    )

    assert metadata["spm"] == {
        "contract_version": SPM_CONTRACT_VERSION,
        "defaults": bundle["measurements"]["spm"],
    }


@pytest.mark.usefixtures("fake_bundle_metadata")
def test_bundle_manifest_declares_job_progress_capability(monkeypatch):
    from policyengine_simulation_contract.job_progress import (
        JOB_PROGRESS_CAPABILITY_VERSION,
    )
    from policyengine_simulation_executor import spm as executor_spm

    monkeypatch.setattr(executor_spm, "runtime_spm_capability", lambda: None)

    metadata = registry.build_bundle_manifest_metadata(
        app_name="policyengine-simulation-py6-2-7",
        policyengine_version="6.2.7",
    )

    # The gateway reports a queued stage only for workers that declare this.
    assert metadata["job_progress"] == JOB_PROGRESS_CAPABILITY_VERSION


_NATIONAL_FILE = "microcosm_uk_2024_25.h5"
_LOCAL_AREA_FILE = "microcosm_uk_2024_25_dense.h5"


def _uk_release_manifest(*, release_kind, constituency_file, local_authority_file):
    from types import SimpleNamespace

    return SimpleNamespace(
        regional_release_kind=release_kind,
        region_datasets={
            "national": SimpleNamespace(path_template=_NATIONAL_FILE),
            "constituency": SimpleNamespace(path_template=constituency_file),
            "local_authority": SimpleNamespace(path_template=local_authority_file),
        },
        datasets={
            "microcosm_uk_2024_25": SimpleNamespace(path=_NATIONAL_FILE),
            "microcosm_uk_2024_25_dense": SimpleNamespace(path=_LOCAL_AREA_FILE),
        },
    )


@pytest.mark.usefixtures("fake_bundle_metadata")
@pytest.mark.parametrize(
    ("release_kind", "constituency_file", "local_authority_file", "declared"),
    [
        ("local_area", _LOCAL_AREA_FILE, _LOCAL_AREA_FILE, True),
        # Routes both area types but registers no area regions in .py.
        ("area_coded_national", _NATIONAL_FILE, _NATIONAL_FILE, False),
        # A breakdown needs both levels on one dataset.
        ("local_area", _LOCAL_AREA_FILE, _NATIONAL_FILE, False),
        (None, _LOCAL_AREA_FILE, _LOCAL_AREA_FILE, False),
    ],
)
def test_bundle_manifest_declares_local_area_breakdowns_only_for_one_local_area_dataset(
    monkeypatch, release_kind, constituency_file, local_authority_file, *, declared
):
    from policyengine_simulation_contract.local_area_breakdown import (
        LOCAL_AREA_BREAKDOWN_CAPABILITY_KEY,
        LOCAL_AREA_BREAKDOWN_CAPABILITY_VERSION,
    )
    from policyengine_simulation_executor import spm as executor_spm

    monkeypatch.setattr(executor_spm, "runtime_spm_capability", lambda: None)
    manifest = _uk_release_manifest(
        release_kind=release_kind,
        constituency_file=constituency_file,
        local_authority_file=local_authority_file,
    )
    monkeypatch.setattr(
        "policyengine.provenance.manifest.get_release_manifest",
        lambda _country: manifest,
    )

    metadata = registry.build_bundle_manifest_metadata(
        app_name="policyengine-simulation-py6-10-0",
        policyengine_version="6.10.0",
    )

    # A breakdown runs on the one local-area dataset the bundle routes both
    # area types to; the gateway sends breakdowns only to workers declaring it.
    if declared:
        assert metadata[LOCAL_AREA_BREAKDOWN_CAPABILITY_KEY] == (
            LOCAL_AREA_BREAKDOWN_CAPABILITY_VERSION
        )
    else:
        assert LOCAL_AREA_BREAKDOWN_CAPABILITY_KEY not in metadata


def test_country_bundle_metadata_records_the_certified_dataset_sha256(monkeypatch):
    from types import SimpleNamespace

    bundle = SimpleNamespace(
        country="uk",
        model_package_name="policyengine-uk",
        model_version="2.126.8",
        data_package_name="policyengine-uk-data",
        data_package_version="1.58.0",
        data_version="policyengine-uk-data-1.58.0",
        data_artifact_revision="1.58.0",
        default_dataset="enhanced_frs_2024_25",
        default_dataset_uri="hf://policyengine/policyengine-uk-data-private/enhanced_frs_2024_25.h5@1.58.0",
        dataset_uris={},
        dataset_repo_types={},
        default_dataset_sha256="a" * 64,
    )
    monkeypatch.setattr(
        "policyengine_simulation_executor.release_bundle.get_country_release_bundle",
        lambda _country: bundle,
    )

    metadata = routing_bundles._country_bundle_metadata("uk")

    assert metadata["default_dataset_sha256"] == "a" * 64
