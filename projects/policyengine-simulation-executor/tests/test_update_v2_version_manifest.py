"""Tests for the separate Stage 12 v2 manifest publisher."""

from __future__ import annotations

import re
from datetime import UTC, datetime

import pytest
from policyengine_simulation_contract.stage12_manifest import (
    LOCAL_AREA_REPORTS_VERSION,
    LocalAreaReports,
    V2WorkerValidation,
)
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
        validated_at=datetime(2026, 9, 14, tzinfo=UTC),
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


def test_publisher_records_every_bundle_country_as_economy_capable() -> None:
    worker = publisher.build_worker(_validation().model_dump(mode="json"))

    assert [
        (
            country.country,
            country.economy_capable,
            country.single_simulation_callable,
            country.household_callable,
        )
        for country in worker.countries
    ] == [
        (country.country, True, f"run_single_simulation_{country.country}", None)
        for country in publisher.load_stage12_bundle().bundle.countries
    ]


def _local_area_reports(worker) -> dict[str, LocalAreaReports | None]:
    return {country.country: country.local_area_reports for country in worker.countries}


def test_publisher_records_the_uk_local_area_dataset_its_coordinator_runs_on() -> None:
    from policyengine_simulation_executor.uk_local_authority_metadata import (
        routed_uk_area_dataset_name,
        uk_area_regions_routed,
    )

    assert uk_area_regions_routed()
    worker = publisher.build_worker(_validation().model_dump(mode="json"))

    assert _local_area_reports(worker) == {
        "us": None,
        "uk": LocalAreaReports(
            input_version=LOCAL_AREA_REPORTS_VERSION,
            dataset=routed_uk_area_dataset_name(),
        ),
    }


def test_publisher_records_no_dataset_when_uk_national_runs_keep_the_areas(
    monkeypatch,
) -> None:
    monkeypatch.setattr(publisher, "uk_area_regions_routed", lambda: False)

    worker = publisher.build_worker(_validation().model_dump(mode="json"))

    assert _local_area_reports(worker)["uk"] == LocalAreaReports(
        input_version=LOCAL_AREA_REPORTS_VERSION, dataset=None
    )


def test_publisher_records_each_validated_household_function() -> None:
    payload = _validation().model_dump(mode="json")
    payload["household_validation_invocation_ids"] = {"uk": "household-uk-1"}

    worker = publisher.build_worker(payload)

    assert {
        country.country: country.household_callable for country in worker.countries
    } == {"us": None, "uk": "calculate_household_uk"}
    assert worker.household_worker("uk") is not None
    assert worker.household_worker("us") is None


def test_publisher_rejects_validation_for_another_application() -> None:
    payload = _validation().model_dump(mode="json")
    payload["application_name"] = "policyengine-simulation-v2-py-another"

    with pytest.raises(ValueError, match="another application"):
        publisher.build_worker(payload)


def test_deployment_workflow_keeps_v2_optional_and_separate() -> None:
    repository_root = publisher.Path(__file__).resolve().parents[3]
    workflow = (
        repository_root / ".github/workflows/simulation-deploy.reusable.yml"
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
    # The Stage 12 jobs never touch v1 routing; revert_routing (after them in
    # the file) restores v1 routing only for existing-stack deployments.
    stage12_jobs = v2_section[: v2_section.index("\n  integration:")]
    assert "src.modal.utils.update_version_registry" not in stage12_jobs
    # Nor do the repository scripts those jobs run.
    called_scripts = re.findall(r"\.github/scripts/([\w.-]+\.sh)", stage12_jobs)
    assert called_scripts
    for name in called_scripts:
        script = (repository_root / ".github/scripts" / name).read_text()
        assert "update_version_registry" not in script, name
    deploy_section = v2_section[: v2_section.index("\n  publish_stage12_v2_manifest:")]
    for declaration in (
        "OBSERVABILITY_SERVICE_NAMESPACE: ${{ vars.OBSERVABILITY_SERVICE_NAMESPACE }}",
        "OBSERVABILITY_TRACE_PROJECT_ID: ${{ vars.OBSERVABILITY_TRACE_PROJECT_ID }}",
        (
            "OBSERVABILITY_LOGGING_PROJECT_ID: "
            "${{ vars.OBSERVABILITY_LOGGING_PROJECT_ID }}"
        ),
        "OBSERVABILITY_LOG_NAME: ${{ vars.OBSERVABILITY_LOG_NAME }}",
        (
            "OBSERVABILITY_GOOGLE_WORKLOAD_IDENTITY_PROVIDER: "
            "${{ vars.OBSERVABILITY_GOOGLE_WORKLOAD_IDENTITY_PROVIDER }}"
        ),
        (
            "OBSERVABILITY_GOOGLE_SERVICE_ACCOUNT_EMAIL: "
            "${{ vars.OBSERVABILITY_GOOGLE_SERVICE_ACCOUNT_EMAIL }}"
        ),
        "OTEL_EXPORTER_OTLP_ENDPOINT: ${{ vars.OBSERVABILITY_OTLP_ENDPOINT }}",
        (
            "POLICYENGINE_OTEL_GOOGLE_AUDIENCE: "
            "${{ vars.OBSERVABILITY_OTLP_GOOGLE_AUDIENCE }}"
        ),
    ):
        assert declaration in deploy_section
