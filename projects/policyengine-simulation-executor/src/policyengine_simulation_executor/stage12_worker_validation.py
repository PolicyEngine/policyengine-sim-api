"""Bounded validation for a deployed Stage 12 country worker."""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from hashlib import sha256
from importlib import import_module
from pathlib import Path
from typing import Any

from policyengine_simulation_contract.stage12_bundle import CountryId

from policyengine_simulation_executor.release_bundle import (
    resolve_local_bundle_dataset_path,
)
from policyengine_simulation_executor.stage12_bundle import load_stage12_bundle

REQUIRED_SECRET_ALTERNATIVES = (
    ("HUGGING_FACE_TOKEN",),
    (
        "GOOGLE_APPLICATION_CREDENTIALS_JSON",
        "GCP_CREDENTIALS_JSON",
        "GOOGLE_APPLICATION_CREDENTIALS",
    ),
    ("STAGE12_DATABASE_URL",),
    ("STAGE12_ARTIFACT_BUCKET",),
    ("STAGE12_CACHE_BUCKET",),
)


def _require_secret_environment(environment: Mapping[str, str]) -> None:
    for alternatives in REQUIRED_SECRET_ALTERNATIVES:
        if not any(environment.get(name) for name in alternatives):
            raise RuntimeError(
                "Stage 12 worker is missing required secret values: "
                + " or ".join(alternatives)
            )


def _resolve_installed_dataset_path(country: CountryId) -> str | None:
    return resolve_local_bundle_dataset_path(country, None)


def _check_dataset_access(path: str, expected_sha256: str) -> None:
    dataset_path = Path(path)
    digest = sha256()
    with dataset_path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    if dataset_path.stat().st_size == 0:
        raise RuntimeError("Stage 12 installed dataset artifact is empty")
    if digest.hexdigest() != expected_sha256:
        raise RuntimeError("Stage 12 installed dataset artifact digest differs")


def _run_non_serving_calculation(country: CountryId) -> None:
    country_module = import_module(f"policyengine.tax_benefit_models.{country}")
    spm = {"geography_kind": "national"} if country == "us" else None
    result = country_module.calculate_household(
        people=[{"age": 40, "employment_income": 50_000}],
        **({"spm": spm} if spm is not None else {}),
        year=2026,
        extra_variables=["household_net_income"],
    )
    household = getattr(result, "household", None)
    if not isinstance(household, Mapping) or "household_net_income" not in household:
        raise RuntimeError("Stage 12 validation calculation returned no net income")


def _check_uk_local_authority_resources() -> None:
    from policyengine_simulation_executor.uk_local_authority_metadata import (
        load_uk_local_authority_resources,
    )

    resources = load_uk_local_authority_resources()
    for boundary_version in resources.coordinates:
        resources.metadata_for(boundary_version)


def _check_uk_local_authority_dataset(dataset_path: str) -> None:
    from policyengine_simulation_executor.uk_local_authority_metadata import (
        detect_uk_local_authority_metadata_from_hdf,
    )

    _check_uk_local_authority_resources()
    detect_uk_local_authority_metadata_from_hdf(dataset_path)


