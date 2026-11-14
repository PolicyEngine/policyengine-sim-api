"""Re-running a stored household report: what the re-run reads, and what it creates.

A household report's re-run gives each side a new run of its own household
simulation, never reusing one, under a new report run with trigger
``manual``. These run against a database the Public API's own v2 migration
chain built (``PUBLIC_API_SCHEMA_DATABASE_URL``). The stored report and its
first runs are written as the superuser; the re-run's reads and writes run
as the simulation service's role, in a transaction the test rolls back.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from conftest import act_as_the_service, rolled_back_transaction
from policyengine_simulation_persistence import (
    EconomyRecordOutcomeUnknownError,
    EconomyRecordReferenceError,
    HouseholdReportSimulation,
    HouseholdReportStore,
    HouseholdRerunSide,
    NewHouseholdReport,
    NewHouseholdReportRerun,
    ReportRunStatus,
    RerunRecordDeletedError,
    RunVersions,
    SideDefinition,
    SimulationChoice,
    SimulationStatus,
    fail_unstarted_report_run,
    insert_household_report,
    read_report_definition,
)
from public_api_rows import (
    insert_catalog_version,
    insert_household,
    insert_model,
    insert_policy,
)
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

if TYPE_CHECKING:
    from collections.abc import Iterator
    from uuid import UUID

    from public_api_rows import CatalogVersion
    from sqlalchemy import Connection

pytestmark = pytest.mark.public_api_schema

VERSIONS = RunVersions(country_package_version="1.764.6", policyengine_version="6.17.1")
# A later release's versions, with a catalog version of its own.
LATER_VERSIONS = RunVersions(
    country_package_version="1.770.0", policyengine_version="6.18.0"
)
CTC_AMOUNT = "gov.irs.credits.ctc.amount.base[0].amount"
DOCUMENT = {
    "people": [
        {
            "id": "you",
            "values": {"age": 40},
            "memberships": {"household": "your household"},
        }
    ],
    "household": [{"id": "your household", "values": {}}],
}
# The year the stored report calculates, which its household's default year
# no longer is.
REPORT_YEAR = 2025
CURRENT_LAW = SimulationChoice(policy_id=None, dynamic_id=None)


def _store_on(connection: Connection) -> HouseholdReportStore:
    """Return a store whose transactions are savepoints of ``connection``."""

    @contextmanager
    def savepoint() -> Iterator[Connection]:
        with connection.begin_nested():
            yield connection

    return HouseholdReportStore(savepoint)


def _catalogs(connection: Connection) -> tuple[CatalogVersion, CatalogVersion]:
    """Insert the US model's catalog version for the report, and a later one."""
    model_id = insert_model(connection, "us")

    def catalog_for(versions: RunVersions) -> CatalogVersion:
        return insert_catalog_version(
            connection,
            country="us",
            model_id=model_id,
            policyengine_version=versions.policyengine_version,
            parameter_names=(CTC_AMOUNT,),
        )

    return catalog_for(VERSIONS), catalog_for(LATER_VERSIONS)


def _stored_report(
    connection: Connection,
    catalog: CatalogVersion,
    *,
    with_reform: bool = True,
    first_run_ended: bool = True,
) -> NewHouseholdReport:
    """Insert a household report and its first runs, as the superuser.

    With ``first_run_ended``, its first run failed before its call started,
    so the report has no run under way.
    """
    household_id = insert_household(
        connection, country="us", default_year=2026, document=DOCUMENT
    )
    report = NewHouseholdReport(
        id=uuid4(),
        run_id=uuid4(),
        country="us",
        label="A larger child tax credit",
        description="For one family",
        tax_benefit_model_id=catalog.model_id,
        tax_benefit_model_version_id=catalog.version_id,
        household_id=household_id,
        year=REPORT_YEAR,
        versions=VERSIONS,
        inputs={},
        baseline=HouseholdReportSimulation(
            simulation_id=uuid4(), simulation_run_id=uuid4(), policy_id=None
        ),
        reform=(
            HouseholdReportSimulation(
                simulation_id=uuid4(),
                simulation_run_id=uuid4(),
                policy_id=insert_policy(connection, catalog),
            )
            if with_reform
            else None
        ),
    )
    insert_household_report(connection, report)
    if first_run_ended:
        fail_unstarted_report_run(
            connection,
            report_run_id=report.run_id,
            simulation_run_ids=report.simulation_run_ids,
            error_message="run_not_started: no call was recorded",
        )
    return report


