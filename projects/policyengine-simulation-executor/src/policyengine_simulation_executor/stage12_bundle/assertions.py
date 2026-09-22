"""Deployment assertions for normalized Stage 12 bundle values."""

from __future__ import annotations

from collections.abc import Mapping

from policyengine_simulation_contract.stage12_bundle import Stage12BundleManifest

from .errors import Stage12BundleError


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
