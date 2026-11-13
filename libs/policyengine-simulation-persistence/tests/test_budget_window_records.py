"""A budget-window report's inputs and rows: one report run per year.

A budget window compares the same baseline and reform in each year of its
window. Each year has its own simulations, with that year, so a year can
reuse a single-year report's run, and the reverse. These run against a
database the Public API's own v2 migration chain built
(``PUBLIC_API_SCHEMA_DATABASE_URL``), acting as the simulation service's
role in a transaction the test rolls back.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from conftest import act_as_the_service, rolled_back_transaction
from economy_rows import (
    CURRENT_LAW,
    OPTIONS,
    OUTPUT_BUCKET_PREFIX,
    POLICYENGINE_VERSION,
    VERSIONS,
    RunShape,
    insert_economy_catalog,
    insert_report_run_naming,
    insert_simulation_run,
)
from policyengine_simulation_persistence import (
    BudgetWindowYear,
    EconomyPopulation,
    EconomyRecordReferenceError,
    EconomyRequest,
    EconomyRunStore,
    NewBudgetWindowReport,
    ReportSimulation,
    ReusableRun,
    RunReuse,
    SimulationChoice,
    SimulationStatus,
)
from public_api_rows import StoredValue, insert_policy
from sqlalchemy import text

if TYPE_CHECKING:
    from collections.abc import Iterator

    from economy_rows import EconomyCatalog
    from sqlalchemy import Connection

pytestmark = pytest.mark.public_api_schema

START = datetime(2026, 1, 1, tzinfo=UTC)
CTC_AMOUNT = "gov.irs.credits.ctc.amount.base[0].amount"
REQUEST = EconomyRequest(
    country="us",
    policyengine_version=POLICYENGINE_VERSION,
    region_code="us",
    dataset_name=None,
    year=2026,
)
REUSE = RunReuse(
    versions=VERSIONS, inputs=OPTIONS, output_uri_prefix=OUTPUT_BUCKET_PREFIX
)
WINDOW_YEARS = (2026, 2027, 2028)


def _store_on(connection: Connection) -> EconomyRunStore:
    """Return a store whose transactions are savepoints of ``connection``."""

    @contextmanager
    def savepoint() -> Iterator[Connection]:
        with connection.begin_nested():
            yield connection

    return EconomyRunStore(savepoint)


def _reusable_run_in(connection: Connection, economy: EconomyCatalog, year: int):
    """Insert a succeeded current-law run of ``year`` that a report run used."""
    population = economy.population(year=year)
    reused = insert_simulation_run(connection, population)
    other_side = insert_simulation_run(
        connection, population, RunShape(status=SimulationStatus.FAILED)
    )
    insert_report_run_naming(connection, economy, (reused, other_side))
    return reused


def test_a_window_reads_each_year_s_reusable_runs(public_api_database_url) -> None:
    with rolled_back_transaction(public_api_database_url) as connection:
        economy = insert_economy_catalog(connection, parameter_names=(CTC_AMOUNT,))
        policy_id = insert_policy(
            connection, economy.catalog, [StoredValue(CTC_AMOUNT, 3_000, START)]
        )
        reused_in_2027 = _reusable_run_in(connection, economy, 2027)
        act_as_the_service(connection)
        reform_choice = SimulationChoice(policy_id=policy_id, dynamic_id=None)

        inputs = _store_on(connection).read_budget_window_inputs(
            REQUEST,
            baseline=CURRENT_LAW,
            reform=reform_choice,
            reuse=REUSE,
            years=WINDOW_YEARS,
        )

    assert inputs.catalog.catalog_version_id == economy.catalog.version_id
    assert (inputs.baseline.choice, inputs.baseline.reform) == (CURRENT_LAW, {})
    assert inputs.reform.choice == reform_choice
    assert inputs.reusable_runs == {
        2026: (None, None),
        2027: (
            ReusableRun(
                simulation_id=reused_in_2027.simulation_id,
                simulation_run_id=reused_in_2027.simulation_run_id,
            ),
            None,
        ),
        2028: (None, None),
    }


def test_a_window_reads_no_reusable_run_without_reuse(public_api_database_url) -> None:
    with rolled_back_transaction(public_api_database_url) as connection:
        economy = insert_economy_catalog(connection)
        _reusable_run_in(connection, economy, 2027)
        act_as_the_service(connection)

        inputs = _store_on(connection).read_budget_window_inputs(
            REQUEST,
            baseline=CURRENT_LAW,
            reform=CURRENT_LAW,
            reuse=None,
            years=WINDOW_YEARS,
        )

    assert set(inputs.reusable_runs.values()) == {(None, None)}


def _new_side(choice: SimulationChoice = CURRENT_LAW) -> ReportSimulation:
    return ReportSimulation(
        simulation_id=uuid4(), simulation_run_id=uuid4(), choice=choice, reused=False
    )


def _window(economy: EconomyCatalog, reused_in_2027) -> NewBudgetWindowReport:
    years = []
    for year in WINDOW_YEARS:
        baseline = (
            ReportSimulation(
                simulation_id=reused_in_2027.simulation_id,
                simulation_run_id=reused_in_2027.simulation_run_id,
                choice=CURRENT_LAW,
                reused=True,
            )
            if year == 2027
            else _new_side()
        )
        years.append(
            BudgetWindowYear(
                year=year,
                report_run_id=uuid4(),
                baseline=baseline,
                reform=_new_side(),
            )
        )
    return NewBudgetWindowReport(
        id=uuid4(),
        window_run_id=uuid4(),
        country="us",
        label="Ten-year budget window",
        description=None,
        tax_benefit_model_id=economy.catalog.model_id,
        population=economy.population(year=2026),
        versions=VERSIONS,
        inputs=OPTIONS,
        years=tuple(years),
    )


def _rows(connection: Connection, table_name: str, **filters):
    rows = connection.execute(
        text(
            f"SELECT * FROM {table_name} WHERE "  # noqa: S608  # test-only names
            + " AND ".join(f"{name} = :{name}" for name in filters)
        ),
        filters,
    )
    return [dict(row._mapping) for row in rows]


def test_a_window_is_created_with_a_report_run_per_year(
    public_api_database_url,
) -> None:
    with rolled_back_transaction(public_api_database_url) as connection:
        economy = insert_economy_catalog(connection)
        reused_in_2027 = _reusable_run_in(connection, economy, 2027)
        window = _window(economy, reused_in_2027)
        act_as_the_service(connection)

        _store_on(connection).create_budget_window_report(window)

        connection.execute(text("RESET ROLE"))
        (report,) = _rows(connection, "reports", id=window.id)
        (window_run,) = _rows(connection, "budget_window_runs", id=window.window_run_id)
        year_runs = {
            row["year"]: row
            for row in _rows(
                connection, "report_runs", budget_window_run_id=window.window_run_id
            )
        }
        simulations = {
            run_id: (
                _rows(connection, "simulation_runs", id=run_id)[0],
                _rows(
                    connection,
                    "simulations",
                    id=_rows(connection, "simulation_runs", id=run_id)[0][
                        "simulation_id"
                    ],
                )[0],
            )
            for year in window.years
            for run_id in (
                year.baseline.simulation_run_id,
                year.reform.simulation_run_id,
            )
        }

    first_year = window.years[0]
    assert (
        report["type"],
        report["year"],
        report["budget_window_size"],
        report["inputs"],
        report["baseline_simulation_id"],
        report["reform_simulation_id"],
        report["household_id"],
    ) == (
        "economy",
        2026,
        3,
        OPTIONS,
        first_year.baseline.simulation_id,
        first_year.reform.simulation_id,
        None,
    )
    assert (
        window_run["report_id"],
        window_run["status"],
        window_run["trigger"],
        window_run["recalculate"],
        window_run["tax_benefit_model_version_id"],
    ) == (window.id, "pending", "initial", False, economy.catalog.version_id)
    for year in window.years:
        year_run = year_runs[year.year]
        assert (
            year_run["id"],
            year_run["report_id"],
            year_run["status"],
            year_run["trigger"],
            year_run["modal_call_id"],
            year_run["baseline_simulation_run_id"],
            year_run["reform_simulation_run_id"],
            year_run["country_package_version"],
            year_run["policyengine_version"],
        ) == (
            year.report_run_id,
            window.id,
            "pending",
            "initial",
            None,
            year.baseline.simulation_run_id,
            year.reform.simulation_run_id,
            VERSIONS.country_package_version,
            VERSIONS.policyengine_version,
        )
        for side in (year.baseline, year.reform):
            simulation_run, simulation = simulations[side.simulation_run_id]
            # Each year has its own simulations, of that year.
            assert (simulation["year"], simulation_run["year"]) == (
                year.year,
                year.year,
            )
            assert simulation_run["status"] == (
                "succeeded" if side.reused else "pending"
            )


def test_a_window_naming_a_missing_policy_is_refused_and_writes_nothing(
    public_api_database_url,
) -> None:
    with rolled_back_transaction(public_api_database_url) as connection:
        economy = insert_economy_catalog(connection)
        reused_in_2027 = _reusable_run_in(connection, economy, 2027)
        window = _window(economy, reused_in_2027)
        missing_policy = SimulationChoice(policy_id=uuid4(), dynamic_id=None)
        last_year = window.years[-1]
        window = replace(
            window,
            years=(
                *window.years[:-1],
                replace(
                    last_year, reform=replace(last_year.reform, choice=missing_policy)
                ),
            ),
        )
        act_as_the_service(connection)

        with pytest.raises(EconomyRecordReferenceError) as refused:
            _store_on(connection).create_budget_window_report(window)

        connection.execute(text("RESET ROLE"))
        written = connection.execute(
            text("SELECT count(*) FROM reports WHERE id = :id"), {"id": window.id}
        ).scalar_one()

    assert (refused.value.table_name, refused.value.column_name) == (
        "simulations",
        "policy_id",
    )
    assert written == 0


@pytest.mark.parametrize(
    "years",
    [(), (2026, 2028), tuple(range(2026, 2102))],
    ids=["no-years", "a-gap", "76-years"],
)
def test_a_window_s_years_must_be_consecutive_from_its_first(years) -> None:
    population = EconomyPopulation(
        tax_benefit_model_version_id=uuid4(),
        region_id=uuid4(),
        dataset_id=uuid4(),
        year=years[0] if years else 2026,
    )
    with pytest.raises(ValueError, match="budget window"):
        NewBudgetWindowReport(
            id=uuid4(),
            window_run_id=uuid4(),
            country="us",
            label="Window",
            description=None,
            tax_benefit_model_id=uuid4(),
            population=population,
            versions=VERSIONS,
            inputs=OPTIONS,
            years=tuple(
                BudgetWindowYear(
                    year=year,
                    report_run_id=uuid4(),
                    baseline=_new_side(),
                    reform=_new_side(),
                )
                for year in years
            ),
        )
