"""Tests for policyengine.py release bundle helpers."""

from copy import deepcopy
from unittest.mock import MagicMock

import pytest
from policyengine.bundle import get_current_bundle
from policyengine.provenance import (
    get_release_manifest,
    https_dataset_uri,
    materialize_dataset,
)
from policyengine_simulation_executor import release_bundle as release_bundle_module
from policyengine_simulation_executor.release_bundle import (
    BUNDLE_RECEIPT_FILENAME,
    get_country_release_bundle,
    resolve_bundle_dataset_name,
    resolve_bundle_dataset_uri,
    resolve_runtime_bundle_dataset_uri,
)


@pytest.fixture(autouse=True)
def stub_hf_revision_validation(monkeypatch):
    def with_revision(dataset_uri, revision):
        return (
            f"{dataset_uri.rsplit('@', maxsplit=1)[0]}@{revision}"
            if dataset_uri.startswith("hf://")
            else dataset_uri
        )

    monkeypatch.setattr(
        "policyengine_simulation_contract.dataset_uri.with_hf_revision",
        with_revision,
    )


@pytest.fixture
def policyengine_uk_data_release(monkeypatch):
    """Expose the UK package-data release introduced by policyengine.py 5.0.4."""

    bundle = deepcopy(get_current_bundle())
    bundle["bundle_version"] = "5.0.4"
    bundle["policyengine_version"] = "5.0.4"
    bundle["packages"]["policyengine-uk"]["version"] = "2.90.2"
    bundle["data_releases"]["uk"] = {
        "version": "policyengine-uk-data-1.56.16",
        "build_id": "policyengine-uk-data-1.56.16",
        "data_package": {
            "name": "policyengine-uk-data",
            "version": "1.56.16",
            "repo_id": "policyengine/policyengine-uk-data-private",
            "repo_type": "model",
            "release_manifest_revision": "uk-release-manifest-commit",
        },
        "certified_data_artifact": {
            "dataset": "enhanced_frs_2024_25",
            "uri": (
                "hf://policyengine/policyengine-uk-data-private/"
                "enhanced_frs_2024_25.h5@1.56.16"
            ),
        },
        "default_dataset": "enhanced_frs_2024_25",
        "default_dataset_uri": (
            "hf://policyengine/policyengine-uk-data-private/"
            "enhanced_frs_2024_25.h5@1.56.16"
        ),
        "datasets": {
            "enhanced_frs_2024_25": {
                "path": "enhanced_frs_2024_25.h5",
                "repo_id": "policyengine/policyengine-uk-data-private",
                "repo_type": "model",
                "revision": "1.56.16",
            },
            "populace_uk_2023": {
                "path": "populace_uk_2023.h5",
                "repo_id": "policyengine/populace-uk-private",
                "repo_type": "dataset",
                "revision": "populace-uk-2023-release",
            },
        },
    }
    monkeypatch.setattr(
        release_bundle_module,
        "_current_policyengine_bundle",
        lambda: bundle,
    )
    get_country_release_bundle.cache_clear()
    yield bundle["data_releases"]["uk"]
    get_country_release_bundle.cache_clear()


def test_country_release_bundle_exposes_model_and_data_versions():
    manifest = get_current_bundle()

    for country in ("us", "uk"):
        bundle = get_country_release_bundle(country)
        release = manifest["data_releases"][country]

        assert bundle.model_package_name == f"policyengine-{country}"
        assert bundle.model_version
        assert bundle.data_package_name == release["data_package"]["name"]
        assert bundle.data_package_version == release["data_package"]["version"]
        assert bundle.data_version == release["version"]
        assert bundle.data_artifact_revision
        assert bundle.default_dataset == release["default_dataset"]
        assert bundle.default_dataset_uri == release["default_dataset_uri"]


def test_policyengine_data_release_keeps_build_and_package_versions_distinct(
    policyengine_uk_data_release,
):
    bundle = get_country_release_bundle("uk")

    assert bundle.data_version == "policyengine-uk-data-1.56.16"
    assert bundle.data_package_version == "1.56.16"
    assert bundle.data_artifact_revision == "1.56.16"


def test_resolve_bundle_dataset_name_uses_manifest_default():
    assert (
        resolve_bundle_dataset_name("us", None)
        == get_country_release_bundle("us").default_dataset
    )
    assert (
        resolve_bundle_dataset_name("uk", None)
        == get_country_release_bundle("uk").default_dataset
    )