def _succeed_first_runs(connection: Connection, report: NewHouseholdReport) -> None:
    """Mark the report's first run and its side runs succeeded, as the superuser."""
    for table_name, run_ids in (
        ("report_runs", [report.run_id]),
        ("simulation_runs", list(report.simulation_run_ids)),
    ):
        connection.execute(
            text(
                f"UPDATE public.{table_name} SET status = 'succeeded', "  # noqa: S608  # fixed table names
                "completed_at = now() WHERE id = ANY(:run_ids)"
            ),
            {"run_ids": run_ids},
        )


def _rerun_of(
    report: NewHouseholdReport, catalog: CatalogVersion
) -> NewHouseholdReportRerun:
    """Return the report's re-run on ``catalog``: a new run of each side's own.

    Forced, as the service re-runs a household report without keys
    (interim answer B).
    """
    return NewHouseholdReportRerun(
        report_id=report.id,
        run_id=uuid4(),
        tax_benefit_model_version_id=catalog.version_id,
        year=report.year,
        versions=LATER_VERSIONS,
        baseline=HouseholdRerunSide(
            simulation_id=report.baseline.simulation_id, simulation_run_id=uuid4()
        ),
        reform=(
            None
            if report.reform is None
            else HouseholdRerunSide(
                simulation_id=report.reform.simulation_id, simulation_run_id=uuid4()
            )
        ),
        forced=True,
    )


def _row(connection: Connection, table_name: str, row_id: UUID) -> dict:
    return dict(
        connection.execute(
            text(f"SELECT * FROM public.{table_name} WHERE id = :id"),  # noqa: S608  # fixed table names
            {"id": row_id},
        )
        .mappings()
        .one()
    )


def _count(connection: Connection, table_name: str, column: str, value: UUID) -> int:
    return connection.execute(
        text(f"SELECT count(*) FROM public.{table_name} WHERE {column} = :value"),  # noqa: S608  # fixed table and column names
        {"value": value},
    ).scalar_one()


def test_a_household_report_is_read_with_its_household_and_each_sides_policy(
    public_api_database_url,
) -> None:
    with rolled_back_transaction(public_api_database_url) as connection:
        catalog, _ = _catalogs(connection)
        report = _stored_report(connection, catalog)
        baseline_alone = _stored_report(connection, catalog, with_reform=False)
        act_as_the_service(connection)

        definition = read_report_definition(connection, report.id)
        alone_definition = read_report_definition(connection, baseline_alone.id)

    assert definition is not None
    assert alone_definition is not None
    assert report.reform is not None
    assert (definition.kind, definition.country, definition.year) == (
        "household",
        "us",
        REPORT_YEAR,
    )
    assert definition.household_id == report.household_id
    assert (definition.region_code, definition.dataset_name) == (None, None)
    assert definition.inputs == {}
    assert definition.baseline == SideDefinition(
        simulation_id=report.baseline.simulation_id, choice=CURRENT_LAW
    )
    assert definition.reform == SideDefinition(
        simulation_id=report.reform.simulation_id,
        choice=SimulationChoice(policy_id=report.reform.policy_id, dynamic_id=None),
    )
    assert alone_definition.kind == "household"
    assert alone_definition.household_id == baseline_alone.household_id
    assert alone_definition.reform is None


