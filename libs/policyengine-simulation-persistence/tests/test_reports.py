"""Report tables: simulation.reports and simulation.report_results."""

from __future__ import annotations

from uuid import uuid4

import pytest
from job_store_support import new_job, running_job, runtime_store
from policyengine_simulation_persistence import create_simulation_engine
from policyengine_simulation_persistence.jobs import JobStatus, result_sha256
from policyengine_simulation_persistence.reports import (
    NewReport,
    ReportArtifact,
    ReportKind,
    ReportStatus,
    ReportStore,
    UnsupportedReportAggregateError,
    report_summary,
    validate_report_aggregate,
)
from pydantic import ValidationError
from report_support import economy_aggregate
from sqlalchemy.exc import IntegrityError

ARTIFACT = ReportArtifact(
    role="baseline_simulation",
    uri="gs://policyengine-simulation-artifacts/reports/baseline.parquet",
    sha256="a" * 64,
    media_type="application/vnd.apache.parquet",
)


# --- Validation, without a database ------------------------------------------


def test_an_economy_aggregate_is_validated_by_the_single_year_model() -> None:
    validated = validate_report_aggregate(ReportKind.ECONOMY, 1, economy_aggregate())

    assert validated.budget.budgetary_impact == 70.0


def test_an_invalid_economy_aggregate_is_rejected() -> None:
    with pytest.raises(ValidationError):
        validate_report_aggregate(
            ReportKind.ECONOMY, 1, economy_aggregate(budget={"budgetary_impact": 1})
        )
    with pytest.raises(ValidationError):
        validate_report_aggregate(
            ReportKind.ECONOMY, 1, economy_aggregate(unexpected_field=True)
        )


@pytest.mark.parametrize(
    ("kind", "schema_version"),
    [(ReportKind.HOUSEHOLD, 1), (ReportKind.ECONOMY, 2)],
)
def test_aggregates_without_a_registered_model_are_refused(kind, schema_version):
    with pytest.raises(UnsupportedReportAggregateError):
        validate_report_aggregate(kind, schema_version, economy_aggregate())


def test_economy_summary_columns_come_from_the_validated_aggregate() -> None:
    validated = validate_report_aggregate(ReportKind.ECONOMY, 1, economy_aggregate())

    assert report_summary(ReportKind.ECONOMY, validated) == {
        "budgetary_impact": 70.0,
        "tax_revenue_impact": 100.0,
        "state_tax_revenue_impact": 20.0,
        "benefit_spending_impact": 30.0,
        "households": 2.0,
        "baseline_net_income": 1000.0,
        "model_version": "1.715.2",
        "data_version": "1.115.5",
    }


@pytest.mark.parametrize(
    "overrides",
    [
        {"sha256": "A" * 64},
        {"sha256": "a" * 63},
        {"uri": "file:///tmp/baseline.parquet"},
        {"role": "Baseline Simulation"},
    ],
)
def test_artifact_references_are_validated(overrides) -> None:
    with pytest.raises(ValidationError):
        ReportArtifact.model_validate(ARTIFACT.model_dump() | overrides)


# --- Reports ------------------------------------------------------------------------


def _report_store(database, clock=None):
    from job_store_support import SteppingClock

    return ReportStore(
        create_simulation_engine(database.runtime_url),
        clock=clock or SteppingClock(),
    )


@pytest.mark.postgres
def test_a_migrated_report_starts_not_started_and_is_created_once(
    migrated_database,
) -> None:
    reports = _report_store(migrated_database)

    first = reports.create_migrated_report(
        legacy_report_id=4321, kind=ReportKind.ECONOMY, country="us"
    )
    second = reports.create_migrated_report(
        legacy_report_id=4321, kind=ReportKind.ECONOMY, country="us"
    )

    assert first.status is ReportStatus.NOT_STARTED
    assert first.job_id is None
    assert second.id == first.id
    assert reports.get_by_legacy_report_id(4321).id == first.id
    assert reports.get(first.id).kind is ReportKind.ECONOMY


@pytest.mark.postgres
def test_a_submitted_report_names_its_job(migrated_database) -> None:
    jobs = runtime_store(migrated_database)
    reports = _report_store(migrated_database)
    job = jobs.submit(new_job(kind="economy_report")).job

    report = reports.create_report(
        NewReport(kind=ReportKind.ECONOMY, country="uk", job_id=job.id)
    )

    assert report.status is ReportStatus.SUBMITTED
    assert report.job_id == job.id
    assert report.legacy_report_id is None


@pytest.mark.postgres
def test_the_report_status_and_job_must_agree(migrated_database) -> None:
    reports = _report_store(migrated_database)

    with pytest.raises(IntegrityError):
        reports.create_report(
            NewReport(kind=ReportKind.ECONOMY, country="us", job_id=uuid4())
        )
    with pytest.raises(ValueError, match="country"):
        NewReport(kind=ReportKind.ECONOMY, country="USA")


# --- Report results ------------------------------------------------------------


@pytest.mark.postgres
def test_a_report_result_is_written_once_with_its_hash_summary_and_artifacts(
    migrated_database,
) -> None:
    jobs = runtime_store(migrated_database)
    reports = _report_store(migrated_database)
    job = running_job(jobs)
    aggregate = economy_aggregate()

    assert jobs.complete_report(
        job.id,
        report_kind=ReportKind.ECONOMY,
        aggregate=aggregate,
        aggregate_schema_version=1,
        artifacts=(ARTIFACT,),
    )
    assert not jobs.complete_report(
        job.id,
        report_kind=ReportKind.ECONOMY,
        aggregate=aggregate,
        aggregate_schema_version=1,
    )

    stored = reports.get_result(job.id)
    assert jobs.get_report_result(job.id) == stored
    assert jobs.get(job.id).status is JobStatus.SUCCEEDED
    assert jobs.get_result(job.id) is None
    assert stored.report_kind is ReportKind.ECONOMY
    assert stored.aggregate_schema_version == 1
    assert stored.aggregate_sha256 == result_sha256(stored.aggregate)
    assert stored.budgetary_impact == 70.0
    assert stored.model_version == "1.715.2"
    assert stored.artifacts == (ARTIFACT,)
    assert validate_report_aggregate(
        ReportKind.ECONOMY, 1, stored.aggregate
    ) == validate_report_aggregate(ReportKind.ECONOMY, 1, aggregate)


@pytest.mark.postgres
def test_an_invalid_aggregate_writes_nothing(migrated_database) -> None:
    jobs = runtime_store(migrated_database)
    reports = _report_store(migrated_database)
    job = running_job(jobs)

    with pytest.raises(ValidationError):
        jobs.complete_report(
            job.id,
            report_kind=ReportKind.ECONOMY,
            aggregate=economy_aggregate(budget=None),
            aggregate_schema_version=1,
        )

    assert jobs.get(job.id).status is JobStatus.RUNNING
    assert reports.get_result(job.id) is None
