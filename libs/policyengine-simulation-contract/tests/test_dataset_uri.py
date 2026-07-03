"""Tests for dataset URI normalization."""

import pytest

from policyengine_simulation_contract.dataset_uri import runtime_dataset_uri


def test_runtime_dataset_uri_preserves_populace_hf_artifact_without_hf_validation(
    monkeypatch,
):
    def reject_hf_validation(dataset_uri: str, revision: str) -> str:
        raise AssertionError(
            f"HF validation should not run for trusted bundle data: {dataset_uri}@{revision}"
        )

    monkeypatch.setattr(
        "policyengine_simulation_contract.dataset_uri.with_hf_revision",
        reject_hf_validation,
    )

    assert (
        runtime_dataset_uri(
            "hf://policyengine/populace-uk-private/"
            "populace_uk_2023.h5@uk-artifact-revision",
            default_revision="populace-uk-2023-release",
            artifact_revision="uk-artifact-revision",
            validate_hf=False,
        )
        == (
            "hf://policyengine/populace-uk-private/"
            "populace_uk_2023.h5@uk-artifact-revision"
        )
    )


def test_runtime_dataset_uri_preserves_explicit_hf_data_version():
    assert (
        runtime_dataset_uri(
            "hf://external/example-data/file.h5@custom-v1",
            default_revision="bundle-default",
            artifact_revision="artifact-revision",
            validate_hf=False,
        )
        == "hf://external/example-data/file.h5@custom-v1"
    )


def test_runtime_dataset_uri_override_revision_wins_for_hf_uri():
    assert (
        runtime_dataset_uri(
            "hf://external/example-data/file.h5@custom-v1",
            default_revision="bundle-default",
            override_revision="custom-v2",
            artifact_revision="artifact-revision",
            validate_hf=False,
        )
        == "hf://external/example-data/file.h5@custom-v2"
    )


def test_runtime_dataset_uri_still_validates_unmanaged_hf_revisions(monkeypatch):
    def pin_hf_revision(dataset_uri: str, revision: str) -> str:
        return f"{dataset_uri.rsplit('@', maxsplit=1)[0]}@{revision}"

    monkeypatch.setattr(
        "policyengine_simulation_contract.dataset_uri.with_hf_revision",
        pin_hf_revision,
    )

    assert (
        runtime_dataset_uri(
            "hf://external/example-data/file.h5@old",
            override_revision="new",
        )
        == "hf://external/example-data/file.h5@new"
    )


def test_runtime_dataset_uri_rejects_conflicting_gcs_revisions():
    with pytest.raises(ValueError, match="Conflicting dataset revisions"):
        runtime_dataset_uri(
            "gs://external-bucket/custom/file.h5@custom-v1",
            default_revision="custom-v2",
        )
