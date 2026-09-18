"""Strict PolicyEngine.py bundle extraction for Stage 12 worker releases."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from hashlib import sha256
import json
from pydantic import ValidationError
from policyengine_simulation_contract.stage12_bundle import (
    CountryId,
    ResolvedStage12Bundle,
    Stage12BundleManifest,
    Stage12CountryBundle,
    Stage12Dataset,
    SUPPORTED_COUNTRIES,
)


class Stage12BundleError(ValueError):
    """The selected PolicyEngine.py bundle cannot define a v2 worker."""


def _mapping(value: object, path: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise Stage12BundleError(f"PolicyEngine.py bundle field {path} is missing")
    return {str(key): item for key, item in value.items()}


def _text(value: object, path: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise Stage12BundleError(
            f"PolicyEngine.py bundle field {path} must be a non-empty string"
        )
    if value != value.strip():
        raise Stage12BundleError(
            f"PolicyEngine.py bundle field {path} must not contain outer whitespace"
        )
    return value


def _package(
    packages: Mapping[str, object],
    package_name: str,
) -> tuple[str, str]:
    package = _mapping(packages.get(package_name), f"packages.{package_name}")
    name = _text(package.get("name"), f"packages.{package_name}.name")
    version = _text(package.get("version"), f"packages.{package_name}.version")
    requirement = _text(
        package.get("install_requirement"),
        f"packages.{package_name}.install_requirement",
    )
    if name != package_name:
        raise Stage12BundleError(
            f"PolicyEngine.py bundle package key {package_name!r} does not match its name"
        )
    expected_requirement = f"{name}=={version}"
    if requirement != expected_requirement:
        raise Stage12BundleError(
            f"PolicyEngine.py bundle package {name!r} is not exactly pinned"
        )
    return version, requirement


def _dataset_uri(
    *,
    repo_id: str,
    path: str,
    revision: str,
) -> str:
    return f"hf://{repo_id}/{path}@{revision}"


def _country_bundle(
    *,
    country: CountryId,
    policyengine_version: str,
    countries: Mapping[str, object],
    packages: Mapping[str, object],
    data_releases: Mapping[str, object],
) -> Stage12CountryBundle:
    country_metadata = _mapping(countries.get(country), f"countries.{country}")
    country_package_name = _text(
        country_metadata.get("model_package"),
        f"countries.{country}.model_package",
    )
    country_package_version, country_package_requirement = _package(
        packages,
        country_package_name,
    )

    release = _mapping(data_releases.get(country), f"data_releases.{country}")
    if (
        _text(release.get("country_id"), f"data_releases.{country}.country_id")
        != country
    ):
        raise Stage12BundleError(
            f"PolicyEngine.py bundle data release for {country!r} names another country"
        )
    if (
        _text(
            release.get("policyengine_version"),
            f"data_releases.{country}.policyengine_version",
        )
        != policyengine_version
    ):
        raise Stage12BundleError(
            f"PolicyEngine.py bundle data release for {country!r} names another PolicyEngine.py version"
        )

    release_model = _mapping(
        release.get("model_package"),
        f"data_releases.{country}.model_package",
    )
    if (
        _text(
            release_model.get("name"),
            f"data_releases.{country}.model_package.name",
        )
        != country_package_name
        or _text(
            release_model.get("version"),
            f"data_releases.{country}.model_package.version",
        )
        != country_package_version
    ):
        raise Stage12BundleError(
            f"PolicyEngine.py bundle country package metadata is inconsistent for {country!r}"
        )

    data_package = _mapping(
        release.get("data_package"),
        f"data_releases.{country}.data_package",
    )
    data_package_name = _text(
        data_package.get("name"),
        f"data_releases.{country}.data_package.name",
    )
    data_package_version = _text(
        data_package.get("version"),
        f"data_releases.{country}.data_package.version",
    )
    default_repo_id = _text(
        data_package.get("repo_id"),
        f"data_releases.{country}.data_package.repo_id",
    )
    default_repo_type = _text(
        data_package.get("repo_type"),
        f"data_releases.{country}.data_package.repo_type",
    )
    data_release_version = _text(
        release.get("version"),
        f"data_releases.{country}.version",
    )
    default_dataset = _text(
        release.get("default_dataset"),
        f"data_releases.{country}.default_dataset",
    )
    default_dataset_uri = _text(
        release.get("default_dataset_uri"),
        f"data_releases.{country}.default_dataset_uri",
    )

    dataset_items = _mapping(
        release.get("datasets"),
        f"data_releases.{country}.datasets",
    )
    datasets: list[Stage12Dataset] = []
    for identity in sorted(dataset_items):
        dataset = _mapping(
            dataset_items[identity],
            f"data_releases.{country}.datasets.{identity}",
        )
        path = _text(
            dataset.get("path"),
            f"data_releases.{country}.datasets.{identity}.path",
        )
        repo_id = _text(
            dataset.get("repo_id") or default_repo_id,
            f"data_releases.{country}.datasets.{identity}.repo_id",
        )
        repo_type = _text(
            dataset.get("repo_type") or default_repo_type,
            f"data_releases.{country}.datasets.{identity}.repo_type",
        )
        revision = _text(
            dataset.get("revision"),
            f"data_releases.{country}.datasets.{identity}.revision",
        )
        digest = _text(
            dataset.get("sha256"),
            f"data_releases.{country}.datasets.{identity}.sha256",
        )
        datasets.append(
            Stage12Dataset(
                identity=identity,
                uri=_dataset_uri(repo_id=repo_id, path=path, revision=revision),
                artifact_revision=revision,
                sha256=digest,
                repo_type=repo_type,
            )
        )

    datasets_by_identity = {dataset.identity: dataset for dataset in datasets}
    selected_dataset = datasets_by_identity.get(default_dataset)
    if selected_dataset is None:
        raise Stage12BundleError(
            f"PolicyEngine.py bundle default dataset is absent for {country!r}"
        )
    if selected_dataset.uri != default_dataset_uri:
        raise Stage12BundleError(
            f"PolicyEngine.py bundle default dataset URI is inconsistent for {country!r}"
        )

    certified = _mapping(
        release.get("certified_data_artifact"),
        f"data_releases.{country}.certified_data_artifact",
    )
    certified_package = _mapping(
        certified.get("data_package"),
        f"data_releases.{country}.certified_data_artifact.data_package",
    )
    certified_values = {
        "dataset": default_dataset,
        "uri": default_dataset_uri,
        "sha256": selected_dataset.sha256,
        "data_package.name": data_package_name,
        "data_package.version": data_package_version,
    }
    actual_certified_values = {
        "dataset": certified.get("dataset"),
        "uri": certified.get("uri"),
        "sha256": certified.get("sha256"),
        "data_package.name": certified_package.get("name"),
        "data_package.version": certified_package.get("version"),
    }
    if actual_certified_values != certified_values:
        raise Stage12BundleError(
            f"PolicyEngine.py bundle certified artifact is inconsistent for {country!r}"
        )

    return Stage12CountryBundle(
        country=country,
        country_package_name=country_package_name,
        country_package_version=country_package_version,
        country_package_requirement=country_package_requirement,
        data_package_name=data_package_name,
        data_package_version=data_package_version,
        data_release_version=data_release_version,
        data_artifact_revision=selected_dataset.artifact_revision,
        default_dataset=default_dataset,
        default_dataset_uri=default_dataset_uri,
        datasets=tuple(datasets),
    )


def _normalize_stage12_bundle(
    raw_bundle: object,
    *,
    supported_countries: Sequence[CountryId] = SUPPORTED_COUNTRIES,
) -> Stage12BundleManifest:
    """Normalize only values certified by the packaged `.py` bundle."""

    bundle = _mapping(raw_bundle, "root")
    if bundle.get("schema_version") != 2:
        raise Stage12BundleError("PolicyEngine.py bundle schema_version must be 2")
    policyengine_version = _text(
        bundle.get("policyengine_version"),
        "policyengine_version",
    )
    if _text(bundle.get("bundle_version"), "bundle_version") != policyengine_version:
        raise Stage12BundleError(
            "PolicyEngine.py bundle_version does not match policyengine_version"
        )

    packages = _mapping(bundle.get("packages"), "packages")
    packaged_policyengine_version, policyengine_requirement = _package(
        packages,
        "policyengine",
    )
    if packaged_policyengine_version != policyengine_version:
        raise Stage12BundleError(
            "PolicyEngine.py package version does not match the bundle version"
        )
    core_package_version, core_package_requirement = _package(
        packages,
        "policyengine-core",
    )
    countries = _mapping(bundle.get("countries"), "countries")
    data_releases = _mapping(bundle.get("data_releases"), "data_releases")

    normalized_countries = tuple(
        _country_bundle(
            country=country,
            policyengine_version=policyengine_version,
            countries=countries,
            packages=packages,
            data_releases=data_releases,
        )
        for country in supported_countries
    )
    return Stage12BundleManifest(
        policyengine_version=policyengine_version,
        policyengine_requirement=policyengine_requirement,
        core_package_version=core_package_version,
        core_package_requirement=core_package_requirement,
        countries=normalized_countries,
    )


def normalize_stage12_bundle(
    raw_bundle: object,
    *,
    supported_countries: Sequence[CountryId] = SUPPORTED_COUNTRIES,
) -> Stage12BundleManifest:
    """Normalize only values certified by the packaged `.py` bundle."""

    try:
        return _normalize_stage12_bundle(
            raw_bundle,
            supported_countries=supported_countries,
        )
    except ValidationError as error:
        raise Stage12BundleError(
            "PolicyEngine.py bundle contains malformed values"
        ) from error


def bundle_digest(bundle: Stage12BundleManifest) -> str:
    payload = json.dumps(
        bundle.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode()
    return sha256(payload).hexdigest()


def resolve_stage12_bundle(raw_bundle: object) -> ResolvedStage12Bundle:
    bundle = normalize_stage12_bundle(raw_bundle)
    return ResolvedStage12Bundle(
        bundle=bundle,
        bundle_manifest_sha256=bundle_digest(bundle),
    )


def load_stage12_bundle() -> ResolvedStage12Bundle:
    """Read the installed PolicyEngine.py release's packaged bundle."""

    from policyengine.bundle import get_current_bundle

    return resolve_stage12_bundle(get_current_bundle())


