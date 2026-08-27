"""Regression tests for versions exported from PolicyEngine.py's bundle."""

from types import SimpleNamespace

import pytest
from policyengine_simulation_executor.release_bundle import (
    get_bundled_package_version,
)
from src.modal.utils import extract_bundle_versions


def test_bundled_package_version_uses_policyengine_manifest(monkeypatch):
    monkeypatch.setattr(
        "policyengine_simulation_executor.release_bundle._current_policyengine_bundle",
        lambda: {
            "packages": {
                "policyengine-core": {
                    "version": "9.9.9",
                }
            }
        },
    )

    assert get_bundled_package_version("policyengine-core") == "9.9.9"


def test_bundled_package_version_rejects_missing_package(monkeypatch):
    monkeypatch.setattr(
        "policyengine_simulation_executor.release_bundle._current_policyengine_bundle",
        lambda: {"packages": {}},
    )

    with pytest.raises(TypeError, match="no metadata for package"):
        get_bundled_package_version("policyengine-core")


def test_version_export_reads_package_versions_from_policyengine_bundle(monkeypatch):
    bundles = {
        "us": SimpleNamespace(
            data_version="1.10.0",
        ),
        "uk": SimpleNamespace(
            data_version="1.20.0",
        ),
    }
    package_versions = {
        "policyengine": "4.1.0",
        "policyengine-core": "9.9.9",
        "policyengine-us": "1.1.0",
        "policyengine-uk": "2.1.0",
    }
    requested_packages = []

    monkeypatch.setattr(
        extract_bundle_versions,
        "get_country_release_bundle",
        lambda country: bundles[country],
    )
    monkeypatch.setattr(
        extract_bundle_versions,
        "get_bundled_package_version",
        lambda package: requested_packages.append(package) or package_versions[package],
    )

    outputs = extract_bundle_versions._bundle_outputs()

    assert requested_packages == [
        "policyengine",
        "policyengine-core",
        "policyengine-us",
        "policyengine-uk",
    ]
    assert outputs == {
        "policyengine_version": "4.1.0",
        "policyengine_core_version": "9.9.9",
        "us_version": "1.1.0",
        "us_data_version": "1.10.0",
        "uk_version": "2.1.0",
        "uk_data_version": "1.20.0",
    }
