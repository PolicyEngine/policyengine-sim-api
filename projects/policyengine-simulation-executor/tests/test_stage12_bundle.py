"""Strict Stage 12 bundle authority tests."""

from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

from policyengine.bundle import get_current_bundle
import pytest

from policyengine_simulation_executor.stage12_bundle import (
    Stage12BundleError,
    assert_expected_bundle_values,
    bundle_digest,
    normalize_stage12_bundle,
    resolve_stage12_bundle,
)


def _bundle() -> dict:
    return deepcopy(get_current_bundle())


def test_complete_packaged_bundle_selects_every_worker_dependency_and_dataset() -> None:
    resolved = resolve_stage12_bundle(_bundle())
    bundle = resolved.bundle

    assert bundle.policyengine_requirement == (
        f"policyengine=={bundle.policyengine_version}"
    )
    assert bundle.core_package_requirement == (
        f"policyengine-core=={bundle.core_package_version}"
    )
    assert [country.country for country in bundle.countries] == ["us", "uk"]
    for country in bundle.countries:
        assert country.country_package_requirement == (
            f"{country.country_package_name}=={country.country_package_version}"
        )
        selected = next(
            dataset
            for dataset in country.datasets
            if dataset.identity == country.default_dataset
        )
        assert selected.uri == country.default_dataset_uri
        assert selected.artifact_revision == country.data_artifact_revision
    assert resolved.bundle_manifest_sha256 == bundle_digest(bundle)
    assert len(json.dumps(bundle.model_dump(mode="json"), sort_keys=True)) > 100


@pytest.mark.parametrize(
    "mutate",
    [
        lambda bundle: bundle.pop("data_releases"),
        lambda bundle: bundle["packages"]["policyengine-us"].update(
            {"install_requirement": "policyengine-us"}
        ),
        lambda bundle: bundle["data_releases"]["us"].update(
            {"policyengine_version": "different"}
        ),
        lambda bundle: bundle["data_releases"]["us"]["model_package"].update(
            {"version": "different"}
        ),
        lambda bundle: bundle["data_releases"]["us"]["datasets"][
            bundle["data_releases"]["us"]["default_dataset"]
        ].update({"revision": "different"}),
        lambda bundle: bundle["data_releases"]["us"]["datasets"][
            bundle["data_releases"]["us"]["default_dataset"]
        ].update({"sha256": "not-a-digest"}),
        lambda bundle: bundle["data_releases"]["us"]["certified_data_artifact"].update(
            {"sha256": "0" * 64}
        ),
    ],
    ids=[
        "missing-data-releases",
        "mutable-country-requirement",
        "policyengine-version-mismatch",
        "country-version-mismatch",
        "dataset-uri-revision-mismatch",
        "malformed-dataset-digest",
        "certified-artifact-mismatch",
    ],
)
def test_missing_or_internally_inconsistent_bundle_values_fail_closed(mutate) -> None:
    bundle = _bundle()
    mutate(bundle)

    with pytest.raises(Stage12BundleError):
        normalize_stage12_bundle(bundle)


def test_external_values_are_assertions_and_cannot_override_the_bundle() -> None:
    bundle = normalize_stage12_bundle(_bundle())
    us = next(country for country in bundle.countries if country.country == "us")

    assert_expected_bundle_values(
        bundle,
        {
            "policyengine_version": bundle.policyengine_version,
            "countries.us.country_package_version": us.country_package_version,
            "countries.us.default_dataset": us.default_dataset,
        },
    )
    with pytest.raises(Stage12BundleError, match="does not match"):
        assert_expected_bundle_values(
            bundle,
            {"countries.us.country_package_version": "operator-selected"},
        )
    with pytest.raises(Stage12BundleError, match="Unknown"):
        assert_expected_bundle_values(bundle, {"operator.dataset": us.default_dataset})


def test_stage12_extractor_does_not_inspect_installed_distribution_versions() -> None:
    from policyengine_simulation_executor import stage12_bundle

    source = stage12_bundle.__file__
    assert source is not None
    text = Path(source).read_text(encoding="utf-8")
    assert "importlib.metadata" not in text
    assert "pkg_resources" not in text
