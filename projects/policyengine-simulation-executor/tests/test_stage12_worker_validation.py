"""Tests for bounded Stage 12 deployed-worker validation."""

from __future__ import annotations

from hashlib import sha256
from types import SimpleNamespace

import pytest

from policyengine_simulation_executor.stage12_bundle import load_stage12_bundle
from policyengine_simulation_executor.stage12_worker_validation import (
    _run_non_serving_calculation,
    validate_country_worker,
)


def _environment() -> dict[str, str]:
    return {
        "HF_TOKEN": "test-token",
        "GOOGLE_APPLICATION_CREDENTIALS_JSON": "{}",
        "STAGE12_DATABASE_URL": "postgresql://runtime:secret@db.example/postgres",
        "STAGE12_ARTIFACT_BUCKET": "policyengine-stage12-staging",
    }


def test_validation_checks_dataset_and_non_serving_calculation() -> None:
    resolved = load_stage12_bundle()
    datasets: list[tuple[str, str]] = []
    countries: list[str] = []

    result = validate_country_worker(
        country="us",
        expected_bundle_manifest_sha256=resolved.bundle_manifest_sha256,
        environment=_environment(),
        dataset_path_resolver=lambda _: "/installed/populace_us_2024.h5",
        dataset_check=lambda path, digest: datasets.append((path, digest)),
        calculation_check=countries.append,
    )

    assert result["validated"] is True
    assert result["bundle_manifest_sha256"] == resolved.bundle_manifest_sha256
    country_bundle = next(
        item for item in resolved.bundle.countries if item.country == "us"
    )
    expected_dataset = next(
        dataset
        for dataset in country_bundle.datasets
        if dataset.identity == country_bundle.default_dataset
    )
    assert datasets == [("/installed/populace_us_2024.h5", expected_dataset.sha256)]
    assert countries == ["us"]


def test_non_serving_us_calculation_supports_national_spm() -> None:
    """The deployed-worker check must not invent a county for its synthetic household."""
    _run_non_serving_calculation("us")


def test_non_serving_us_calculation_selects_national_without_county(
    monkeypatch,
) -> None:
    calls: list[dict] = []

    def calculate_household(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(household={"household_net_income": 1})

    monkeypatch.setattr(
        "policyengine_simulation_executor.stage12_worker_validation.import_module",
        lambda _: SimpleNamespace(calculate_household=calculate_household),
    )

    _run_non_serving_calculation("us")

    assert calls[0]["spm"] == {"geography_kind": "national"}
    assert "household" not in calls[0]


def test_uk_validation_loads_packaged_local_authority_resources() -> None:
    resolved = load_stage12_bundle()
    checks: list[str] = []

    result = validate_country_worker(
        country="uk",
        expected_bundle_manifest_sha256=resolved.bundle_manifest_sha256,
        environment=_environment(),
        dataset_path_resolver=lambda _: "/installed/enhanced_frs_2024_25.h5",
        dataset_check=lambda *_: None,
        calculation_check=lambda _: None,
        local_authority_resource_check=lambda: checks.append("resources-loaded"),
    )

    assert result["validated"] is True
    assert checks == ["resources-loaded"]


def test_validation_rejects_digest_mismatch_before_dataset_access() -> None:
    accessed: list[str] = []
    with pytest.raises(RuntimeError, match="digest differs"):
        validate_country_worker(
            country="us",
            expected_bundle_manifest_sha256="0" * 64,
            environment=_environment(),
            dataset_path_resolver=lambda _: "/installed/populace_us_2024.h5",
            dataset_check=lambda path, _: accessed.append(path),
            calculation_check=lambda _: None,
        )
    assert accessed == []


@pytest.mark.parametrize(
    "environment",
    [
        {
            "GOOGLE_APPLICATION_CREDENTIALS_JSON": "{}",
            "STAGE12_DATABASE_URL": "postgresql://runtime:secret@db.example/postgres",
            "STAGE12_ARTIFACT_BUCKET": "policyengine-stage12-staging",
        },
        {
            "HF_TOKEN": "test-token",
            "STAGE12_DATABASE_URL": "postgresql://runtime:secret@db.example/postgres",
            "STAGE12_ARTIFACT_BUCKET": "policyengine-stage12-staging",
        },
        {
            "HF_TOKEN": "test-token",
            "GOOGLE_APPLICATION_CREDENTIALS_JSON": "{}",
            "STAGE12_ARTIFACT_BUCKET": "policyengine-stage12-staging",
        },
        {
            "HF_TOKEN": "test-token",
            "GOOGLE_APPLICATION_CREDENTIALS_JSON": "{}",
            "STAGE12_DATABASE_URL": "postgresql://runtime:secret@db.example/postgres",
        },
    ],
)
def test_validation_rejects_missing_secret_values(environment) -> None:
    resolved = load_stage12_bundle()
    with pytest.raises(RuntimeError, match="missing required secret"):
        validate_country_worker(
            country="us",
            expected_bundle_manifest_sha256=resolved.bundle_manifest_sha256,
            environment=environment,
            dataset_path_resolver=lambda _: "/installed/populace_us_2024.h5",
            dataset_check=lambda *_: None,
            calculation_check=lambda _: None,
        )


def test_validation_rejects_missing_installed_dataset() -> None:
    resolved = load_stage12_bundle()
    with pytest.raises(RuntimeError, match="installed certified dataset"):
        validate_country_worker(
            country="us",
            expected_bundle_manifest_sha256=resolved.bundle_manifest_sha256,
            environment=_environment(),
            dataset_path_resolver=lambda _: None,
            dataset_check=lambda *_: None,
            calculation_check=lambda _: None,
        )


def test_dataset_check_verifies_installed_content(tmp_path) -> None:
    from policyengine_simulation_executor.stage12_worker_validation import (
        _check_dataset_access,
    )

    dataset = tmp_path / "dataset.h5"
    dataset.write_bytes(b"certified-data")
    digest = sha256(b"certified-data").hexdigest()

    _check_dataset_access(str(dataset), digest)

    with pytest.raises(RuntimeError, match="digest differs"):
        _check_dataset_access(str(dataset), "0" * 64)
