"""Tests for bounded Stage 12 deployed-worker validation."""

from __future__ import annotations

from hashlib import sha256
from types import SimpleNamespace

import pytest
from policyengine_simulation_executor.stage12_bundle import load_stage12_bundle
from policyengine_simulation_executor.stage12_worker_validation import (
    WorkerValidationSeams,
    _run_non_serving_calculation,
    validate_country_worker,
)


def _environment() -> dict[str, str]:
    return {
        "HUGGING_FACE_TOKEN": "test-token",
        "GOOGLE_APPLICATION_CREDENTIALS_JSON": "{}",
        "STAGE12_DATABASE_URL": "postgresql://runtime:secret@db.example/postgres",
        "STAGE12_ARTIFACT_BUCKET": "policyengine-stage12-staging",
        "STAGE12_CACHE_BUCKET": "policyengine-stage12-cache-staging",
    }


def test_validation_checks_dataset_and_non_serving_calculation() -> None:
    resolved = load_stage12_bundle()
    datasets: list[tuple[str, str]] = []
    countries: list[str] = []

    result = validate_country_worker(
        country="us",
        expected_bundle_manifest_sha256=resolved.bundle_manifest_sha256,
        seams=WorkerValidationSeams(
            environment=_environment(),
            dataset_path_resolver=lambda _: "/installed/populace_us_2024.h5",
            dataset_check=lambda path, digest: datasets.append((path, digest)),
            calculation_check=countries.append,
            cache_check=lambda digest: {"manifest_sha256": digest},
        ),
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
    assert result["cache"] is None


def test_non_serving_us_calculation_supports_national_spm() -> None:
    """The deployed-worker check invents no county for its synthetic household."""
    _run_non_serving_calculation("us")


def test_non_serving_us_calculation_selects_national_without_county(
    monkeypatch,
) -> None:
    calls: list[dict] = []

    def calculate_household(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(
            household={"household_net_income": 1},
            tax_unit={"ctc": 1},
        )

    monkeypatch.setattr(
        "policyengine_simulation_executor.stage12_worker_validation.import_module",
        lambda _: SimpleNamespace(calculate_household=calculate_household),
    )

    _run_non_serving_calculation("us")

    assert calls[0]["spm"] == {"geography_kind": "national"}
    assert "household" not in calls[0]


def test_us_validation_preserves_the_frontend_reform_interval(monkeypatch) -> None:
    from policyengine_simulation_executor import stage12_worker_validation

    received = {}

    def calculate_household(**kwargs):
        received.update(kwargs)
        return SimpleNamespace(
            household={"household_net_income": 50_000},
            tax_unit={"ctc": 3_000},
        )

    monkeypatch.setattr(
        stage12_worker_validation,
        "import_module",
        lambda _: SimpleNamespace(calculate_household=calculate_household),
    )

    _run_non_serving_calculation("us")

    assert received["reform"] == {
        # The current wrapper compiles the complete interval; do not discard
        # its end date while passing the front-end reform to calculate_household.
        "gov.irs.credits.ctc.amount.base[0].amount": {"2026-01-01.2100-12-31": 3_000}
    }
    assert received["people"][1]["is_tax_unit_dependent"] is True
    assert "ctc" in received["extra_variables"]


def test_uk_validation_loads_packaged_local_authority_resources() -> None:
    resolved = load_stage12_bundle()
    checks: list[str] = []

    result = validate_country_worker(
        country="uk",
        expected_bundle_manifest_sha256=resolved.bundle_manifest_sha256,
        seams=WorkerValidationSeams(
            environment=_environment(),
            dataset_path_resolver=lambda _: "/installed/enhanced_frs_2024_25.h5",
            dataset_check=lambda *_: None,
            calculation_check=lambda _: None,
            local_authority_resource_check=checks.append,
        ),
    )

    assert result["validated"] is True
    assert checks == ["/installed/enhanced_frs_2024_25.h5"]


def test_validation_rejects_digest_mismatch_before_dataset_access() -> None:
    accessed: list[str] = []
    with pytest.raises(RuntimeError, match="digest differs"):
        validate_country_worker(
            country="us",
            expected_bundle_manifest_sha256="0" * 64,
            seams=WorkerValidationSeams(
                environment=_environment(),
                dataset_path_resolver=lambda _: "/installed/populace_us_2024.h5",
                dataset_check=lambda path, _: accessed.append(path),
                calculation_check=lambda _: None,
            ),
        )
    assert accessed == []


def test_us_validation_reports_the_verified_cache_release() -> None:
    resolved = load_stage12_bundle()
    seen: list[str] = []
    cache = {
        "manifest_sha256": "c" * 64,
        "partition_sha256": "d" * 64,
        "years": [2026, 2027, 2025],
        "segment_count": 20,
        "dataset_count": 3,
        "baseline_count": 60,
    }

    result = validate_country_worker(
        country="us",
        expected_bundle_manifest_sha256=resolved.bundle_manifest_sha256,
        expected_cache_manifest_sha256="c" * 64,
        seams=WorkerValidationSeams(
            environment=_environment(),
            dataset_path_resolver=lambda _: "/installed/populace_us_2024.h5",
            dataset_check=lambda *_: None,
            calculation_check=lambda _: None,
            cache_check=lambda digest: seen.append(digest) or cache,
        ),
    )

    assert seen == ["c" * 64]
    assert result["cache"] == cache


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
            seams=WorkerValidationSeams(
                environment=environment,
                dataset_path_resolver=lambda _: "/installed/populace_us_2024.h5",
                dataset_check=lambda *_: None,
                calculation_check=lambda _: None,
            ),
        )


def test_validation_rejects_missing_installed_dataset() -> None:
    resolved = load_stage12_bundle()
    with pytest.raises(RuntimeError, match="installed certified dataset"):
        validate_country_worker(
            country="us",
            expected_bundle_manifest_sha256=resolved.bundle_manifest_sha256,
            seams=WorkerValidationSeams(
                environment=_environment(),
                dataset_path_resolver=lambda _: None,
                dataset_check=lambda *_: None,
                calculation_check=lambda _: None,
            ),
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
    monkeypatch, tmp_path, *, routed: bool
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


def _validate(country="us", expected_digest=None, **seams):
    seams.setdefault("environment", _environment())
    seams.setdefault("dataset_path_resolver", lambda _: f"/installed/{country}.h5")
    seams.setdefault("dataset_check", lambda *_: None)
    seams.setdefault("calculation_check", lambda _: None)
    return validate_country_worker(
        country=country,
        expected_bundle_manifest_sha256=(
            expected_digest or load_stage12_bundle().bundle_manifest_sha256
        ),
        seams=WorkerValidationSeams(**seams),
    )


@pytest.mark.parametrize("country", ["us", "uk"])
def test_validation_reports_the_complete_installed_bundle(country) -> None:
    resolved = load_stage12_bundle()
    country_bundle = next(
        item for item in resolved.bundle.countries if item.country == country
    )
    events = []

    result = _validate(
        country,
        dataset_check=lambda path, _digest: events.append(("dataset", path)),
        calculation_check=lambda name: events.append(("calculation", name)),
        local_authority_resource_check=lambda path: events.append(("uk", path)),
    )

    assert result == {
        "validated": True,
        "country": country,
        "policyengine_version": resolved.bundle.policyengine_version,
        "country_package_name": country_bundle.country_package_name,
        "country_package_version": country_bundle.country_package_version,
        "dataset_identity": country_bundle.default_dataset,
        "dataset_uri": country_bundle.default_dataset_uri,
        "data_artifact_revision": country_bundle.data_artifact_revision,
        "bundle_manifest_sha256": resolved.bundle_manifest_sha256,
        "cache": None,
    }
    expected_events = [("dataset", f"/installed/{country}.h5")]
    if country == "uk":
        expected_events.append(("uk", f"/installed/{country}.h5"))
    expected_events.append(("calculation", country))
    assert events == expected_events


def test_validation_rejects_a_country_the_bundle_does_not_serve(monkeypatch) -> None:
    from policyengine_simulation_executor import stage12_worker_validation

    resolved = load_stage12_bundle()
    us_only = resolved.model_copy(
        update={
            "bundle": resolved.bundle.model_copy(
                update={
                    "countries": tuple(
                        item
                        for item in resolved.bundle.countries
                        if item.country == "us"
                    )
                }
            )
        }
    )
    monkeypatch.setattr(
        stage12_worker_validation, "load_stage12_bundle", lambda: us_only
    )

    with pytest.raises(RuntimeError) as error:
        _validate("uk")

    assert str(error.value) == "installed PolicyEngine.py bundle does not support 'uk'"
    assert error.value.__cause__ is None
    assert error.value.__suppress_context__ is True


def test_validation_uses_the_process_environment_by_default(monkeypatch) -> None:
    for name, value in _environment().items():
        monkeypatch.setenv(name, value)

    assert _validate(environment=None)["validated"] is True

    monkeypatch.delenv("STAGE12_ARTIFACT_BUCKET")
    with pytest.raises(
        RuntimeError,
        match=r"^Stage 12 worker is missing required secret values: "
        r"STAGE12_ARTIFACT_BUCKET$",
    ):
        _validate(environment=None)


def test_digest_mismatch_is_checked_before_any_dataset_access() -> None:
    events = []

    with pytest.raises(
        RuntimeError,
        match=(
            r"^installed PolicyEngine\.py bundle digest differs "
            r"from deployment input$"
        ),
    ):
        _validate(
            expected_digest="0" * 64,
            dataset_path_resolver=lambda _: events.append("resolve"),
        )

    assert events == []


def _installed_manifest(*, state_codes=("UT", "WY")):
    from policyengine_simulation_executor.stage12_cache.models import CacheManifest
    from policyengine_simulation_executor.stage12_cache.precompute import (
        PRECOMPUTE_YEARS,
    )
    from policyengine_simulation_executor.stage12_runtime.partition import (
        US_REGION_GROUPS,
    )

    def entry(kind, year, index):
        return {
            "type": kind,
            "path": f"{kind}s/us/{year}-{index}/{kind}-{year}-{index}.h5",
            "filename": f"{kind}-{year}-{index}.h5",
            "year": year,
            "identity_digest": f"{kind}-{year}-{index}",
            "content_sha256": "f" * 64,
            "size_bytes": 1,
        }

    artifacts = [entry("dataset", year, 0) for year in PRECOMPUTE_YEARS]
    artifacts += [
        entry("baseline", year, index)
        for year in PRECOMPUTE_YEARS
        for index in range(len(US_REGION_GROUPS))
    ]
    artifacts += [
        {
            "type": "state_dataset",
            "path": f"state-datasets/us/{year}-{state}/acs_year_{year}.h5",
            "filename": f"acs_year_{year}.h5",
            "folder": f"state-datasets/acs/{state}",
            "state_code": state,
            "year": year,
            "identity_digest": f"state-{year}-{state}",
            "content_sha256": "f" * 64,
            "size_bytes": 1,
        }
        for year in PRECOMPUTE_YEARS
        for state in state_codes
    ]
    return CacheManifest.model_validate(
        {
            "schema": "stage12-mf1",
            "country": "us",
            "years": list(PRECOMPUTE_YEARS),
            "partition_sha256": "p" * 64,
            "bundle": {
                "bundle_manifest_sha256": "b" * 64,
                "policyengine_version": "5.2.0",
                "core_package_version": "3.0.0",
                "country_package_version": "1.0.0",
                "data_package_version": "2.0.0",
                "data_artifact_revision": "revision",
                "default_dataset": "populace_us_2024",
            },
            "artifacts": artifacts,
        }
    )


def _install_state_files(data_folder, manifest):
    for item in manifest.artifacts:
        if item.type == "state_dataset":
            folder = data_folder / item.folder
            folder.mkdir(parents=True, exist_ok=True)
            (folder / item.filename).write_bytes(b"h5")


def test_installed_cache_accepts_the_expected_state_files(tmp_path) -> None:
    from policyengine_simulation_executor import stage12_worker_validation as module

    manifest = _installed_manifest()
    _install_state_files(tmp_path, manifest)

    module._require_installed_artifacts(manifest, tmp_path, state_dataset_count=6)


def test_installed_cache_refuses_a_state_file_count_the_bundle_does_not_give(
    tmp_path,
) -> None:
    from policyengine_simulation_executor import stage12_worker_validation as module

    manifest = _installed_manifest(state_codes=("UT",))
    _install_state_files(tmp_path, manifest)

    with pytest.raises(RuntimeError, match="artifact count differs"):
        module._require_installed_artifacts(manifest, tmp_path, state_dataset_count=6)


def test_installed_cache_refuses_a_missing_state_file(tmp_path) -> None:
    from policyengine_simulation_executor import stage12_worker_validation as module

    manifest = _installed_manifest()

    with pytest.raises(RuntimeError, match="state dataset file is missing"):
        module._require_installed_artifacts(manifest, tmp_path, state_dataset_count=6)


def test_expected_state_file_count_follows_the_installed_bundle(monkeypatch) -> None:
    from policyengine_simulation_executor import stage12_worker_validation as module
    from policyengine_simulation_executor.stage12_cache import precompute

    monkeypatch.setattr(precompute, "state_dataset_targets", lambda: ("UT", "WY"))

    assert module.expected_state_dataset_count() == 2 * len(precompute.PRECOMPUTE_YEARS)
