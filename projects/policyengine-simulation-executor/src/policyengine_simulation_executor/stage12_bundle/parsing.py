"""Field-level parsing for the packaged PolicyEngine.py bundle."""

from __future__ import annotations

from collections.abc import Mapping

from policyengine_simulation_contract.stage12_bundle import (
    CountryId,
    Stage12CountryBundle,
    Stage12Dataset,
)

from .errors import Stage12BundleError


def require_mapping(value: object, path: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise Stage12BundleError(f"PolicyEngine.py bundle field {path} is missing")
    return {str(key): item for key, item in value.items()}


def require_text(value: object, path: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise Stage12BundleError(
            f"PolicyEngine.py bundle field {path} must be a non-empty string"
        )
    if value != value.strip():
        raise Stage12BundleError(
            f"PolicyEngine.py bundle field {path} must not contain outer whitespace"
        )
    return value


def require_exact_package(
    packages: Mapping[str, object],
    package_name: str,
) -> tuple[str, str]:
    package = require_mapping(packages.get(package_name), f"packages.{package_name}")
    name = require_text(package.get("name"), f"packages.{package_name}.name")
    version = require_text(
        package.get("version"),
        f"packages.{package_name}.version",
    )
    requirement = require_text(
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


def _dataset_uri(*, repo_id: str, path: str, revision: str) -> str:
    return f"hf://{repo_id}/{path}@{revision}"


def normalize_country_bundle(
    *,
    country: CountryId,
    policyengine_version: str,
    countries: Mapping[str, object],
    packages: Mapping[str, object],
    data_releases: Mapping[str, object],
) -> Stage12CountryBundle:
    """Validate and normalize one country's model and dataset release."""

    country_metadata = require_mapping(countries.get(country), f"countries.{country}")
    country_package_name = require_text(
        country_metadata.get("model_package"),
        f"countries.{country}.model_package",
    )
    country_package_version, country_package_requirement = require_exact_package(
        packages,
        country_package_name,
    )

    release = require_mapping(data_releases.get(country), f"data_releases.{country}")
    if (
        require_text(release.get("country_id"), f"data_releases.{country}.country_id")
        != country
    ):
        raise Stage12BundleError(
            f"PolicyEngine.py bundle data release for {country!r} names another country"
        )
    if (
        require_text(
            release.get("policyengine_version"),
            f"data_releases.{country}.policyengine_version",
        )
        != policyengine_version
    ):
        raise Stage12BundleError(
            f"PolicyEngine.py bundle data release for {country!r} names another PolicyEngine.py version"
        )

    release_model = require_mapping(
        release.get("model_package"),
        f"data_releases.{country}.model_package",
    )
    if (
        require_text(
            release_model.get("name"),
            f"data_releases.{country}.model_package.name",
        )
        != country_package_name
        or require_text(
            release_model.get("version"),
            f"data_releases.{country}.model_package.version",
        )
        != country_package_version
    ):
        raise Stage12BundleError(
            f"PolicyEngine.py bundle country package metadata is inconsistent for {country!r}"
        )

    data_package = require_mapping(
        release.get("data_package"),
        f"data_releases.{country}.data_package",
    )
    data_package_name = require_text(
        data_package.get("name"),
        f"data_releases.{country}.data_package.name",
    )
    data_package_version = require_text(
        data_package.get("version"),
        f"data_releases.{country}.data_package.version",
    )
    default_repo_id = require_text(
        data_package.get("repo_id"),
        f"data_releases.{country}.data_package.repo_id",
    )
    default_repo_type = require_text(
        data_package.get("repo_type"),
        f"data_releases.{country}.data_package.repo_type",
    )
    data_release_version = require_text(
        release.get("version"),
        f"data_releases.{country}.version",
    )
    default_dataset = require_text(
        release.get("default_dataset"),
        f"data_releases.{country}.default_dataset",
    )
    default_dataset_uri = require_text(
        release.get("default_dataset_uri"),
        f"data_releases.{country}.default_dataset_uri",
    )

    dataset_items = require_mapping(
        release.get("datasets"),
        f"data_releases.{country}.datasets",
    )
    datasets: list[Stage12Dataset] = []
    for identity in sorted(dataset_items):
        dataset = require_mapping(
            dataset_items[identity],
            f"data_releases.{country}.datasets.{identity}",
        )
        path = require_text(
            dataset.get("path"),
            f"data_releases.{country}.datasets.{identity}.path",
        )
        repo_id = require_text(
            dataset.get("repo_id") or default_repo_id,
            f"data_releases.{country}.datasets.{identity}.repo_id",
        )
        repo_type = require_text(
            dataset.get("repo_type") or default_repo_type,
            f"data_releases.{country}.datasets.{identity}.repo_type",
        )
        revision = require_text(
            dataset.get("revision"),
            f"data_releases.{country}.datasets.{identity}.revision",
        )
        digest = require_text(
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

    certified = require_mapping(
        release.get("certified_data_artifact"),
        f"data_releases.{country}.certified_data_artifact",
    )
    certified_package = require_mapping(
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