def test_a_household_reports_rerun_is_a_manual_run_of_its_own_simulations(
    public_api_database_url,
) -> None:
    with rolled_back_transaction(public_api_database_url) as connection:
        catalog, later_catalog = _catalogs(connection)
        report = _stored_report(connection, catalog)
        act_as_the_service(connection)
        rerun = _rerun_of(report, later_catalog)

        _store_on(connection).create_report_rerun(rerun)
        report_run = _row(connection, "report_runs", rerun.run_id)
        side_runs = [
            _row(connection, "simulation_runs", side.simulation_run_id)
            for side in rerun.sides
        ]
        stored_simulations = [
            _row(connection, "simulations", side.simulation_id) for side in rerun.sides
        ]
        first_run = _row(connection, "report_runs", report.run_id)

    assert rerun.simulation_run_ids == tuple(
        side.simulation_run_id for side in rerun.sides
    )
    assert report_run["report_id"] == report.id
    # Answer 6: the trigger is each user's record's, not the shared run's.
    assert report_run["trigger"] is None
    assert report_run["status"] == ReportRunStatus.PENDING
    assert report_run["modal_call_id"] is None
    assert (
        report_run["baseline_simulation_run_id"],
        report_run["reform_simulation_run_id"],
    ) == rerun.simulation_run_ids
    assert (report_run["budget_window_run_id"], report_run["year"]) == (None, None)
    assert (
        report_run["country_package_version"],
        report_run["policyengine_version"],
    ) == (LATER_VERSIONS.country_package_version, LATER_VERSIONS.policyengine_version)
    # The insert left both times to the server's defaults, so they are equal.
    assert report_run["updated_at"] == report_run["created_at"]
    for side, side_run in zip(rerun.sides, side_runs, strict=True):
        assert side_run["simulation_id"] == side.simulation_id
        assert side_run["status"] == SimulationStatus.PENDING
        assert side_run["modal_call_id"] is None
        assert side_run["year"] == REPORT_YEAR
        assert side_run["tax_benefit_model_version_id"] == later_catalog.version_id
        assert (side_run["dataset_id"], side_run["region_id"]) == (None, None)
        assert (
            side_run["country_package_version"],
            side_run["policyengine_version"],
        ) == (
            LATER_VERSIONS.country_package_version,
            LATER_VERSIONS.policyengine_version,
        )
        assert side_run["updated_at"] == side_run["created_at"]
    # The simulations keep the catalog version they were created on, and the
    # report's first run is kept.
    assert [
        simulation["tax_benefit_model_version_id"] for simulation in stored_simulations
    ] == [catalog.version_id, catalog.version_id]
    assert first_run["status"] == ReportRunStatus.FAILED


def test_a_rerun_on_the_versions_of_a_succeeded_run_is_a_forced_extra_run(
    public_api_database_url,
) -> None:
    """Interim answer B: a household report has no keys yet, so nothing joins.

    Its runs are marked forced, so the one-unfailed-run indexes leave them
    out beside the succeeded runs on the same versions.
    """
    with rolled_back_transaction(public_api_database_url) as connection:
        catalog, _ = _catalogs(connection)
        report = _stored_report(connection, catalog, first_run_ended=False)
        _succeed_first_runs(connection, report)
        act_as_the_service(connection)
        rerun = replace(_rerun_of(report, catalog), versions=VERSIONS)

        _store_on(connection).create_report_rerun(rerun)
        report_run = _row(connection, "report_runs", rerun.run_id)
        side_runs = [
            _row(connection, "simulation_runs", run_id)
            for run_id in rerun.simulation_run_ids
        ]
        first_run = _row(connection, "report_runs", report.run_id)

    assert report_run["forced"] is True
    assert [side_run["forced"] for side_run in side_runs] == [True, True]
    assert first_run["forced"] is False
    assert first_run["status"] == ReportRunStatus.SUCCEEDED


def test_a_rerun_of_a_report_of_the_baseline_alone_names_no_reform_run(
    public_api_database_url,
) -> None:
    with rolled_back_transaction(public_api_database_url) as connection:
        catalog, later_catalog = _catalogs(connection)
        report = _stored_report(connection, catalog, with_reform=False)
        act_as_the_service(connection)
        rerun = _rerun_of(report, later_catalog)

        _store_on(connection).create_report_rerun(rerun)
        report_run = _row(connection, "report_runs", rerun.run_id)
        baseline_runs = _count(
            connection,
            "simulation_runs",
            "simulation_id",
            report.baseline.simulation_id,
        )

    assert rerun.simulation_run_ids == (rerun.baseline.simulation_run_id,)
    assert report_run["baseline_simulation_run_id"] == rerun.baseline.simulation_run_id
    assert report_run["reform_simulation_run_id"] is None
    assert baseline_runs == 2


