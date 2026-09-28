"""Tests for the separate Stage 12 v2 manifest publisher."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from policyengine_simulation_contract.stage12_manifest import V2WorkerValidation
from src.modal.utils import update_v2_version_manifest as publisher


def _validation() -> V2WorkerValidation:
    resolved = publisher.load_stage12_bundle()
    application_name = publisher.v2_application_name(
        resolved.bundle.policyengine_version
    )
    return V2WorkerValidation(
        validated=True,
        application_name=application_name,
        bundle_manifest_sha256=resolved.bundle_manifest_sha256,
        validated_at=datetime(2026, 9, 14, tzinfo=timezone.utc),
        validation_invocation_id="validation-release-1",
        country_validation_invocation_ids={
            "us": "validation-us-1",
            "uk": "validation-uk-1",
        },
    )


def test_publisher_uses_only_packaged_bundle_values() -> None:
    worker = publisher.build_worker(_validation().model_dump(mode="json"))
    resolved = publisher.load_stage12_bundle()

    assert worker.bundle == resolved.bundle
    assert worker.bundle_manifest_sha256 == resolved.bundle_manifest_sha256
    assert worker.application_name == publisher.v2_application_name(
        resolved.bundle.policyengine_version
    )


def test_publisher_rejects_validation_for_another_application() -> None:
    payload = _validation().model_dump(mode="json")
    payload["application_name"] = "policyengine-simulation-v2-py-another"

    with pytest.raises(ValueError, match="another application"):
        publisher.build_worker(payload)


def test_deployment_workflow_keeps_v2_optional_and_separate() -> None:
    workflow = (
        publisher.Path(__file__).resolve().parents[3]
        / ".github/workflows/simulation-deploy.reusable.yml"
    ).read_text(encoding="utf-8")

    assert "deploy_stage12_v2:" in workflow
    assert "default: false" in workflow
    assert "src/modal/v2_app.py" in workflow
    assert "simulation-api-v2-version-manifest" in workflow
    assert "src.modal.utils.update_v2_version_manifest" in workflow
    update_job = workflow.index("\n  update_routing:")
    v2_job = workflow.index("\n  deploy_stage12_v2:")
    assert update_job < v2_job
    v2_section = workflow[v2_job:]
    assert "src.modal.utils.update_version_registry" not in v2_section
    deploy_section = v2_section[: v2_section.index("\n  publish_stage12_v2_manifest:")]
    for declaration in (
        "OBSERVABILITY_SERVICE_NAMESPACE: ${{ vars.OBSERVABILITY_SERVICE_NAMESPACE }}",
        "OBSERVABILITY_TRACE_PROJECT_ID: ${{ vars.OBSERVABILITY_TRACE_PROJECT_ID }}",
        "OBSERVABILITY_LOGGING_PROJECT_ID: ${{ vars.OBSERVABILITY_LOGGING_PROJECT_ID }}",
        "OBSERVABILITY_LOG_NAME: ${{ vars.OBSERVABILITY_LOG_NAME }}",
        "OBSERVABILITY_GOOGLE_WORKLOAD_IDENTITY_PROVIDER: ${{ vars.OBSERVABILITY_GOOGLE_WORKLOAD_IDENTITY_PROVIDER }}",
        "OBSERVABILITY_GOOGLE_SERVICE_ACCOUNT_EMAIL: ${{ vars.OBSERVABILITY_GOOGLE_SERVICE_ACCOUNT_EMAIL }}",
        "OTEL_EXPORTER_OTLP_ENDPOINT: ${{ vars.OBSERVABILITY_OTLP_ENDPOINT }}",
        "POLICYENGINE_OTEL_GOOGLE_AUDIENCE: ${{ vars.OBSERVABILITY_OTLP_GOOGLE_AUDIENCE }}",
    ):
        assert declaration in deploy_section
