"""Find budget windows and their window runs by key, or create them (answer 8).

A budget window's report is keyed with its window size; each year has its
own simulations, keyed by that year, and there is one window run per
report and version set that hasn't failed and isn't a recalculation. These
run against a database the Public API's own v2 migration chain built
(``PUBLIC_API_SCHEMA_DATABASE_URL``), as the simulation service's role, in
a transaction rolled back afterwards.
"""

from __future__ import annotations

import threading
from contextlib import contextmanager
from dataclasses import replace
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

import pytest
from committed_rows import (
    LOCK_WAIT_TIMEOUT_SECONDS,
    run_together,
    wait_until_another_backend_waits_for,
)
from conftest import act_as_the_service, rolled_back_transaction
from economy_rows import (
    CURRENT_LAW,
    VERSIONS,
    EconomyCatalog,
    insert_economy_catalog,
)
from policyengine_simulation_persistence import (
    KEY_VERSION,
    EconomyPopulation,
    EconomyReportDefinition,
    EconomyRunStore,
    FoundBudgetWindow,
    KeyedBudgetWindow,
    KeyedEconomyReport,
    KeyedWindowYear,
    ProposedSide,
    ReportRunStatus,
    SimulationChoice,
    find_or_create_budget_window,
    find_or_create_economy_report,
)
from public_api_rows import insert_policy
from sqlalchemy import func, select, text

if TYPE_CHECKING:
    from collections.abc import Iterator

    from sqlalchemy import Connection

pytestmark = pytest.mark.public_api_schema

FIRST_YEAR = 2026
WINDOW_YEARS = (2026, 2027, 2028)


def _definition(
    reform: SimulationChoice,
    *,
    country: str = "us",
    year: int = FIRST_YEAR,
    size: int | None = 3,
) -> EconomyReportDefinition:
    return EconomyReportDefinition(
        country=country,
        baseline=CURRENT_LAW,
        reform=reform,
        dataset_name="populace_us_2024",
        region_code="us",
        year=year,
        budget_window_size=size,
        spm=None,
    )


def _keyed_window(
    economy: EconomyCatalog, reform: SimulationChoice
) -> KeyedBudgetWindow:
    """Return a request's three-year window on ``economy``, with fresh IDs."""
    return KeyedBudgetWindow(
        definition=_definition(reform, country=economy.catalog.country),
        report_id=uuid4(),
        window_run_id=uuid4(),
        tax_benefit_model_id=economy.catalog.model_id,
        population=economy.population(),
        versions=VERSIONS,
        inputs={},
        years=tuple(
            KeyedWindowYear(
                year=year,
                report_run_id=uuid4(),
                baseline=ProposedSide(simulation_id=uuid4(), simulation_run_id=uuid4()),
                reform=ProposedSide(simulation_id=uuid4(), simulation_run_id=uuid4()),
            )
            for year in WINDOW_YEARS
        ),
    )


