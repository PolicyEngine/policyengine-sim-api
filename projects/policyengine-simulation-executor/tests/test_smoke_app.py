"""Unit tests for the executor image smoke's bundle validation."""

import importlib
import sys

import pytest

from fixtures.fake_modal import install_fake_modal


@pytest.fixture
def summarize_bundle_status(monkeypatch):
    install_fake_modal(monkeypatch)
    sys.modules.pop("src.modal.app", None)
    sys.modules.pop("src.modal.smoke_app", None)
    return importlib.import_module("src.modal.smoke_app")._summarize_bundle_status


def _passing_status() -> dict:
    package_versions = {
        "policyengine": "6.1.2",
        "policyengine-core": "3.32.5",
        "policyengine-us": "2.2.1",
        "policyengine-uk": "2.90.2",
        "spm-calculator": "1.0.0",
    }
    return {
        "matched": True,
        "bundle_version": "6.1.2",
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
                "expected_version": "populace-us-2024-spm-20260915",
                "expected_sha256": "a" * 64,
                "status": "ok",
            },
            {
                "country": "uk",
                "dataset": "enhanced_frs_2024_25",
                "expected_version": "policyengine-uk-data-1.56.16",
                "expected_sha256": "b" * 64,
                "status": "ok",
            },
        ],
        "receipt": {"countries": ["us", "uk"]},
    }


def test_bundle_status_summary_requires_complete_v6_runtime(summarize_bundle_status):
    summary = summarize_bundle_status(_passing_status())

    assert summary["bundle_version"] == "6.1.2"
    assert summary["packages"]["spm-calculator"] == "1.0.0"
    assert summary["datasets"]["uk"]["version"] == "policyengine-uk-data-1.56.16"


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
