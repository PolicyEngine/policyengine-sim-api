"""Reading an economy report or simulation as it stands, with its newest run.

These run against a database the Public API's own v2 migration chain built
(``PUBLIC_API_SCHEMA_DATABASE_URL``), as the simulation service's role, each
in a transaction rolled back afterwards. The rows are written as the
superuser, to give runs their own creation times.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from conftest import act_as_the_service, rolled_back_transaction
from economy_rows import (
    COUNTRY_PACKAGE_VERSION,
    POLICYENGINE_VERSION,
    EconomyCatalog,
    ReportRunOutcome,
    RunShape,
    insert_economy_catalog,
    insert_report_run_naming,
    insert_simulation_run,
)
from policyengine_simulation_persistence import (
    ReportRunStatus,
    SimulationStatus,
    read_economy_report,
    read_economy_simulation,
    report_runs,
    simulation_runs,
)
from sqlalchemy import update

pytestmark = pytest.mark.public_api_schema

EARLIER = datetime(2026, 11, 1, tzinfo=UTC)


def _report_with_runs(connection, economy: EconomyCatalog) -> tuple[UUID, list[UUID]]:
    """Insert a report and three runs of it; return the report and its runs.

    The first failed; the two after it are forced re-runs' runs, still
    pending, as only forced runs share one report's versions with another
    run that hasn't failed.
    """
    population = economy.population()
    baseline = insert_simulation_run(connection, population)
    reform = insert_simulation_run(
        connection, population, RunShape(status=SimulationStatus.FAILED)
    )
    run_ids = [
        insert_report_run_naming(
            connection,
            economy,
            (baseline, reform),
            ReportRunOutcome(status=ReportRunStatus.FAILED, error_message="first"),
        )
    ]
    report_id = (
        connection.execute(report_runs.select().where(report_runs.c.id == run_ids[0]))
        .one()
        .report_id
    )
    for _ in range(2):
        run_id = uuid4()
        connection.execute(
            report_runs.insert().values(
                id=run_id,
                report_id=report_id,
                baseline_simulation_run_id=baseline.simulation_run_id,
                reform_simulation_run_id=reform.simulation_run_id,
                country_package_version=COUNTRY_PACKAGE_VERSION,
                policyengine_version=POLICYENGINE_VERSION,
                status=ReportRunStatus.PENDING,
                trigger="manual",
                forced=True,
            )
        )
        run_ids.append(run_id)
    return report_id, run_ids


def test_a_report_is_read_with_its_newest_run(public_api_database_url) -> None:
    with rolled_back_transaction(public_api_database_url) as connection:
        economy = insert_economy_catalog(connection)
        report_id, (first, second, third) = _report_with_runs(connection, economy)
        # The first run is the oldest; the other two share a creation time, so
        # the larger ID is the newer.
        connection.execute(
            update(report_runs)
            .where(report_runs.c.id == first)
            .values(created_at=EARLIER)
        )
        shared = EARLIER + timedelta(days=1)
        connection.execute(
            update(report_runs)
            .where(report_runs.c.id.in_([second, third]))
            .values(created_at=shared)
        )
        act_as_the_service(connection)

        report = read_economy_report(connection, report_id)
        missing = read_economy_report(connection, uuid4())

    assert missing is None
    assert report is not None
    assert (report.id, report.country) == (report_id, "us")
    run = report.run
    assert run is not None
    assert run.id == max(second, third)
    assert (run.status, run.error_message) == (ReportRunStatus.PENDING, None)
    assert (run.country_package_version, run.policyengine_version) == (
        COUNTRY_PACKAGE_VERSION,
        POLICYENGINE_VERSION,
    )
    assert run.created_at == shared
    assert (run.started_at, run.completed_at) == (None, None)


def test_a_failed_newest_run_is_read_with_its_error(public_api_database_url) -> None:
    with rolled_back_transaction(public_api_database_url) as connection:
        economy = insert_economy_catalog(connection)
        population = economy.population()
        baseline = insert_simulation_run(connection, population)
        reform = insert_simulation_run(connection, population)
        run_id = insert_report_run_naming(
            connection,
            economy,
            (baseline, reform),
            ReportRunOutcome(
                status=ReportRunStatus.FAILED,
                error_message="simulation_failed: the reform failed",
            ),
        )
        report_id = (
            connection.execute(report_runs.select().where(report_runs.c.id == run_id))
            .one()
            .report_id
        )
        act_as_the_service(connection)

        report = read_economy_report(connection, report_id)

    assert report is not None
    assert (report.baseline_simulation_id, report.reform_simulation_id) == (
        baseline.simulation_id,
        reform.simulation_id,
    )
    assert report.run is not None
    assert (
        report.run.baseline_simulation_run_id,
        report.run.reform_simulation_run_id,
    ) == (
        baseline.simulation_run_id,
        reform.simulation_run_id,
    )
    assert (report.run.status, report.run.error_message) == (
        ReportRunStatus.FAILED,
        "simulation_failed: the reform failed",
    )
    assert report.run.completed_at is not None


def test_a_simulation_is_read_with_its_newest_run(public_api_database_url) -> None:
    with rolled_back_transaction(public_api_database_url) as connection:
        economy = insert_economy_catalog(connection)
        stored = insert_simulation_run(connection, economy.population())
        newer_run_id = uuid4()
        connection.execute(
            simulation_runs.insert().values(
                id=newer_run_id,
                simulation_id=stored.simulation_id,
                year=2026,
                tax_benefit_model_version_id=economy.catalog.version_id,
                country_package_version=COUNTRY_PACKAGE_VERSION,
                policyengine_version="6.18.0",
                dataset_id=economy.dataset_id,
                region_id=economy.region_id,
                status=SimulationStatus.RUNNING,
                started_at=EARLIER + timedelta(days=2),
            )
        )
        connection.execute(
            update(simulation_runs)
            .where(simulation_runs.c.id == stored.simulation_run_id)
            .values(created_at=EARLIER)
        )
        act_as_the_service(connection)

        simulation = read_economy_simulation(connection, stored.simulation_id)
        missing = read_economy_simulation(connection, uuid4())

    assert missing is None
    assert simulation is not None
    assert simulation.id == stored.simulation_id
    assert simulation.run is not None
    assert (simulation.run.id, simulation.run.status) == (
        newer_run_id,
        SimulationStatus.RUNNING,
    )
    assert simulation.run.policyengine_version == "6.18.0"
    assert simulation.run.started_at == EARLIER + timedelta(days=2)
