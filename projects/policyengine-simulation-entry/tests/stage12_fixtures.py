"""Reusable Stage 12 test values for the Simulation Entrypoint."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

from policyengine_simulation_contract.stage12_bundle import (
    Stage12BundleManifest,
    Stage12CountryBundle,
    Stage12Dataset,
)
from policyengine_simulation_contract.stage12_execution import (
    ComparisonReportRecord,
    ComparisonRunAggregationStatus,
    ComparisonRunLifecycleStatus,
    ComparisonSimulationRecord,
    SimulationRole,
)
from policyengine_simulation_contract.stage12_manifest import (
    V2CountryWorker,
    V2WorkerValidation,
    V2WorkerVersion,
    v2_application_name,
)

NOW = datetime(2026, 9, 14, tzinfo=UTC)
EVALUATION_ID = UUID("00000000-0000-0000-0000-000000000001")


def worker() -> V2WorkerVersion:
    countries = []
    country_workers = []
    for country, package, package_version, dataset in (
        ("us", "policyengine-us", "1.764.6", "populace_us_2024"),
        ("uk", "policyengine-uk", "2.90.2", "populace_uk_2023"),
    ):
        revision = f"{dataset}-revision"
        countries.append(
            Stage12CountryBundle(
                country=country,
                country_package_name=package,
                country_package_version=package_version,
                country_package_requirement=f"{package}=={package_version}",
                data_package_name="populace-data",
                data_package_version="0.1.0",
                data_release_version=revision,
                data_artifact_revision=revision,
                default_dataset=dataset,
                default_dataset_uri=f"hf://policyengine/data/{dataset}.h5@{revision}",
                datasets=(
                    Stage12Dataset(
                        identity=dataset,
                        uri=f"hf://policyengine/data/{dataset}.h5@{revision}",
                        artifact_revision=revision,
                        sha256="a" * 64,
                        repo_type="dataset",
                    ),
                ),
            )
        )
        country_workers.append(
            V2CountryWorker(
                country=country,
                single_simulation_callable=f"run_single_simulation_{country}",
            )
        )
    application_name = v2_application_name("5.2.0")
    return V2WorkerVersion(
        application_name=application_name,
        report_coordinator_callable="coordinate_report",
        countries=tuple(country_workers),
        bundle=Stage12BundleManifest(
            policyengine_version="5.2.0",
            policyengine_requirement="policyengine==5.2.0",
            core_package_version="3.30.1",
            core_package_requirement="policyengine-core==3.30.1",
            countries=tuple(countries),
        ),
        bundle_manifest_sha256="b" * 64,
        validation=V2WorkerValidation(
            validated=True,
            application_name=application_name,
            bundle_manifest_sha256="b" * 64,
            validated_at=datetime(2026, 9, 14, tzinfo=UTC),
            validation_invocation_id="validation-1",
            country_validation_invocation_ids={
                "us": "validation-us",
                "uk": "validation-uk",
            },
        ),
    )


def eligible_payload() -> dict:
    return {
        "country": "us",
        "scope": "macro",
        "baseline": {},
        "reform": {"gov.irs.credits.ctc.amount.base[0].amount": {"2026": 3000}},
        "time_period": "2026",
        "region": "us",
        "include_cliffs": False,
        "version": "1.764.6",
        "policyengine_version": "5.2.0",
    }


def comparison_report(
    *,
    status: ComparisonRunLifecycleStatus = ComparisonRunLifecycleStatus.RUNNING,
    environment: str = "staging",
) -> ComparisonReportRecord:
    values = {
        "evaluation_id": EVALUATION_ID,
        "status": status,
        "aggregation_status": ComparisonRunAggregationStatus.RUNNING,
        "environment": environment,
        "calculation_flow": "economy",
        "originating_request_id": "request-1",
        "production_identity": f"direct:{EVALUATION_ID}",
        "incumbent_execution_id": None,
        "worker_version": "5.2.0",
        "modal_application": "policyengine-simulation-v2-py5-2-0",
        "report_coordinator_callable": "coordinate_report",
        "version_manifest_sha256": "c" * 64,
        "policyengine_version": "5.2.0",
        "country_package_name": "policyengine-us",
        "country_package_version": "1.764.6",
        "country": "us",
        "dataset_identity": "populace_us_2024",
        "dataset_uri": "hf://policyengine/data/populace_us_2024.h5@revision",
        "data_package_name": "populace-data",
        "data_package_version": "0.1.0",
        "data_artifact_revision": "revision",
        "coordinator_invocation_id": "modal-call-1",
        "created_at": NOW,
        "updated_at": NOW,
        "started_at": NOW,
        "retention_expires_at": NOW + timedelta(days=30),
    }
    if status is ComparisonRunLifecycleStatus.SUCCEEDED:
        values.update(
            {
                "aggregation_status": ComparisonRunAggregationStatus.SUCCEEDED,
                "aggregate_output_uri": "gs://stage12-private/report.json",
                "aggregate_output_sha256": "d" * 64,
                "aggregate_schema_version": 1,
                "completed_at": NOW,
            }
        )
    elif status is ComparisonRunLifecycleStatus.FAILED:
        values.update(
            {
                "aggregation_status": ComparisonRunAggregationStatus.FAILED,
                "error_code": "comparison_dispatch_failed",
                "error_summary": "RuntimeError",
                "completed_at": NOW,
            }
        )
    return ComparisonReportRecord.model_validate(values)


def comparison_simulation(role: SimulationRole) -> ComparisonSimulationRecord:
    suffix = 2 if role is SimulationRole.BASELINE else 3
    return ComparisonSimulationRecord(
        simulation_execution_id=UUID(f"00000000-0000-0000-0000-{suffix:012d}"),
        evaluation_id=EVALUATION_ID,
        role=role,
        input_sha256=("a" if role is SimulationRole.BASELINE else "b") * 64,
        worker_version="5.2.0",
        modal_application="policyengine-simulation-v2-py5-2-0",
        simulation_callable="run_single_simulation_us",
        version_manifest_sha256="c" * 64,
        modal_invocation_id=f"modal-{role.value}",
        status=ComparisonRunLifecycleStatus.RUNNING,
        created_at=NOW,
        updated_at=NOW,
        started_at=NOW,
        retention_expires_at=NOW + timedelta(days=30),
    )
