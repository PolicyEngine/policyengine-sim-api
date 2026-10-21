"""Bounded validation for a deployed Stage 12 country worker."""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from hashlib import sha256
from importlib import import_module
from pathlib import Path
from typing import TYPE_CHECKING, Any

from policyengine_simulation_executor.release_bundle import (
    resolve_local_bundle_dataset_path,
)
from policyengine_simulation_executor.simulation_runtime import (
    _normalise_policy,
    resolve_data_folder,
)
from policyengine_simulation_executor.stage12_bundle import load_stage12_bundle
from policyengine_simulation_executor.stage12_cache import (
    precompute as cache_precompute,
)
from policyengine_simulation_executor.stage12_cache.models import CacheManifest
from policyengine_simulation_executor.stage12_cache.precompute import (
    PRECOMPUTE_YEARS,
    _scoped_planned_simulation,
)
from policyengine_simulation_executor.stage12_runtime.partition import (
    US_REGION_GROUPS,
)
from policyengine_simulation_executor.stage12_runtime.simulation import (
    calculate_simulation_frames,
)

if TYPE_CHECKING:
    from policyengine_simulation_contract.stage12_bundle import CountryId

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

US_VALIDATION_REFORM = {
    "gov.irs.credits.ctc.amount.base[0].amount": {"2026-01-01.2100-12-31": 3_000}
}


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
        msg = "Stage 12 installed dataset artifact is empty"
        raise RuntimeError(msg)
    if digest.hexdigest() != expected_sha256:
        msg = "Stage 12 installed dataset artifact digest differs"
        raise RuntimeError(msg)


def _run_non_serving_calculation(country: CountryId) -> None:
    country_module = import_module(f"policyengine.tax_benefit_models.{country}")
    people = [{"age": 40, "employment_income": 50_000}]
    extra_variables = ["household_net_income"]
    options: dict[str, Any] = {}
    if country == "us":
        people = [
            {
                "age": 40,
                "employment_income": 50_000,
                "is_tax_unit_head": True,
            },
            {"age": 10, "is_tax_unit_dependent": True},
        ]
        extra_variables.append("ctc")
        options["spm"] = {"geography_kind": "national"}
        options["reform"] = _normalise_policy(US_VALIDATION_REFORM)
    result = country_module.calculate_household(
        people=people,
        year=2026,
        extra_variables=extra_variables,
        **options,
    )
    household = getattr(result, "household", None)
    if not isinstance(household, Mapping) or "household_net_income" not in household:
        msg = "Stage 12 validation calculation returned no net income"
        raise RuntimeError(msg)
    if country == "us":
        tax_unit = getattr(result, "tax_unit", None)
        if not isinstance(tax_unit, Mapping) or not isinstance(
            tax_unit.get("ctc"), (int, float)
        ):
            msg = "Stage 12 validation reform returned no CTC value"
            raise RuntimeError(msg)


def _check_uk_local_authority_resources() -> None:
    from policyengine_simulation_executor.uk_local_authority_metadata import (  # noqa: PLC0415  # the UK metadata module is not loaded at import; loaded per call
        load_uk_local_authority_resources,
    )

    resources = load_uk_local_authority_resources()
    for boundary_version in resources.coordinates:
        resources.metadata_for(boundary_version)


def _check_uk_local_authority_dataset(dataset_path: str) -> None:
    from policyengine_simulation_executor.uk_local_authority_metadata import (  # noqa: PLC0415  # resolved per call so tests can patch uk_local_authority_metadata
        detect_uk_local_authority_metadata_from_hdf,
        uk_area_regions_routed,
        uk_hdf_household_has_local_authority_codes,
    )

    _check_uk_local_authority_resources()
    # A national default may carry no area codes only when the bundle routes
    # constituency and local-authority regions to another dataset; anything
    # else is checked exactly as before.
    if uk_area_regions_routed() and not uk_hdf_household_has_local_authority_codes(
        dataset_path
    ):
        return
    detect_uk_local_authority_metadata_from_hdf(dataset_path)


# The installed cache holds one dataset per precomputed year and one baseline
# per US region group per year; the deploy check probes the first year.
STAGE12_CACHE_ARTIFACT_COUNT = len(PRECOMPUTE_YEARS) * (1 + len(US_REGION_GROUPS))
STAGE12_CACHE_PROBE_YEAR = PRECOMPUTE_YEARS[0]


def expected_state_dataset_count() -> int:
    """Return how many state dataset files the installed bundle's cache holds.

    One per precomputed year for each US state region on a non-default
    dataset; none when every state uses the default dataset.
    """
    return len(PRECOMPUTE_YEARS) * len(cache_precompute.state_dataset_targets())


def _require_installed_artifacts(
    manifest: CacheManifest, data_folder: Path, *, state_dataset_count: int
) -> None:
    """Require the expected number of listed files and every state file on disk."""
    if len(manifest.artifacts) != STAGE12_CACHE_ARTIFACT_COUNT + state_dataset_count:
        msg = "installed Stage 12 cache artifact count differs"
        raise RuntimeError(msg)
    for item in manifest.artifacts:
        if (
            item.type == "state_dataset"
            and not (data_folder / item.folder / item.filename).is_file()
        ):
            msg = f"installed Stage 12 state dataset file is missing: {item.path}"
            raise RuntimeError(msg)


