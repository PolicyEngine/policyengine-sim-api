"""Tests for deployed Stage 12 release validation."""

from __future__ import annotations

import pytest

from src.modal.utils import validate_v2_worker_release as validation


def test_all_country_results_must_match_the_deployment_bundle(monkeypatch) -> None:
    invocation_ids: list[str] = []

    def fake_spawn_validation(*, application_name, function_name, environment):
        country = function_name.rsplit("_", maxsplit=1)[1]
        resolved = validation.load_stage12_bundle()
        bundle = next(
            item for item in resolved.bundle.countries if item.country == country
        )
        invocation_id = f"call-{country}"
        invocation_ids.append(invocation_id)
        return (
            {
                "validated": True,
                "country": country,
                "policyengine_version": resolved.bundle.policyengine_version,
                "country_package_name": bundle.country_package_name,
                "country_package_version": bundle.country_package_version,
                "dataset_identity": bundle.default_dataset,
                "dataset_uri": bundle.default_dataset_uri,
                "data_artifact_revision": bundle.data_artifact_revision,
                "bundle_manifest_sha256": resolved.bundle_manifest_sha256,
            },
            invocation_id,
        )

    monkeypatch.setattr(validation, "_spawn_validation", fake_spawn_validation)

    result = validation.validate_release(environment="staging")

    assert result.validated is True
    assert result.country_validation_invocation_ids == {
        "us": "call-us",
        "uk": "call-uk",
    }
    assert invocation_ids == ["call-us", "call-uk"]


def test_country_result_mismatch_stops_publication(monkeypatch) -> None:
    monkeypatch.setattr(
        validation,
        "_spawn_validation",
        lambda **_: (
            {
                "validated": True,
                "country": "us",
                "policyengine_version": "different",
            },
            "call-us",
        ),
    )

    with pytest.raises(RuntimeError, match="differ from deployment"):
        validation.validate_release(environment="staging")