def _check_installed_cache(expected_manifest_sha256: str) -> dict[str, Any]:
    """Validate the baked manifest and require one real baseline cache hit."""

    from policyengine_simulation_executor.simulation_runtime import resolve_data_folder
    from policyengine_simulation_executor.stage12_cache.models import CacheManifest
    from policyengine_simulation_executor.stage12_cache.precompute import (
        _scoped_planned_simulation,
    )
    from policyengine_simulation_executor.stage12_runtime.partition import (
        US_REGION_GROUPS,
    )
    from policyengine_simulation_executor.stage12_runtime.simulation import (
        calculate_simulation_frames,
    )

    data_folder = Path(resolve_data_folder())
    manifest_path = data_folder / ".stage12-cache-manifest.json"
    payload = manifest_path.read_bytes()
    if sha256(payload).hexdigest() != expected_manifest_sha256:
        raise RuntimeError("installed Stage 12 cache manifest digest differs")
    manifest = CacheManifest.model_validate_json(payload)
    if manifest.years != (2026, 2027, 2025):
        raise RuntimeError("installed Stage 12 cache years differ")
    if len(manifest.artifacts) != 63:
        raise RuntimeError("installed Stage 12 cache artifact count differs")
    probe = next(
        item
        for item in manifest.artifacts
        if item.type == "baseline" and item.year == 2026
    )
    probe_path = data_folder / probe.filename
    _check_dataset_access(str(probe_path), probe.content_sha256)
    calculation = calculate_simulation_frames(
        _scoped_planned_simulation(2026, US_REGION_GROUPS[0])
    )
    if calculation.cache_outcome != "hit":
        raise RuntimeError("deployed Stage 12 cache probe did not load a cache hit")
    return {
        "manifest_sha256": expected_manifest_sha256,
        "partition_sha256": manifest.partition_sha256,
        "years": list(manifest.years),
        "segment_count": 20,
        "dataset_count": sum(item.type == "dataset" for item in manifest.artifacts),
        "baseline_count": sum(item.type == "baseline" for item in manifest.artifacts),
    }


def validate_country_worker(
    *,
    country: CountryId,
    expected_bundle_manifest_sha256: str,
    expected_cache_manifest_sha256: str | None = None,
    environment: Mapping[str, str] | None = None,
    dataset_path_resolver: Callable[[CountryId], str | None] = (
        _resolve_installed_dataset_path
    ),
    dataset_check: Callable[[str, str], None] = _check_dataset_access,
    calculation_check: Callable[[CountryId], None] = _run_non_serving_calculation,
    local_authority_resource_check: Callable[[str], None] = (
        _check_uk_local_authority_dataset
    ),
    cache_check: Callable[[str], dict[str, Any]] = _check_installed_cache,
) -> dict[str, Any]:
    """Validate imports, installed bundle, secrets, data, and calculation."""

    runtime_environment = environment if environment is not None else os.environ
    _require_secret_environment(runtime_environment)
    # Import the same typed persistence package and coordinator modules loaded
    # by a real report run before publishing the manifest.
    import_module("policyengine_stage12_persistence")
    import_module("policyengine_simulation_executor.stage12_runtime")
    resolved = load_stage12_bundle()
    if resolved.bundle_manifest_sha256 != expected_bundle_manifest_sha256:
        raise RuntimeError(
            "installed PolicyEngine.py bundle digest differs from deployment input"
        )
    try:
        country_bundle = next(
            item for item in resolved.bundle.countries if item.country == country
        )
    except StopIteration:
        raise RuntimeError(
            f"installed PolicyEngine.py bundle does not support {country!r}"
        ) from None
    import_module(f"policyengine.tax_benefit_models.{country}")
    installed_dataset_path = dataset_path_resolver(country)
    if installed_dataset_path is None:
        raise RuntimeError(
            f"installed certified dataset is unavailable for {country!r}"
        )
    selected_dataset = next(
        dataset
        for dataset in country_bundle.datasets
        if dataset.identity == country_bundle.default_dataset
    )
    dataset_check(installed_dataset_path, selected_dataset.sha256)
    if country == "uk":
        local_authority_resource_check(installed_dataset_path)
    calculation_check(country)
    cache = None
    if country == "us" and expected_cache_manifest_sha256:
        cache = cache_check(expected_cache_manifest_sha256)
    return {
        "validated": True,
        "country": country,
        "policyengine_version": resolved.bundle.policyengine_version,
        "country_package_name": country_bundle.country_package_name,
        "country_package_version": country_bundle.country_package_version,
        "dataset_identity": country_bundle.default_dataset,
        "dataset_uri": country_bundle.default_dataset_uri,
        "data_artifact_revision": country_bundle.data_artifact_revision,
        "bundle_manifest_sha256": resolved.bundle_manifest_sha256,
        "cache": cache,
    }
