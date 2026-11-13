"""The simulation service's reads and writes for economy simulations and reports.

These run against a database the Public API's own v2 migration chain built
(``PUBLIC_API_SCHEMA_DATABASE_URL``), as the simulation service's role, each
in a transaction rolled back afterwards. The Public API's rows a request names
are inserted first, as the superuser.
"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

import pytest
from conftest import act_as_the_service, rolled_back_transaction
from economy_rows import (
    CURRENT_LAW,
    VERSIONS,
    EconomyCatalog,
    insert_economy_catalog,
    insert_reusable_run,
)
from policyengine_simulation_persistence import (
    EconomyRecordReferenceError,
    NewEconomyReport,
    NewEconomySimulation,
    ReportRunStatus,
    ReportSimulation,
    SimulationChoice,
    SimulationStatus,
    fail_unstarted_report_run,
    fail_unstarted_simulation_run,
    insert_economy_report,
    insert_economy_simulation,
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
    insert_policy,
    insert_region,
)
from sqlalchemy import select, text, update

if TYPE_CHECKING:
    from public_api_rows import CatalogVersion
    from sqlalchemy import Connection

pytestmark = pytest.mark.public_api_schema

REPORT_OPTIONS = {"include_cliffs": True, "spm": {"geography_kind": "national"}}


def _new_side(choice: SimulationChoice = CURRENT_LAW) -> ReportSimulation:
    return ReportSimulation(
        simulation_id=uuid4(),
        simulation_run_id=uuid4(),
        choice=choice,
        reused=False,
    )


def _new_report(
    economy: EconomyCatalog,
    *,
    baseline: ReportSimulation | None = None,
    reform: ReportSimulation | None = None,
) -> NewEconomyReport:
    return NewEconomyReport(
        id=uuid4(),
        run_id=uuid4(),
        country=economy.catalog.country,
        label="Child tax credit",
        description="A larger credit",
        tax_benefit_model_id=economy.catalog.model_id,
        population=economy.population(),
        versions=VERSIONS,
        inputs=REPORT_OPTIONS,
        baseline=baseline or _new_side(),
        reform=reform or _new_side(),
    )


def _row(connection: Connection, table_name: str, row_id: UUID) -> dict:
    row = connection.execute(
        text(f"SELECT * FROM public.{table_name} WHERE id = :id"),  # noqa: S608  # fixed table names
        {"id": row_id},
    ).mappings()
    return dict(row.one())


def _count(connection: Connection, table_name: str) -> int:
    return connection.execute(
        text(f"SELECT count(*) FROM public.{table_name}")  # noqa: S608  # fixed table names
    ).scalar_one()


def test_the_countrys_model_is_found_by_its_name(public_api_database_url) -> None:
    with rolled_back_transaction(public_api_database_url) as connection:
        economy = insert_economy_catalog(connection)
        act_as_the_service(connection)

        assert read_model_id(connection, country="us") == economy.catalog.model_id
        assert read_model_id(connection, country="uk") is None


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

        region = read_region(
            connection,
            catalog_version_id=economy.catalog.version_id,
            region_code="state/ca",
        )
        assert (
            read_region(
                connection,
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

        by_name = read_dataset(
            connection, catalog_version_id=version_id, name="acs_local_2024"
        )
        by_id = read_dataset_by_id(
            connection, catalog_version_id=version_id, dataset_id=economy.dataset_id
        )
        other_version = other.catalog.version_id
        assert (
            read_dataset(
                connection, catalog_version_id=other_version, name="acs_local_2024"
            )
            is None
        )
        assert (
            read_dataset_by_id(
                connection, catalog_version_id=other_version, dataset_id=local_id
            )
            is None
        )

    assert by_name is not None
    assert (by_name.id, by_name.name) == (local_id, "acs_local_2024")
    assert by_id is not None
    assert (by_id.id, by_id.name) == (economy.dataset_id, "populace_us_2024")


def _insert_area_regions(
    connection: Connection,
    catalog: CatalogVersion,
    *,
    constituency_dataset_id: UUID,
    local_authority_dataset_id: UUID,
) -> None:
    """Insert two constituencies and a local authority, each using its dataset."""
    for code in ("constituency/Aldershot", "constituency/Cardiff West"):
        insert_region(
            connection,
            catalog,
            code,
            default_dataset_id=constituency_dataset_id,
            region_type="constituency",
        )
    insert_region(
        connection,
        catalog,
        "local_authority/Cardiff",
        default_dataset_id=local_authority_dataset_id,
        region_type="local_authority",
    )


def test_the_local_area_dataset_is_the_one_both_area_types_use(
    public_api_database_url,
) -> None:
    with rolled_back_transaction(public_api_database_url) as connection:
        economy = insert_economy_catalog(connection, country="uk")
        local_area_id = insert_dataset(
            connection, economy.catalog, "microcosm_uk_2024_25_dense"
        )
        _insert_area_regions(
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
        _insert_area_regions(
            connection,
            older,
            constituency_dataset_id=older_dataset_id,
            local_authority_dataset_id=older_dataset_id,
        )
        act_as_the_service(connection)

        found = read_local_area_dataset(
            connection, catalog_version_id=economy.catalog.version_id
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
            _insert_area_regions(
                connection,
                economy.catalog,
                constituency_dataset_id=constituency_dataset_id,
                local_authority_dataset_id=local_authority_dataset_id,
            )
        act_as_the_service(connection)

        found = read_local_area_dataset(
            connection, catalog_version_id=economy.catalog.version_id
        )

    assert found is None


def test_the_local_area_dataset_may_be_the_national_default(
    public_api_database_url,
) -> None:
    """Stage 12 leaves the areas out of a national run whenever both use one file."""
    with rolled_back_transaction(public_api_database_url) as connection:
        economy = insert_economy_catalog(connection, country="uk")
        _insert_area_regions(
            connection,
            economy.catalog,
            constituency_dataset_id=economy.dataset_id,
            local_authority_dataset_id=economy.dataset_id,
        )
        act_as_the_service(connection)

        found = read_local_area_dataset(
            connection, catalog_version_id=economy.catalog.version_id
        )

    assert found is not None
    assert (found.id, found.name) == (economy.dataset_id, "populace_uk_2024")


def test_a_report_its_simulations_and_runs_are_created_pending(
    public_api_database_url,
) -> None:
    with rolled_back_transaction(public_api_database_url) as connection:
        economy = insert_economy_catalog(connection)
        policy_id = insert_policy(connection, economy.catalog)
        report = _new_report(
            economy, reform=_new_side(SimulationChoice(policy_id, dynamic_id=None))
        )
        act_as_the_service(connection)

        insert_economy_report(connection, report)
        stored_report = _row(connection, "reports", report.id)
        stored_run = _row(connection, "report_runs", report.run_id)
        baseline = _row(connection, "simulations", report.baseline.simulation_id)
        reform = _row(connection, "simulations", report.reform.simulation_id)
        baseline_run = _row(
            connection, "simulation_runs", report.baseline.simulation_run_id
        )
        reform_run = _row(
            connection, "simulation_runs", report.reform.simulation_run_id
        )

    population = economy.population()
    assert {
        key: stored_report[key]
        for key in (
            "label",
            "description",
            "country",
            "type",
            "user_id",
            "tax_benefit_model_id",
            "policy_id",
            "baseline_simulation_id",
            "reform_simulation_id",
            "household_id",
            "dataset_id",
            "region_id",
            "year",
            "inputs",
            "budget_window_size",
        )
    } == {
        "label": "Child tax credit",
        "description": "A larger credit",
        "country": "us",
        "type": "economy",
        "user_id": None,
        "tax_benefit_model_id": economy.catalog.model_id,
        "policy_id": policy_id,
        "baseline_simulation_id": report.baseline.simulation_id,
        "reform_simulation_id": report.reform.simulation_id,
        "household_id": None,
        "dataset_id": economy.dataset_id,
        "region_id": economy.region_id,
        "year": 2026,
        "inputs": REPORT_OPTIONS,
        "budget_window_size": None,
    }
    assert {
        key: stored_run[key]
        for key in (
            "report_id",
            "baseline_simulation_run_id",
            "reform_simulation_run_id",
            "budget_window_run_id",
            "year",
            "country_package_version",
            "policyengine_version",
            "status",
            "trigger",
            "modal_call_id",
            "started_at",
            "completed_at",
        )
    } == {
        "report_id": report.id,
        "baseline_simulation_run_id": report.baseline.simulation_run_id,
        "reform_simulation_run_id": report.reform.simulation_run_id,
        "budget_window_run_id": None,
        "year": None,
        "country_package_version": VERSIONS.country_package_version,
        "policyengine_version": VERSIONS.policyengine_version,
        "status": "pending",
        "trigger": "initial",
        "modal_call_id": None,
        "started_at": None,
        "completed_at": None,
    }
    for simulation, run, expected_policy in (
        (baseline, baseline_run, None),
        (reform, reform_run, policy_id),
    ):
        assert (
            simulation["simulation_type"],
            simulation["policy_id"],
            simulation["dynamic_id"],
            simulation["household_id"],
            simulation["dataset_id"],
            simulation["region_id"],
            simulation["tax_benefit_model_version_id"],
            simulation["year"],
            simulation["filter_field"],
        ) == (
            "economy",
            expected_policy,
            None,
            None,
            population.dataset_id,
            population.region_id,
            population.tax_benefit_model_version_id,
            2026,
            None,
        )
        assert (
            run["simulation_id"],
            run["year"],
            run["tax_benefit_model_version_id"],
            run["country_package_version"],
            run["policyengine_version"],
            run["dataset_id"],
            run["region_id"],
            run["status"],
            run["modal_call_id"],
            run["output_uri"],
        ) == (
            simulation["id"],
            2026,
            population.tax_benefit_model_version_id,
            VERSIONS.country_package_version,
            VERSIONS.policyengine_version,
            population.dataset_id,
            population.region_id,
            "pending",
            None,
            None,
        )


def test_a_side_reusing_a_succeeded_run_creates_nothing_for_that_side(
    public_api_database_url,
) -> None:
    with rolled_back_transaction(public_api_database_url) as connection:
        economy = insert_economy_catalog(connection)
        reused = insert_reusable_run(connection, economy)
        report = _new_report(
            economy,
            baseline=ReportSimulation(
                simulation_id=reused.simulation_id,
                simulation_run_id=reused.simulation_run_id,
                choice=CURRENT_LAW,
                reused=True,
            ),
        )
        simulations_before = _count(connection, "simulations")
        runs_before = _count(connection, "simulation_runs")
        act_as_the_service(connection)

        insert_economy_report(connection, report)
        stored_report = _row(connection, "reports", report.id)
        stored_run = _row(connection, "report_runs", report.run_id)
        simulations_after = _count(connection, "simulations")
        runs_after = _count(connection, "simulation_runs")

    assert stored_report["baseline_simulation_id"] == reused.simulation_id
    assert stored_run["baseline_simulation_run_id"] == reused.simulation_run_id
    assert stored_run["reform_simulation_run_id"] == report.reform.simulation_run_id
    assert (simulations_after, runs_after) == (
        simulations_before + 1,
        runs_before + 1,
    )


@pytest.mark.parametrize(
    ("missing_column", "table_name"),
    [("region_id", "simulations"), ("dataset_id", "simulations")],
)
def test_a_report_naming_a_missing_row_names_the_column_that_refused_it(
    public_api_database_url, missing_column: str, table_name: str
) -> None:
    with rolled_back_transaction(public_api_database_url) as connection:
        economy = insert_economy_catalog(connection)
        report = _new_report(economy)
        population = replace(report.population, **{missing_column: uuid4()})
        act_as_the_service(connection)

        with pytest.raises(EconomyRecordReferenceError) as refusal:
            insert_economy_report(connection, replace(report, population=population))

    assert (refusal.value.table_name, refusal.value.column_name) == (
        table_name,
        missing_column,
    )


def test_a_report_naming_a_missing_dynamic_names_its_column(
    public_api_database_url,
) -> None:
    with rolled_back_transaction(public_api_database_url) as connection:
        economy = insert_economy_catalog(connection)
        report = _new_report(
            economy, reform=_new_side(SimulationChoice(None, dynamic_id=uuid4()))
        )
        act_as_the_service(connection)

        with pytest.raises(EconomyRecordReferenceError) as refusal:
            insert_economy_report(connection, report)

    assert (refusal.value.table_name, refusal.value.column_name) == (
        "simulations",
        "dynamic_id",
    )


def test_a_simulation_and_its_run_are_created_pending(public_api_database_url) -> None:
    with rolled_back_transaction(public_api_database_url) as connection:
        economy = insert_economy_catalog(connection)
        policy_id = insert_policy(connection, economy.catalog)
        simulation = NewEconomySimulation(
            id=uuid4(),
            run_id=uuid4(),
            choice=SimulationChoice(policy_id, dynamic_id=None),
            population=economy.population(year=2027),
            versions=VERSIONS,
        )
        act_as_the_service(connection)

        insert_economy_simulation(connection, simulation)
        stored = _row(connection, "simulations", simulation.id)
        run = _row(connection, "simulation_runs", simulation.run_id)

    assert (
        stored["simulation_type"],
        stored["policy_id"],
        stored["dynamic_id"],
        stored["dataset_id"],
        stored["region_id"],
        stored["year"],
    ) == ("economy", policy_id, None, economy.dataset_id, economy.region_id, 2027)
    assert (
        run["simulation_id"],
        run["year"],
        run["status"],
        run["modal_call_id"],
        run["policyengine_version"],
    ) == (simulation.id, 2027, "pending", None, VERSIONS.policyengine_version)


def _created_report(connection: Connection) -> NewEconomyReport:
    economy = insert_economy_catalog(connection)
    report = _new_report(economy)
    act_as_the_service(connection)
    insert_economy_report(connection, report)
    return report


def test_a_report_runs_call_is_recorded_once_while_it_is_pending_without_one(
    public_api_database_url,
) -> None:
    with rolled_back_transaction(public_api_database_url) as connection:
        report = _created_report(connection)

        first = record_report_run_call(
            connection, report_run_id=report.run_id, modal_call_id="fc-first"
        )
        second = record_report_run_call(
            connection, report_run_id=report.run_id, modal_call_id="fc-second"
        )
        unknown = record_report_run_call(
            connection, report_run_id=uuid4(), modal_call_id="fc-other"
        )
        call_id = _row(connection, "report_runs", report.run_id)["modal_call_id"]

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
            .where(report_runs.c.id == report.run_id)
            .values(status=ReportRunStatus.RUNNING, modal_call_id="fc-coordinator")
        )
        act_as_the_service(connection)

        status = record_report_run_call(
            connection, report_run_id=report.run_id, modal_call_id="fc-late"
        )
        failed = fail_unstarted_report_run(
            connection,
            report_run_id=report.run_id,
            simulation_run_ids=(report.baseline.simulation_run_id,),
            error_message="modal_call_refused: Modal refused the call.",
        )
        stored = _row(connection, "report_runs", report.run_id)
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

        failed = fail_unstarted_report_run(
            connection,
            report_run_id=report.run_id,
            simulation_run_ids=(
                report.baseline.simulation_run_id,
                report.reform.simulation_run_id,
            ),
            error_message=message,
        )
        again = fail_unstarted_report_run(
            connection,
            report_run_id=report.run_id,
            simulation_run_ids=(),
            error_message="a second failure",
        )
        stored = _row(connection, "report_runs", report.run_id)
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
        recorded = NewEconomySimulation(
            id=uuid4(),
            run_id=uuid4(),
            choice=CURRENT_LAW,
            population=economy.population(),
            versions=VERSIONS,
        )
        refused = NewEconomySimulation(
            id=uuid4(),
            run_id=uuid4(),
            choice=CURRENT_LAW,
            population=economy.population(),
            versions=VERSIONS,
        )
        act_as_the_service(connection)
        insert_economy_simulation(connection, recorded)
        insert_economy_simulation(connection, refused)

        status = record_simulation_run_call(
            connection, simulation_run_id=recorded.run_id, modal_call_id="fc-worker"
        )
        not_failed = fail_unstarted_simulation_run(
            connection,
            simulation_run_id=recorded.run_id,
            error_message="modal_call_refused: refused",
        )
        failed = fail_unstarted_simulation_run(
            connection,
            simulation_run_id=refused.run_id,
            error_message="modal_call_refused: refused",
        )
        not_recorded = record_simulation_run_call(
            connection, simulation_run_id=refused.run_id, modal_call_id="fc-late"
        )
        unknown = record_simulation_run_call(
            connection, simulation_run_id=uuid4(), modal_call_id="fc-other"
        )
        recorded_run = _row(connection, "simulation_runs", recorded.run_id)
        refused_run = _row(connection, "simulation_runs", refused.run_id)

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
