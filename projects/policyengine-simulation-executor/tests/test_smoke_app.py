"""Unit tests for the executor image smoke's bundle validation."""

import importlib
import sys

import pandas as pd
import pytest
from fixtures.fake_modal import install_fake_modal
from policyengine_simulation_contract.uk_geography import (
    UKLocalAuthorityBoundaryVersion,
    UKLocalAuthorityMetadata,
)


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


def _uk_area_dataset_smoke(
    monkeypatch,
    smoke_module,
    *,
    installed: str,
    area_dataset: str,
    boundary_version: UKLocalAuthorityBoundaryVersion,
) -> list[str]:
    """Fake the installed default and the dataset serving UK area results."""
    observed: list[str] = []
    monkeypatch.setattr(
        smoke_module,
        "resolve_local_bundle_dataset_path",
        lambda _country, _requested_data: installed,
    )
    monkeypatch.setattr(
        smoke_module,
        "uk_area_hdf_dataset_path",
        lambda path: area_dataset if path == installed else pytest.fail(path),
    )
    monkeypatch.setattr(
        smoke_module,
        "detect_uk_area_metadata_from_hdf",
        lambda path: (
            observed.append(path)
            or UKLocalAuthorityMetadata(boundary_version=boundary_version)
        ),
    )
    return observed


def test_uk_dataset_smoke_validates_installed_codes_against_packaged_resources(
    monkeypatch,
    smoke_module,
) -> None:
    observed = _uk_area_dataset_smoke(
        monkeypatch,
        smoke_module,
        installed="/installed/enhanced_frs_2024_25.h5",
        area_dataset="/installed/enhanced_frs_2024_25.h5",
        boundary_version=UKLocalAuthorityBoundaryVersion.LAD22,
    )

    assert smoke_module._validate_installed_uk_local_authority_dataset() == "lad22"
    assert observed == ["/installed/enhanced_frs_2024_25.h5"]


def test_uk_dataset_smoke_checks_the_routed_dataset_of_a_national_default(
    monkeypatch,
    smoke_module,
) -> None:
    observed = _uk_area_dataset_smoke(
        monkeypatch,
        smoke_module,
        installed="/installed/microcosm_uk_2024_25.h5",
        area_dataset="/installed/microcosm_uk_2024_25_dense.h5",
        boundary_version=UKLocalAuthorityBoundaryVersion.LAD23,
    )

    assert smoke_module._validate_installed_uk_local_authority_dataset() == "lad23"
    assert observed == ["/installed/microcosm_uk_2024_25_dense.h5"]


_AREA_OUTPUT_COLUMNS = (
    "constituency_code_oa",
    "la_code_oa",
    "region_code_oa",
    "source_household_id",
)


def write_area_household_hdf(path, *, without: str | None = None) -> str:
    """Write a household table with the area-output columns, less ``without``."""
    household = {
        "household_id": [1, 2],
        "constituency_code_oa": ["E14001063", "W07000112"],
        "la_code_oa": ["E06000063", "W06000001"],
        "region_code_oa": ["SOUTH_EAST", "WALES"],
        "source_household_id": [1, 2],
    }
    household.pop(without or "", None)
    pd.DataFrame(household).to_hdf(
        path, key="household", format="table", data_columns=True
    )
    return str(path)


def _routed_area_smoke(monkeypatch, smoke_module, tmp_path, *, without):
    installed = str(tmp_path / "microcosm_uk_2024_25.h5")
    routed = write_area_household_hdf(
        tmp_path / "microcosm_uk_2024_25_dense.h5", without=without
    )
    monkeypatch.setattr(
        smoke_module,
        "resolve_local_bundle_dataset_path",
        lambda _country, _requested_data: installed,
    )
    monkeypatch.setattr(
        smoke_module,
        "uk_area_hdf_dataset_path",
        lambda path: routed if path == installed else pytest.fail(path),
    )


@pytest.mark.parametrize("missing", _AREA_OUTPUT_COLUMNS)
def test_uk_dataset_smoke_requires_every_area_output_column(
    monkeypatch, smoke_module, tmp_path, missing
) -> None:
    _routed_area_smoke(monkeypatch, smoke_module, tmp_path, without=missing)

    with pytest.raises(ValueError, match=f"has no {missing} column"):
        smoke_module._validate_installed_uk_local_authority_dataset()


def test_uk_dataset_smoke_accepts_a_routed_dataset_with_every_column(
    monkeypatch, smoke_module, tmp_path
) -> None:
    _routed_area_smoke(monkeypatch, smoke_module, tmp_path, without=None)

    assert smoke_module._validate_installed_uk_local_authority_dataset() == "lad23"


def test_executor_smoke_can_download_a_routed_private_dataset(smoke_module) -> None:
    functions = dict(smoke_module.app.function_calls)

    assert functions["smoke_import_executor"]["secrets"] == [smoke_module.hf_secret]
