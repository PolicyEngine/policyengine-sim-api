"""Tests for complete UK Hugging Face bundle access validation."""

from __future__ import annotations

import pytest

from policyengine_simulation_executor.stage12_bundle import load_stage12_bundle
from policyengine_simulation_executor.uk_hf_access import (
    validate_uk_hf_dataset_access,
)


def _uk_dataset_uris() -> tuple[str, ...]:
    bundle = load_stage12_bundle().bundle
    uk_bundle = next(country for country in bundle.countries if country.country == "uk")
    return tuple(dataset.uri for dataset in uk_bundle.datasets)


def test_validation_exercises_every_uk_dataset_declared_by_bundle() -> None:
    validated_uris: list[str] = []

    def record_validation(dataset_uri: str, repository_type: str) -> str:
        validated_uris.append(dataset_uri)
        return dataset_uri

    report = validate_uk_hf_dataset_access(
        environment={"HF_TOKEN": "test-token"},
        uri_validator=record_validation,
    )

    expected_uris = _uk_dataset_uris()
    assert tuple(validated_uris) == expected_uris
    assert report.country == "uk"
    assert report.dataset_count == len(expected_uris)
    assert report.dataset_count > 0
    assert report.repository_ids
    assert report.repository_types == ("model",)
    assert report.revisions


def test_validation_requires_the_exact_modal_hf_token_variable() -> None:
    with pytest.raises(RuntimeError, match="HF_TOKEN is required"):
        validate_uk_hf_dataset_access(
            environment={"HUGGINGFACE_TOKEN": "wrong-variable"},
            uri_validator=lambda dataset_uri, repository_type: dataset_uri,
        )


def test_validation_propagates_an_inaccessible_artifact_failure() -> None:
    first_uri = _uk_dataset_uris()[0]

    def reject_first_artifact(dataset_uri: str, repository_type: str) -> str:
        if dataset_uri == first_uri:
            raise RuntimeError("artifact is inaccessible")
        return dataset_uri

    with pytest.raises(RuntimeError, match="artifact is inaccessible"):
        validate_uk_hf_dataset_access(
            environment={"HF_TOKEN": "test-token"},
            uri_validator=reject_first_artifact,
        )
