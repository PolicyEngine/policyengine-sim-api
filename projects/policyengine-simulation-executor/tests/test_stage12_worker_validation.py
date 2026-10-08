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
        "HUGGING_FACE_TOKEN": "test-token",
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
        local_authority_resource_check=lambda path: checks.append(path),
    )

    assert result["validated"] is True
    assert checks == ["/installed/enhanced_frs_2024_25.h5"]


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
            "HUGGING_FACE_TOKEN": "test-token",
            "STAGE12_DATABASE_URL": "postgresql://runtime:secret@db.example/postgres",
            "STAGE12_ARTIFACT_BUCKET": "policyengine-stage12-staging",
        },
        {
            "HUGGING_FACE_TOKEN": "test-token",
            "GOOGLE_APPLICATION_CREDENTIALS_JSON": "{}",
            "STAGE12_ARTIFACT_BUCKET": "policyengine-stage12-staging",
        },
        {
            "HUGGING_FACE_TOKEN": "test-token",
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


def _uk_household_hdf(tmp_path, columns: dict[str, list]) -> str:
    import pandas as pd

    dataset = tmp_path / "uk-dataset.h5"
    pd.DataFrame({"household_id": [1, 2], **columns}).to_hdf(
        dataset, key="household", format="table", data_columns=True
    )
    return str(dataset)


@pytest.mark.parametrize("routed", [False, True])
def test_uk_dataset_check_validates_codes_whenever_present(
    monkeypatch, tmp_path, routed: bool
) -> None:
    from policyengine_simulation_executor.stage12_worker_validation import (
        _check_uk_local_authority_dataset,
    )

    monkeypatch.setattr(
        "policyengine_simulation_executor.uk_local_authority_metadata."
        "uk_area_regions_routed",
        lambda: routed,
    )

    _check_uk_local_authority_dataset(
        _uk_household_hdf(tmp_path, {"la_code_oa": ["E06000001", "E06000063"]})
    )
    with pytest.raises(ValueError, match="unsupported local-authority code"):
        _check_uk_local_authority_dataset(
            _uk_household_hdf(tmp_path, {"la_code_oa": ["E06000001", "E06000999"]})
        )


def test_uk_dataset_check_admits_missing_codes_only_when_area_regions_are_routed(
    monkeypatch, tmp_path
) -> None:
    from policyengine_simulation_executor.stage12_worker_validation import (
        _check_uk_local_authority_dataset,
    )

    national = _uk_household_hdf(tmp_path, {"region": ["LONDON", "WALES"]})
    routed = {"value": False}
    monkeypatch.setattr(
        "policyengine_simulation_executor.uk_local_authority_metadata."
        "uk_area_regions_routed",
        lambda: routed["value"],
    )

    with pytest.raises(ValueError, match="contains no la_code_oa column"):
        _check_uk_local_authority_dataset(national)

    routed["value"] = True
    _check_uk_local_authority_dataset(national)
