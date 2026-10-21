"""Strict Stage 12 bundle authority tests."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest
from policyengine.bundle import get_current_bundle
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
        lambda bundle: bundle["data_releases"]["us"]["region_datasets"].update(
            {"state": {"path_template": "no_such_dataset.h5"}}
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
        "region-dataset-names-no-dataset",
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
    package = Path(source).parent
    text = "\n".join(
        module.read_text(encoding="utf-8") for module in sorted(package.glob("*.py"))
    )
    assert "importlib.metadata" not in text
    assert "pkg_resources" not in text


@pytest.mark.parametrize("schema_version", [None, 1, 3, "2"])
def test_only_bundle_schema_version_2_is_supported(schema_version) -> None:
    bundle = {**_bundle(), "schema_version": schema_version}

    with pytest.raises(
        Stage12BundleError, match=r"^PolicyEngine\.py bundle schema_version must be 2$"
    ):
        normalize_stage12_bundle(bundle)


def _us(bundle):
    return next(country for country in bundle.countries if country.country == "us")


def test_the_installed_bundle_names_the_dataset_each_us_region_type_loads() -> None:
    us = _us(normalize_stage12_bundle(_bundle()))

    assert us.dataset_for_region_type("national") == "populace_us_2024"
    assert us.dataset_for_region_type("state") == "populace_us_2024_acs_local"
    assert (
        us.dataset_for_region_type("congressional_district")
        == "populace_us_2024_acs_local"
    )


def test_a_per_region_file_pattern_names_no_single_dataset() -> None:
    bundle = _bundle()
    bundle["data_releases"]["us"]["region_datasets"]["state"] = {
        "path_template": "states/{state_code}.h5"
    }

    us = _us(normalize_stage12_bundle(bundle))

    assert any(item.region_type == "state" for item in us.region_datasets)
    assert us.dataset_for_region_type("state") is None


def test_a_bundle_without_region_datasets_records_none() -> None:
    bundle = _bundle()
    bundle["data_releases"]["us"].pop("region_datasets")

    assert _us(normalize_stage12_bundle(bundle)).region_datasets is None


def test_region_datasets_leave_the_bundle_digest_unchanged() -> None:
    """A worker redeployed with region datasets keeps the digest it had.

    The redeploy replaces the running v2 application before the new version
    manifest is published, and entry services send the digest they read from
    that manifest, so a changed digest would refuse every run in between.
    Region datasets come from the same .py bundle, whose version the digest
    already covers.
    """
    bundle = normalize_stage12_bundle(_bundle())
    without_region_datasets = bundle.model_copy(
        update={
            "countries": tuple(
                country.model_copy(update={"region_datasets": None})
                for country in bundle.countries
            )
        }
    )

    assert _us(bundle).region_datasets is not None
    assert bundle_digest(bundle) == bundle_digest(without_region_datasets)


def test_a_dataset_sharing_the_default_path_does_not_replace_the_default() -> None:
    bundle = _bundle()
    release = bundle["data_releases"]["us"]
    release["datasets"]["a_alias_of_the_default"] = deepcopy(
        release["datasets"][release["default_dataset"]]
    )

    us = _us(normalize_stage12_bundle(bundle))

    assert us.dataset_for_region_type("national") == us.default_dataset