def test_resolve_bundle_dataset_uri_maps_certified_defaults_to_manifest_uris():
    assert (
        resolve_bundle_dataset_uri(
            "us", get_country_release_bundle("us").default_dataset
        )
        == get_country_release_bundle("us").default_dataset_uri
    )
    assert (
        resolve_bundle_dataset_uri(
            "uk", get_country_release_bundle("uk").default_dataset
        )
        == get_country_release_bundle("uk").default_dataset_uri
    )


def test_resolve_bundle_dataset_uri_does_not_certify_unknown_dataset_labels():
    bundle = get_country_release_bundle("us")

    assert "custom_dataset_label" not in bundle.dataset_uris
    assert (
        resolve_bundle_dataset_uri("us", "custom_dataset_label")
        == "custom_dataset_label"
    )


def test_resolve_bundle_dataset_uri_maps_dataset_names_to_their_own_bundle_uris():
    for country, dataset in (
        ("us", "populace_us_2024"),
        ("uk", "populace_uk_2023"),
    ):
        bundle = get_country_release_bundle(country)

        assert (
            resolve_bundle_dataset_uri(country, dataset) == bundle.dataset_uris[dataset]
        )


def test_resolve_bundle_dataset_uri_preserves_explicit_dataset_uri_and_revision():
    uri = "hf://external/example-data/file.h5@custom-v1"

    assert resolve_bundle_dataset_name("us", uri) == uri
    assert resolve_bundle_dataset_uri("us", uri) == uri


def test_resolve_bundle_dataset_uri_maps_explicit_logical_revision_to_hf_uri():
    dataset = "populace_us_2024@custom-v1"

    assert resolve_bundle_dataset_name("us", dataset).startswith(
        "hf://policyengine/populace-us/populace_us_2024.h5@custom-v1"
    )
    assert resolve_bundle_dataset_uri("us", dataset).startswith(
        "hf://policyengine/populace-us/populace_us_2024.h5@custom-v1"
    )


def test_resolve_bundle_dataset_uri_preserves_explicit_gcs_uri():
    uri = "gs://external-bucket/custom/file.h5"

    assert resolve_bundle_dataset_name("us", uri) == uri
    assert resolve_bundle_dataset_uri("us", uri) == uri


def test_resolve_bundle_dataset_uri_preserves_unmanaged_unknown_values():
    assert resolve_bundle_dataset_uri("us", "custom_dataset_label") == (
        "custom_dataset_label"
    )


def test_resolve_bundle_dataset_uri_rejects_unknown_logical_revision():
    with pytest.raises(ValueError, match="Unknown dataset revision reference"):
        resolve_bundle_dataset_uri("us", "custom_dataset_label@1.0.0")


def test_resolve_runtime_bundle_dataset_uri_preserves_current_default_reference():
    bundle = get_country_release_bundle("us")

    assert resolve_runtime_bundle_dataset_uri("us", None) == bundle.default_dataset_uri


def test_resolve_runtime_bundle_dataset_uri_maps_dataset_name_to_its_bundle_uri():
    bundle = get_country_release_bundle("uk")

    assert (
        resolve_runtime_bundle_dataset_uri("uk", "populace_uk_2023")
        == bundle.dataset_uris["populace_uk_2023"]
    )


def test_policyengine_data_release_default_resolves_to_gcs_package_version(
    policyengine_uk_data_release,
):
    assert resolve_runtime_bundle_dataset_uri("uk", None, prefer_local=False) == (
        "gs://policyengine-uk-data-private/enhanced_frs_2024_25.h5@1.56.16"
    )


def test_policyengine_data_release_preserves_explicit_legacy_populace_dataset(
    policyengine_uk_data_release,
):
    assert resolve_runtime_bundle_dataset_uri(
        "uk", "populace_uk_2023", prefer_local=False
    ) == (
        "hf://policyengine/populace-uk-private/"
        "populace_uk_2023.h5@populace-uk-2023-release"
    )