def assertion_values(bundle: Stage12BundleManifest) -> dict[str, str]:
    values = {
        "policyengine_version": bundle.policyengine_version,
        "policyengine_requirement": bundle.policyengine_requirement,
        "core_package_version": bundle.core_package_version,
        "core_package_requirement": bundle.core_package_requirement,
    }
    for country in bundle.countries:
        prefix = f"countries.{country.country}"
        values.update(
            {
                f"{prefix}.country_package_name": country.country_package_name,
                f"{prefix}.country_package_version": country.country_package_version,
                f"{prefix}.data_package_name": country.data_package_name,
                f"{prefix}.data_package_version": country.data_package_version,
                f"{prefix}.data_release_version": country.data_release_version,
                f"{prefix}.default_dataset": country.default_dataset,
                f"{prefix}.default_dataset_uri": country.default_dataset_uri,
                f"{prefix}.data_artifact_revision": country.data_artifact_revision,
            }
        )
    return values


def assert_expected_bundle_values(
    bundle: Stage12BundleManifest,
    expected: Mapping[str, str],
) -> None:
    """Check external values without allowing them to select bundle content."""

    actual = assertion_values(bundle)
    for name, expected_value in expected.items():
        if name not in actual:
            raise Stage12BundleError(f"Unknown Stage 12 bundle assertion {name!r}")
        if actual[name] != expected_value:
            raise Stage12BundleError(
                f"Stage 12 bundle assertion {name!r} does not match the packaged bundle"
            )
