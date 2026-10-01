"""Unit tests for the executor image smoke's bundle validation."""

import importlib
import sys

import pytest
from policyengine_simulation_contract.uk_geography import (
    UKLocalAuthorityBoundaryVersion,
    UKLocalAuthorityMetadata,
)

from fixtures.fake_modal import install_fake_modal


@pytest.fixture
def smoke_module(monkeypatch):
    install_fake_modal(monkeypatch)
    sys.modules.pop("src.modal.app", None)
    sys.modules.pop("src.modal.smoke_app", None)
    return importlib.import_module("src.modal.smoke_app")


@pytest.fixture
def summarize_bundle_status(smoke_module):
    return smoke_module._summarize_bundle_status


def _passing_status() -> dict:
    package_versions = {
        "policyengine": "wrapper-test-version",
        "policyengine-core": "core-test-version",
        "policyengine-us": "us-test-version",
        "policyengine-uk": "uk-test-version",
        "spm-calculator": "spm-test-version",
    }
    return {
        "matched": True,
        "bundle_version": "bundle-test-version",
        "packages": [
            {
                "package": package,
                "installed_version": version,
                "status": "ok",
            }
            for package, version in package_versions.items()
        ],
        "datasets": [
            {
                "country": "us",
                "dataset": "populace_us_2024",
                "expected_version": "us-data-test-version",
                "expected_sha256": "a" * 64,
                "status": "ok",
            },
            {
                "country": "uk",
                "dataset": "enhanced_frs_2024_25",
                "expected_version": "uk-data-test-version",
                "expected_sha256": "b" * 64,
                "status": "ok",
            },
        ],
        "receipt": {"countries": ["us", "uk"]},
    }


def test_bundle_status_summary_requires_complete_v6_runtime(summarize_bundle_status):
    summary = summarize_bundle_status(_passing_status())

    assert summary["bundle_version"] == "bundle-test-version"
    assert summary["packages"]["spm-calculator"] == "spm-test-version"
    assert summary["datasets"]["uk"]["version"] == "uk-data-test-version"


@pytest.mark.parametrize(
    "mutation",
    [
        lambda status: status.update(matched=False),
        lambda status: status["packages"].pop(),
        lambda status: status["datasets"].pop(),
        lambda status: status.update(receipt=None),
    ],
)
def test_bundle_status_summary_rejects_mismatch_or_incomplete_install(
    mutation,
    summarize_bundle_status,
):
    status = _passing_status()
    mutation(status)

    with pytest.raises(RuntimeError, match="runtime bundle validation failed"):
        summarize_bundle_status(status)


def test_uk_dataset_smoke_validates_installed_codes_against_packaged_resources(
    monkeypatch,
    smoke_module,
) -> None:
    observed: list[str] = []

    monkeypatch.setattr(
        smoke_module,
        "resolve_local_bundle_dataset_path",
        lambda country, requested: "/installed/enhanced_frs_2024_25.h5",
    )
    monkeypatch.setattr(
        smoke_module,
        "detect_uk_local_authority_metadata_from_hdf",
        lambda path: observed.append(path)
        or UKLocalAuthorityMetadata(
            boundary_version=UKLocalAuthorityBoundaryVersion.LAD22
        ),
    )

    assert smoke_module._validate_installed_uk_local_authority_dataset() == "lad22"
    assert observed == ["/installed/enhanced_frs_2024_25.h5"]