def test_a_forced_household_rerun_beside_a_run_under_way_is_an_extra_run(
    public_api_database_url,
) -> None:
    """Answer 3: a forced run is never refused, so the 409 goes."""
    with rolled_back_transaction(public_api_database_url) as connection:
        catalog, later_catalog = _catalogs(connection)
        report = _stored_report(connection, catalog, first_run_ended=False)
        act_as_the_service(connection)
        rerun = _rerun_of(report, later_catalog)

        created = _store_on(connection).create_report_rerun(rerun)
        report_runs = _count(connection, "report_runs", "report_id", report.id)
        baseline_runs = _count(
            connection,
            "simulation_runs",
            "simulation_id",
            report.baseline.simulation_id,
        )
        first_run = _row(connection, "report_runs", report.run_id)

    assert (created.id, created.inserted, created.status) == (
        rerun.run_id,
        True,
        ReportRunStatus.PENDING,
    )
    assert (
        created.baseline_simulation_run_id,
        created.reform_simulation_run_id,
    ) == rerun.simulation_run_ids
    assert (report_runs, baseline_runs) == (2, 2)
    assert first_run["status"] == ReportRunStatus.PENDING


def test_a_rerun_naming_a_deleted_report_or_simulation_is_refused_by_its_key(
    public_api_database_url,
) -> None:
    with rolled_back_transaction(public_api_database_url) as connection:
        catalog, later_catalog = _catalogs(connection)
        report = _stored_report(connection, catalog)
        act_as_the_service(connection)
        store = _store_on(connection)
        rerun = _rerun_of(report, later_catalog)

        # A report deleted after the re-run read it, its simulations kept.
        with pytest.raises(RerunRecordDeletedError):
            store.create_report_rerun(replace(rerun, report_id=uuid4()))
        # A side's simulation deleted after the re-run read it.
        with pytest.raises(EconomyRecordReferenceError) as refused:
            store.create_report_rerun(
                replace(
                    rerun,
                    reform=HouseholdRerunSide(
                        simulation_id=uuid4(), simulation_run_id=uuid4()
                    ),
                )
            )
        report_runs = _count(connection, "report_runs", "report_id", report.id)

    assert refused.value.column_name == "simulation_id"
    assert report_runs == 1


class _CommitFails:
    """A transaction whose block runs, and whose commit then fails."""

    def __init__(self, connection) -> None:
        self.connection = connection

    def __enter__(self):
        return self.connection

    def __exit__(self, error_type, error, traceback) -> None:
        if error is None:
            statement = "COMMIT"
            raise OperationalError(statement, {}, Exception("connection lost"))


class _RecordingConnection:
    """Records each statement it is given, and answers each with no rows."""

    def __init__(self) -> None:
        self.statements: list[object] = []

    def execute(self, statement, *arguments):
        self.statements.append(statement)
        return _NoRows()


class _NoRows:
    def scalar_one_or_none(self) -> None:
        return None


def test_a_rerun_whose_commit_fails_may_have_been_written() -> None:
    connection = _RecordingConnection()
    store = HouseholdReportStore(lambda: _CommitFails(connection))

    with pytest.raises(EconomyRecordOutcomeUnknownError) as raised:
        store.create_report_rerun(
            NewHouseholdReportRerun(
                report_id=uuid4(),
                run_id=uuid4(),
                tax_benefit_model_version_id=uuid4(),
                year=REPORT_YEAR,
                versions=LATER_VERSIONS,
                baseline=HouseholdRerunSide(
                    simulation_id=uuid4(), simulation_run_id=uuid4()
                ),
                reform=None,
                forced=True,
            )
        )

    assert isinstance(raised.value.__cause__, OperationalError)
    assert connection.statements