def _check_installed_cache(expected_manifest_sha256: str) -> dict[str, Any]:
    """Validate the baked manifest and require one real baseline cache hit."""
    data_folder = Path(resolve_data_folder())
    manifest_path = data_folder / ".stage12-cache-manifest.json"
    payload = manifest_path.read_bytes()
    if sha256(payload).hexdigest() != expected_manifest_sha256:
        msg = "installed Stage 12 cache manifest digest differs"
        raise RuntimeError(msg)
    manifest = CacheManifest.model_validate_json(payload)
    if manifest.years != PRECOMPUTE_YEARS:
        msg = "installed Stage 12 cache years differ"
        raise RuntimeError(msg)
    _require_installed_artifacts(
        manifest,
        data_folder,
        state_dataset_count=expected_state_dataset_count(),
    )
    probe = next(
        item
        for item in manifest.artifacts
        if item.type == "baseline" and item.year == STAGE12_CACHE_PROBE_YEAR
    )
    probe_path = data_folder / probe.filename
    _check_dataset_access(str(probe_path), probe.content_sha256)
    calculation = calculate_simulation_frames(
        _scoped_planned_simulation(STAGE12_CACHE_PROBE_YEAR, US_REGION_GROUPS[0])
    )
    if calculation.cache_outcome != "hit":
        msg = "deployed Stage 12 cache probe did not load a cache hit"
        raise RuntimeError(msg)
    return _cache_release_summary(manifest, expected_manifest_sha256)


def _cache_release_summary(
    manifest: CacheManifest, manifest_sha256: str
) -> dict[str, Any]:
    """Return the cache release a worker reports, in ``Stage12CacheRelease`` form.

    Only national datasets and baselines are counted: the release model pins
    those counts and forbids other fields, so state datasets are not reported.
    """
    return {
        "manifest_sha256": manifest_sha256,
        "partition_sha256": manifest.partition_sha256,
        "years": list(manifest.years),
        "segment_count": len(US_REGION_GROUPS),
        "dataset_count": sum(item.type == "dataset" for item in manifest.artifacts),
        "baseline_count": sum(item.type == "baseline" for item in manifest.artifacts),
    }


@dataclass(frozen=True)
class WorkerValidationSeams:
    """What worker validation reads and runs; the defaults inspect this worker.

    ``environment`` defaults to the process environment. The checks default
    to the installed dataset path, a streamed digest of that dataset, a
    non-serving household calculation, the UK local-authority resources and
    the installed Stage 12 cache.
    """

    environment: Mapping[str, str] | None = None
    dataset_path_resolver: Callable[[CountryId], str | None] = (
        _resolve_installed_dataset_path
    )
    dataset_check: Callable[[str, str], None] = _check_dataset_access
    calculation_check: Callable[[CountryId], None] = _run_non_serving_calculation
    local_authority_resource_check: Callable[[str], None] = (
        _check_uk_local_authority_dataset
    )
    cache_check: Callable[[str], dict[str, Any]] = _check_installed_cache


def validate_country_worker(
    *,
    country: CountryId,
    expected_bundle_manifest_sha256: str,
    expected_cache_manifest_sha256: str | None = None,
    seams: WorkerValidationSeams | None = None,
) -> dict[str, Any]:
    """Validate imports, installed bundle, secrets, data, and calculation."""
    seams = seams or WorkerValidationSeams()
    environment = seams.environment
    runtime_environment = environment if environment is not None else os.environ
    _require_secret_environment(runtime_environment)
    # Import the same typed persistence package and coordinator modules loaded
    # by a real report run before publishing the manifest.
    import_module("policyengine_stage12_persistence")
    import_module("policyengine_simulation_executor.stage12_runtime")
    resolved = load_stage12_bundle()
    if resolved.bundle_manifest_sha256 != expected_bundle_manifest_sha256:
        msg = "installed PolicyEngine.py bundle digest differs from deployment input"
        raise RuntimeError(msg)
    try:
        country_bundle = next(
            item for item in resolved.bundle.countries if item.country == country
        )
    except StopIteration:
        msg = f"installed PolicyEngine.py bundle does not support {country!r}"
        raise RuntimeError(msg) from None
    import_module(f"policyengine.tax_benefit_models.{country}")
    installed_dataset_path = seams.dataset_path_resolver(country)
    if installed_dataset_path is None:
        msg = f"installed certified dataset is unavailable for {country!r}"
        raise RuntimeError(msg)
    selected_dataset = next(
        dataset
        for dataset in country_bundle.datasets
        if dataset.identity == country_bundle.default_dataset
    )
    seams.dataset_check(installed_dataset_path, selected_dataset.sha256)
    if country == "uk":
        seams.local_authority_resource_check(installed_dataset_path)
    seams.calculation_check(country)
    cache = None
    if country == "us" and expected_cache_manifest_sha256:
        cache = seams.cache_check(expected_cache_manifest_sha256)
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