def test_policyengine_dataset_interface_downloads_certified_uk_bundle_reference(
    tmp_path,
    monkeypatch,
):
    manifest = get_release_manifest("uk")
    reference = manifest.datasets[manifest.default_dataset]
    response = MagicMock()
    response.__enter__.return_value = response
    response.status_code = 200
    response.iter_content.return_value = [b"dataset"]
    get = MagicMock(return_value=response)
    monkeypatch.setattr(
        "policyengine.provenance.dataset_materialization.requests.get",
        get,
    )
    monkeypatch.setattr(
        "policyengine.provenance.dataset_materialization.sha256_file",
        lambda _: reference.sha256,
    )

    result = materialize_dataset("uk", data_dir=tmp_path)

    assert result.bundle_dataset is not None
    assert result.bundle_dataset.data_package_name == (
        reference.data_package_name or manifest.data_package.name
    )
    assert result.bundle_dataset.repo_type == (
        reference.repo_type or manifest.data_package.repo_type
    )
    assert result.bundle_dataset.revision == reference.revision
    assert result.bundle_dataset.sha256 == reference.sha256
    assert result.bundle_dataset.path.read_bytes() == b"dataset"
    assert get.call_args.args[0] == https_dataset_uri(
        reference.repo_id or manifest.data_package.repo_id,
        reference.path,
        reference.revision,
        repo_type=reference.repo_type or manifest.data_package.repo_type,
    )


def test_resolve_runtime_bundle_dataset_uri_applies_requested_version():
    bundle_uri = get_country_release_bundle("us").default_dataset_uri
    bundle_uri_without_revision = bundle_uri.rsplit("@", maxsplit=1)[0]

    assert (
        resolve_runtime_bundle_dataset_uri(
            "us",
            "populace_us_2024",
            "custom-v1",
        )
        == f"{bundle_uri_without_revision}@custom-v1"
    )


def test_resolve_runtime_bundle_dataset_uri_preserves_explicit_hf_data_version():
    assert (
        resolve_runtime_bundle_dataset_uri(
            "us",
            "hf://external/example-data/file.h5@custom-v1",
        )
        == "hf://external/example-data/file.h5@custom-v1"
    )


def test_resolve_runtime_bundle_dataset_uri_preserves_explicit_gcs_data_version():
    assert (
        resolve_runtime_bundle_dataset_uri(
            "us",
            "gs://external-bucket/custom/file.h5@custom-v1",
        )
        == "gs://external-bucket/custom/file.h5@custom-v1"
    )


def test_resolve_runtime_bundle_dataset_uri_preserves_unmanaged_unknown_values():
    assert (
        resolve_runtime_bundle_dataset_uri("us", "custom_dataset_label")
        == "custom_dataset_label"
    )


def test_resolve_runtime_bundle_dataset_uri_preserves_explicit_gcs_uri():
    uri = "gs://external-bucket/custom/file.h5"

    assert resolve_runtime_bundle_dataset_uri("us", uri) == uri


def test_resolve_runtime_bundle_dataset_uri_prefers_installed_default_dataset(
    tmp_path, monkeypatch
):
    bundle = get_country_release_bundle("us")
    dataset_path = tmp_path / f"{bundle.default_dataset}.h5"
    dataset_path.write_bytes(b"data")
    receipt_path = tmp_path / BUNDLE_RECEIPT_FILENAME
    receipt_path.write_text(
        """
        {
          "bundle_version": "4.18.3",
          "policyengine_version": "4.18.3",
          "datasets": [
            {
              "country": "us",
              "dataset": "%s",
              "version": "%s",
              "path": "%s"
            }
          ]
        }
        """
        % (bundle.default_dataset, bundle.data_version, str(dataset_path)),
        encoding="utf-8",
    )
    monkeypatch.setenv("POLICYENGINE_BUNDLE_RECEIPT", str(receipt_path))
    get_country_release_bundle.cache_clear()

    assert resolve_runtime_bundle_dataset_uri("us", None) == str(dataset_path)
    assert resolve_runtime_bundle_dataset_uri("us", bundle.default_dataset) == str(
        dataset_path
    )


def test_resolve_runtime_bundle_dataset_uri_preserves_nondefault_override_with_receipt(
    tmp_path, monkeypatch
):
    bundle = get_country_release_bundle("us")
    dataset_path = tmp_path / f"{bundle.default_dataset}.h5"
    dataset_path.write_bytes(b"data")
    receipt_path = tmp_path / BUNDLE_RECEIPT_FILENAME
    receipt_path.write_text(
        """
        {
          "datasets": [
            {
              "country": "us",
              "dataset": "%s",
              "version": "%s",
              "path": "%s"
            }
          ]
        }
        """
        % (bundle.default_dataset, bundle.data_version, str(dataset_path)),
        encoding="utf-8",
    )
    monkeypatch.setenv("POLICYENGINE_BUNDLE_RECEIPT", str(receipt_path))
    get_country_release_bundle.cache_clear()

    assert (
        resolve_runtime_bundle_dataset_uri("us", "custom_dataset_label")
        == "custom_dataset_label"
    )
