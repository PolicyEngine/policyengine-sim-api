"""The simulation service's catalog reads and run writes for economy records.

These run against a database the Public API's own v2 migration chain built
(``PUBLIC_API_SCHEMA_DATABASE_URL``), as the simulation service's role, each
in a transaction rolled back afterwards. The Public API's rows a request
names, and any stored report or simulation, are inserted first, as the
superuser.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID, uuid4

import pytest
from conftest import act_as_the_service, in_session, rolled_back_transaction
from economy_rows import (
    PendingReport,
    RunShape,
    insert_economy_catalog,
    insert_pending_report,
    insert_simulation_run,
    insert_uk_area_regions,
)
from policyengine_simulation_persistence import (
    ReportRunStatus,
    SimulationStatus,
    fail_unstarted_report_run,
    fail_unstarted_simulation_run,
    read_dataset,
    read_dataset_by_id,
    read_local_area_dataset,
    read_model_id,
    read_region,
    record_report_run_call,
    record_simulation_run_call,
    report_runs,
    simulation_runs,
)
from public_api_rows import (
    insert_catalog_version,
    insert_dataset,
    insert_region,
)
from sqlalchemy import select, text, update

if TYPE_CHECKING:
    from sqlalchemy import Connection

pytestmark = pytest.mark.public_api_schema


def _row(connection: Connection, table_name: str, row_id: UUID) -> dict:
    row = connection.execute(
        text(f"SELECT * FROM public.{table_name} WHERE id = :id"),  # noqa: S608  # fixed table names
        {"id": row_id},
    ).mappings()
    return dict(row.one())


def test_the_countrys_model_is_found_by_its_name(public_api_database_url) -> None:
    with rolled_back_transaction(public_api_database_url) as connection:
        economy = insert_economy_catalog(connection)
        act_as_the_service(connection)

        assert (
            in_session(connection, read_model_id, country="us")
            == economy.catalog.model_id
        )
        assert in_session(connection, read_model_id, country="uk") is None


def test_a_region_is_found_by_code_in_its_catalog_version_only(
    public_api_database_url,
) -> None:
    with rolled_back_transaction(public_api_database_url) as connection:
        economy = insert_economy_catalog(connection)
        other = insert_economy_catalog(connection, country="uk")
        state_id = insert_region(
            connection,
            economy.catalog,
            "state/ca",
            default_dataset_id=economy.dataset_id,
            region_type="state",
        )
        act_as_the_service(connection)

        region = in_session(
            connection,
            read_region,
            catalog_version_id=economy.catalog.version_id,
            region_code="state/ca",
        )
        assert (
            in_session(
                connection,
                read_region,
                catalog_version_id=other.catalog.version_id,
                region_code="state/ca",
            )
            is None
        )

    assert region is not None
    assert (region.id, region.code, region.default_dataset_id) == (
        state_id,
        "state/ca",
        economy.dataset_id,
    )


def test_a_dataset_is_found_by_name_or_id_in_its_catalog_version_only(
    public_api_database_url,
) -> None:
    with rolled_back_transaction(public_api_database_url) as connection:
        economy = insert_economy_catalog(connection)
        other = insert_economy_catalog(connection, country="uk")
        local_id = insert_dataset(connection, economy.catalog, "acs_local_2024")
        act_as_the_service(connection)
        version_id = economy.catalog.version_id

        by_name = in_session(
            connection,
            read_dataset,
            catalog_version_id=version_id,
            name="acs_local_2024",
        )
        by_id = in_session(
            connection,
            read_dataset_by_id,
            catalog_version_id=version_id,
            dataset_id=economy.dataset_id,
        )
        other_version = other.catalog.version_id
        assert (
            in_session(
                connection,
                read_dataset,
                catalog_version_id=other_version,
                name="acs_local_2024",
            )
            is None
        )
        assert (
            in_session(
                connection,
                read_dataset_by_id,
                catalog_version_id=other_version,
                dataset_id=local_id,
            )
            is None
        )

    assert by_name is not None
    assert (by_name.id, by_name.name) == (local_id, "acs_local_2024")
    assert by_id is not None
    assert (by_id.id, by_id.name) == (economy.dataset_id, "populace_us_2024")


def test_the_local_area_dataset_is_the_one_both_area_types_use(
    public_api_database_url,
) -> None:
    with rolled_back_transaction(public_api_database_url) as connection:
        economy = insert_economy_catalog(connection, country="uk")
        local_area_id = insert_dataset(
            connection, economy.catalog, "microcosm_uk_2024_25_dense"
        )
        insert_uk_area_regions(
            connection,
            economy.catalog,
            constituency_dataset_id=local_area_id,
            local_authority_dataset_id=local_area_id,
        )
        # Another catalog version of the model routes its areas elsewhere.
        older = insert_catalog_version(
            connection,
            country="uk",
            model_id=economy.catalog.model_id,
            policyengine_version="6.16.0",
        )
        older_dataset_id = insert_dataset(connection, older, "microcosm_uk_2024_25")
        insert_uk_area_regions(
            connection,
            older,
            constituency_dataset_id=older_dataset_id,
            local_authority_dataset_id=older_dataset_id,
        )
        act_as_the_service(connection)

        found = in_session(
            connection,
            read_local_area_dataset,
            catalog_version_id=economy.catalog.version_id,
        )

    assert found is not None
    assert (found.id, found.name) == (local_area_id, "microcosm_uk_2024_25_dense")


@pytest.mark.parametrize(
    "area_regions", ["none", "constituencies only", "a dataset for each type"]
)
def test_a_catalog_version_without_one_dataset_for_both_area_types_has_none(
    public_api_database_url, area_regions
) -> None:
    with rolled_back_transaction(public_api_database_url) as connection:
        economy = insert_economy_catalog(connection, country="uk")
        constituency_dataset_id = insert_dataset(
            connection, economy.catalog, "microcosm_uk_constituencies"
        )
        local_authority_dataset_id = insert_dataset(
            connection, economy.catalog, "microcosm_uk_local_authorities"
        )
        if area_regions == "constituencies only":
            insert_region(
                connection,
                economy.catalog,
                "constituency/Aldershot",
                default_dataset_id=constituency_dataset_id,
                region_type="constituency",
            )
        elif area_regions == "a dataset for each type":
            insert_uk_area_regions(
                connection,
                economy.catalog,
                constituency_dataset_id=constituency_dataset_id,
                local_authority_dataset_id=local_authority_dataset_id,
            )
        act_as_the_service(connection)

        found = in_session(
            connection,
            read_local_area_dataset,
            catalog_version_id=economy.catalog.version_id,
        )

    assert found is None


def test_the_local_area_dataset_may_be_the_national_default(
    public_api_database_url,
) -> None:
    """Stage 12 leaves the areas out of a national run whenever both use one file."""
    with rolled_back_transaction(public_api_database_url) as connection:
        economy = insert_economy_catalog(connection, country="uk")
        insert_uk_area_regions(
            connection,
            economy.catalog,
            constituency_dataset_id=economy.dataset_id,
            local_authority_dataset_id=economy.dataset_id,
        )
        act_as_the_service(connection)

        found = in_session(
            connection,
            read_local_area_dataset,
            catalog_version_id=economy.catalog.version_id,
        )

    assert found is not None
    assert (found.id, found.name) == (economy.dataset_id, "populace_uk_2024")


def _created_report(connection: Connection) -> PendingReport:
    """Insert a pending report and its runs, then act as the service."""
    report = insert_pending_report(connection, insert_economy_catalog(connection))
    act_as_the_service(connection)
    return report


def test_a_report_runs_call_is_recorded_once_while_it_is_pending_without_one(
    public_api_database_url,
) -> None:
    with rolled_back_transaction(public_api_database_url) as connection:
        report = _created_report(connection)

        first = in_session(
            connection,
            record_report_run_call,
            report_run_id=report.report_run_id,
            modal_call_id="fc-first",
        )
        second = in_session(
            connection,
            record_report_run_call,
            report_run_id=report.report_run_id,
            modal_call_id="fc-second",
        )
        unknown = in_session(
            connection,
            record_report_run_call,
            report_run_id=uuid4(),
            modal_call_id="fc-other",
        )
        call_id = _row(connection, "report_runs", report.report_run_id)["modal_call_id"]

    assert (first, second, unknown) == (
        ReportRunStatus.PENDING,
        ReportRunStatus.PENDING,
        None,
    )
    assert call_id == "fc-first"


def test_a_claimed_report_run_keeps_its_claim(public_api_database_url) -> None:
    with rolled_back_transaction(public_api_database_url) as connection:
        report = _created_report(connection)
        connection.execute(text("RESET ROLE"))
        connection.execute(
            update(report_runs)
            .where(report_runs.c.id == report.report_run_id)
            .values(status=ReportRunStatus.RUNNING, modal_call_id="fc-coordinator")
        )
        act_as_the_service(connection)

        status = in_session(
            connection,
            record_report_run_call,
            report_run_id=report.report_run_id,
            modal_call_id="fc-late",
        )
        failed = in_session(
            connection,
            fail_unstarted_report_run,
            report_run_id=report.report_run_id,
            simulation_run_ids=(report.baseline.simulation_run_id,),
            error_message="modal_call_refused: Modal refused the call.",
        )
        stored = _row(connection, "report_runs", report.report_run_id)
        baseline = _row(
            connection, "simulation_runs", report.baseline.simulation_run_id
        )

    assert status is ReportRunStatus.RUNNING
    assert failed is False
    assert (stored["status"], stored["modal_call_id"]) == ("running", "fc-coordinator")
    assert baseline["status"] == "pending"


def test_an_unstarted_report_run_and_its_new_simulation_runs_fail_at_once(
    public_api_database_url,
) -> None:
    message = "modal_call_refused: Modal refused the coordinator's call."
    with rolled_back_transaction(public_api_database_url) as connection:
        report = _created_report(connection)

        failed = in_session(
            connection,
            fail_unstarted_report_run,
            report_run_id=report.report_run_id,
            simulation_run_ids=(
                report.baseline.simulation_run_id,
                report.reform.simulation_run_id,
            ),
            error_message=message,
        )
        again = in_session(
            connection,
            fail_unstarted_report_run,
            report_run_id=report.report_run_id,
            simulation_run_ids=(),
            error_message="a second failure",
        )
        stored = _row(connection, "report_runs", report.report_run_id)
        stored_simulation_runs = connection.execute(
            select(
                simulation_runs.c.status,
                simulation_runs.c.error_message,
                simulation_runs.c.completed_at,
            ).where(
                simulation_runs.c.id.in_(
                    [
                        report.baseline.simulation_run_id,
                        report.reform.simulation_run_id,
                    ]
                )
            )
        ).all()

    assert (failed, again) == (True, False)
    assert (stored["status"], stored["error_message"]) == ("failed", message)
    assert stored["completed_at"] is not None
    assert [
        (row.status, row.error_message, row.completed_at is not None)
        for row in stored_simulation_runs
    ] == [("failed", message, True)] * 2


def test_a_simulation_runs_call_is_recorded_or_the_run_failed_while_unstarted(
    public_api_database_url,
) -> None:
    with rolled_back_transaction(public_api_database_url) as connection:
        economy = insert_economy_catalog(connection)
        pending = RunShape(status=SimulationStatus.PENDING)
        recorded, refused = (
            insert_simulation_run(connection, economy.population(), pending)
            for _ in range(2)
        )
        act_as_the_service(connection)

        status = in_session(
            connection,
            record_simulation_run_call,
            simulation_run_id=recorded.simulation_run_id,
            modal_call_id="fc-worker",
        )
        not_failed = in_session(
            connection,
            fail_unstarted_simulation_run,
            simulation_run_id=recorded.simulation_run_id,
            error_message="modal_call_refused: refused",
        )
        failed = in_session(
            connection,
            fail_unstarted_simulation_run,
            simulation_run_id=refused.simulation_run_id,
            error_message="modal_call_refused: refused",
        )
        not_recorded = in_session(
            connection,
            record_simulation_run_call,
            simulation_run_id=refused.simulation_run_id,
            modal_call_id="fc-late",
        )
        unknown = in_session(
            connection,
            record_simulation_run_call,
            simulation_run_id=uuid4(),
            modal_call_id="fc-other",
        )
        recorded_run = _row(connection, "simulation_runs", recorded.simulation_run_id)
        refused_run = _row(connection, "simulation_runs", refused.simulation_run_id)

    assert (status, not_failed, failed, not_recorded, unknown) == (
        SimulationStatus.PENDING,
        False,
        True,
        SimulationStatus.FAILED,
        None,
    )
    assert (recorded_run["status"], recorded_run["modal_call_id"]) == (
        "pending",
        "fc-worker",
    )
    assert (
        refused_run["status"],
        refused_run["modal_call_id"],
        refused_run["error_message"],
    ) == ("failed", None, "modal_call_refused: refused")