def _reform(connection: Connection, economy: EconomyCatalog) -> SimulationChoice:
    return SimulationChoice(
        policy_id=insert_policy(connection, economy.catalog), dynamic_id=None
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


def _counts(connection: Connection) -> list[int]:
    return [
        _count(connection, name)
        for name in (
            "simulations",
            "reports",
            "budget_window_runs",
            "simulation_runs",
            "report_runs",
        )
    ]


def _fail_window_run(connection: Connection, window_run_id: UUID) -> None:
    """Fail a window run as the superuser, as its coordinators would."""
    connection.execute(text("RESET ROLE"))
    connection.execute(
        text(
            "UPDATE public.budget_window_runs SET status = 'failed', "
            "completed_at = now() WHERE id = :id"
        ),
        {"id": window_run_id},
    )
    act_as_the_service(connection)


def test_a_keyed_window_is_created_with_each_years_simulations_and_runs(
    public_api_database_url,
) -> None:
    with rolled_back_transaction(public_api_database_url) as connection:
        economy = insert_economy_catalog(connection)
        request = _keyed_window(economy, _reform(connection, economy))
        act_as_the_service(connection)

        found = find_or_create_budget_window(connection, request)
        report = _row(connection, "reports", found.report_id)
        window_run = _row(connection, "budget_window_runs", found.window_run.id)
        year_runs = [
            _row(connection, "report_runs", year.report_run_id) for year in found.years
        ]
        year_simulations = [
            _row(connection, "simulations", year.reform.simulation_id)
            for year in found.years
        ]

    definition = request.definition
    assert (found.report_id, found.report_inserted) == (request.report_id, True)
    assert (found.window_run.id, found.window_run.inserted) == (
        request.window_run_id,
        True,
    )
    assert found.window_run.status is ReportRunStatus.PENDING
    assert {
        name: report[name]
        for name in (
            "budget_window_size",
            "year",
            "key_version",
            "definition_key",
            "dataset_independent_key",
            "label",
            "description",
            "user_id",
        )
    } == {
        "budget_window_size": 3,
        "year": FIRST_YEAR,
        "key_version": KEY_VERSION,
        "definition_key": definition.definition_key,
        "dataset_independent_key": definition.dataset_independent_key,
        "label": None,
        "description": None,
        "user_id": None,
    }
    # The report names its first year's simulations.
    assert report["reform_simulation_id"] == found.years[0].reform.simulation_id
    assert {
        name: window_run[name]
        for name in ("policyengine_version", "recalculate", "trigger", "status")
    } == {
        "policyengine_version": VERSIONS.policyengine_version,
        "recalculate": False,
        "trigger": None,
        "status": ReportRunStatus.PENDING,
    }
    assert [year.year for year in found.years] == list(WINDOW_YEARS)
    for proposed, year, year_run, simulation in zip(
        request.years, found.years, year_runs, year_simulations, strict=True
    ):
        assert year.report_run_id == proposed.report_run_id
        assert year.baseline.run_inserted is True
        assert year.reform.simulation_run_id == proposed.reform.simulation_run_id
        assert (year_run["budget_window_run_id"], year_run["year"]) == (
            request.window_run_id,
            year.year,
        )
        assert (year_run["trigger"], year_run["forced"]) == (None, False)
        assert year_run["reform_simulation_run_id"] == year.reform.simulation_run_id
        # Each year's simulations are that year's, keyed by it.
        assert simulation["year"] == year.year
        assert simulation["definition_key"] == (
            definition.simulation("reform", year=year.year).definition_key
        )


def test_an_identical_window_request_joins_its_window_run_and_inserts_nothing(
    public_api_database_url,
) -> None:
    with rolled_back_transaction(public_api_database_url) as connection:
        economy = insert_economy_catalog(connection)
        reform = _reform(connection, economy)
        act_as_the_service(connection)
        first = find_or_create_budget_window(connection, _keyed_window(economy, reform))
        counts = _counts(connection)

        again = find_or_create_budget_window(connection, _keyed_window(economy, reform))
        counts_again = _counts(connection)

    assert (again.report_id, again.report_inserted) == (first.report_id, False)
    assert (again.window_run.id, again.window_run.inserted) == (
        first.window_run.id,
        False,
    )
    assert again.years == tuple(
        replace(
            year,
            baseline=replace(year.baseline, run_inserted=False),
            reform=replace(year.reform, run_inserted=False),
        )
        for year in first.years
    )
    assert counts_again == counts


def test_a_window_year_names_a_single_year_reports_simulation_run(
    public_api_database_url,
) -> None:
    """Review round 4's bug 3: a window year and a report of that year share runs."""
    with rolled_back_transaction(public_api_database_url) as connection:
        economy = insert_economy_catalog(connection)
        reform = _reform(connection, economy)
        act_as_the_service(connection)
        single_year = find_or_create_economy_report(
            connection,
            KeyedEconomyReport(
                definition=_definition(
                    reform, country=economy.catalog.country, year=2027, size=None
                ),
                report_id=uuid4(),
                run_id=uuid4(),
                baseline=ProposedSide(simulation_id=uuid4(), simulation_run_id=uuid4()),
                reform=ProposedSide(simulation_id=uuid4(), simulation_run_id=uuid4()),
                tax_benefit_model_id=economy.catalog.model_id,
                population=economy.population(year=2027),
                versions=VERSIONS,
                inputs={},
            ),
        )

        window = find_or_create_budget_window(
            connection, _keyed_window(economy, reform)
        )

    year_2027 = window.years[1]
    assert year_2027.year == 2027
    assert (year_2027.reform.simulation_id, year_2027.reform.simulation_run_id) == (
        single_year.reform.simulation_id,
        single_year.reform.simulation_run_id,
    )
    assert year_2027.reform.run_inserted is False
    assert window.years[0].reform.run_inserted is True


def test_a_window_whose_run_failed_gets_a_new_run_on_its_years_simulation_runs(
    public_api_database_url,
) -> None:
    """Answer 2: a failed window run never counts; its years' runs still may."""
    with rolled_back_transaction(public_api_database_url) as connection:
        economy = insert_economy_catalog(connection)
        reform = _reform(connection, economy)
        act_as_the_service(connection)
        first = find_or_create_budget_window(connection, _keyed_window(economy, reform))
        _fail_window_run(connection, first.window_run.id)

        second = find_or_create_budget_window(
            connection, _keyed_window(economy, reform)
        )

    assert second.report_id == first.report_id
    assert second.window_run.inserted is True
    assert second.window_run.id != first.window_run.id
    for first_year, second_year in zip(first.years, second.years, strict=True):
        assert second_year.report_run_id != first_year.report_run_id
        assert second_year.reform.simulation_run_id == (
            first_year.reform.simulation_run_id
        )
        assert second_year.reform.run_inserted is False


def test_a_window_comparing_a_policy_with_itself_names_one_run_a_year_twice(
    public_api_database_url,
) -> None:
    with rolled_back_transaction(public_api_database_url) as connection:
        economy = insert_economy_catalog(connection)
        request = _keyed_window(economy, CURRENT_LAW)
        act_as_the_service(connection)

        found = find_or_create_budget_window(connection, request)

    for proposed, year in zip(request.years, found.years, strict=True):
        assert year.baseline.simulation_id == year.reform.simulation_id
        assert year.baseline.simulation_run_id == year.reform.simulation_run_id
        assert year.baseline.simulation_run_id == proposed.baseline.simulation_run_id


def test_the_store_finds_or_creates_a_window_in_a_transaction(
    public_api_database_url,
) -> None:
    with rolled_back_transaction(public_api_database_url) as connection:
        economy = insert_economy_catalog(connection)
        reform = _reform(connection, economy)
        act_as_the_service(connection)

        @contextmanager
        def savepoint() -> Iterator[Connection]:
            with connection.begin_nested():
                yield connection

        store = EconomyRunStore(savepoint)
        found = store.find_or_create_budget_window(_keyed_window(economy, reform))
        again = store.find_or_create_budget_window(_keyed_window(economy, reform))

    assert (found.window_run.inserted, again.window_run.inserted) == (True, False)
    assert again.window_run.id == found.window_run.id


def test_a_window_request_naming_another_length_than_its_years_is_refused() -> None:
    with pytest.raises(ValueError, match="first year and its length"):
        KeyedBudgetWindow(
            definition=_definition(CURRENT_LAW, size=2),
            report_id=uuid4(),
            window_run_id=uuid4(),
            tax_benefit_model_id=uuid4(),
            population=EconomyPopulation(
                tax_benefit_model_version_id=uuid4(),
                region_id=uuid4(),
                dataset_id=uuid4(),
                year=FIRST_YEAR,
            ),
            versions=VERSIONS,
            inputs={},
            years=tuple(
                KeyedWindowYear(
                    year=year,
                    report_run_id=uuid4(),
                    baseline=ProposedSide(
                        simulation_id=uuid4(), simulation_run_id=uuid4()
                    ),
                    reform=ProposedSide(
                        simulation_id=uuid4(), simulation_run_id=uuid4()
                    ),
                )
                for year in WINDOW_YEARS
            ),
        )


def test_two_identical_window_requests_at_once_end_on_one_window_run_started_once(
    committed_engine, committed_catalog
) -> None:
    """Both find the stored window; the second waits at the window run's index.

    An earlier window run of the report failed, so neither finds a run to
    join; the first inserts one and holds its transaction open until the
    second waits for it, then the second joins it with its year runs. Each
    starts the first years only of a window run it inserted, as the service
    does; exactly one does.
    """
    economy = committed_catalog
    with committed_engine.begin() as connection:
        act_as_the_service(connection)
        earlier = find_or_create_budget_window(
            connection, _keyed_window(economy, CURRENT_LAW)
        )
    with committed_engine.begin() as connection:
        _fail_window_run(connection, earlier.window_run.id)
    first_has_inserted = threading.Event()
    found: dict[str, FoundBudgetWindow] = {}
    started_windows: list[UUID] = []
    waited_on: list[str] = []

    def create_and_start(connection: Connection, name: str) -> None:
        act_as_the_service(connection)
        result = find_or_create_budget_window(
            connection, _keyed_window(economy, CURRENT_LAW)
        )
        found[name] = result
        if result.window_run.inserted:
            started_windows.append(result.window_run.id)

    def first_request() -> None:
        with committed_engine.begin() as connection:
            create_and_start(connection, "first")
            holder_pid = connection.execute(select(func.pg_backend_pid())).scalar_one()
            first_has_inserted.set()
            waited_on.append(
                wait_until_another_backend_waits_for(committed_engine, holder_pid)
            )

    def second_request() -> None:
        assert first_has_inserted.wait(LOCK_WAIT_TIMEOUT_SECONDS)
        with committed_engine.begin() as connection:
            create_and_start(connection, "second")

    run_together((first_request, second_request), released=first_has_inserted)

    # The second waited at the window run's index, not on another row.
    (statement,) = waited_on
    assert statement.lstrip().startswith("INSERT INTO public.budget_window_runs")
    first, second = found["first"], found["second"]
    assert first.report_id == second.report_id == earlier.report_id
    assert first.window_run.id != earlier.window_run.id
    assert (second.window_run.id, second.window_run.inserted) == (
        first.window_run.id,
        False,
    )
    assert [year.report_run_id for year in second.years] == [
        year.report_run_id for year in first.years
    ]
    assert started_windows == [first.window_run.id]
