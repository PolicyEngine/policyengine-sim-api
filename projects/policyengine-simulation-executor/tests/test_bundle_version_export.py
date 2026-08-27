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


def test_version_export_reads_core_from_policyengine_bundle(monkeypatch):
    bundles = {
        "us": SimpleNamespace(
            policyengine_version="4.1.0",
            model_version="1.1.0",
            data_version="1.10.0",
        ),
        "uk": SimpleNamespace(
            policyengine_version="4.1.0",
            model_version="2.1.0",
            data_version="1.20.0",
        ),
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
        lambda package: requested_packages.append(package) or "9.9.9",
    )

    outputs = extract_bundle_versions._bundle_outputs()

    assert requested_packages == ["policyengine-core"]
    assert outputs["policyengine_core_version"] == "9.9.9"
