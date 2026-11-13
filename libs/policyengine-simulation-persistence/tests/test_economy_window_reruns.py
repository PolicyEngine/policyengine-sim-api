"""Re-running a stored budget-window report on a later catalog version.

A budget window's re-run is a new budget-window run of the report, with a
report run per year. Each year's sides are the simulations that year's
report run of the report's newest window run names, which may belong to
another report when that year reused a run. A side may reuse a run of its
own simulation that succeeded on the current versions. These run against a
database the Public API's own v2 migration chain built
(``PUBLIC_API_SCHEMA_DATABASE_URL``), as the simulation service's role, in
a transaction rolled back afterwards.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

import pytest
from conftest import act_as_the_service, rolled_back_transaction
from economy_rows import (
    CURRENT_LAW,
    LATER_POLICYENGINE_VERSION,
    LATER_VERSIONS,
    OPTIONS,
    OUTPUT_BUCKET_PREFIX,
    VERSIONS,
    EconomyCatalog,
    RunShape,
    insert_economy_catalog,
    insert_later_catalog,
    insert_report_run_of,
    insert_run_of,
)
from policyengine_simulation_persistence import (
    BudgetWindowYear,
    EconomyPopulation,
    EconomyRecordReferenceError,
    EconomyRequest,
    EconomyRunStore,
    NewBudgetWindowReport,
    NewReportRerun,
    NewWindowRerun,
    ReportRunStatus,
    ReportRunTrigger,
    ReportSimulation,
    RerunRecordDeletedError,
    RerunSide,
    ReusableRun,
    RunInProgressError,
    RunReuse,
    SimulationStatus,
    WindowRerunYear,
    WindowYearDefinition,
    insert_budget_window_report,
    insert_report_rerun,
    insert_window_rerun,
    read_report_definition,
)
from sqlalchemy import text

if TYPE_CHECKING:
    from collections.abc import Iterator

    from sqlalchemy import Connection

pytestmark = pytest.mark.public_api_schema

WINDOW_YEARS = (2026, 2027, 2028)
LATER_REQUEST = EconomyRequest(
    country="us",
    policyengine_version=LATER_POLICYENGINE_VERSION,
    region_code="us",
    dataset_name="populace_us_2024",
    year=2026,
)
LATER_REUSE = RunReuse(
    versions=LATER_VERSIONS, inputs=OPTIONS, output_uri_prefix=OUTPUT_BUCKET_PREFIX
)


@dataclass(frozen=True)
class StoredWindow:
    """A stored budget-window report, its window run and each year's rows."""

    report_id: UUID
    window_run_id: UUID
    years: tuple[BudgetWindowYear, ...]


def _new_side() -> ReportSimulation:
    return ReportSimulation(
        simulation_id=uuid4(),
        simulation_run_id=uuid4(),
        choice=CURRENT_LAW,
        reused=False,
    )


def _stored_window(
    connection: Connection,
    economy: EconomyCatalog,
    *,
    window_status: ReportRunStatus = ReportRunStatus.SUCCEEDED,
) -> StoredWindow:
    """Insert a budget-window report as the service would, then end its runs."""
    years = tuple(
        BudgetWindowYear(
            year=year,
            report_run_id=uuid4(),
            baseline=_new_side(),
            reform=_new_side(),
        )
        for year in WINDOW_YEARS
    )
    report = NewBudgetWindowReport(
        id=uuid4(),
        window_run_id=uuid4(),
        country="us",
        label="Ten-year cost",
        description=None,
        tax_benefit_model_id=economy.catalog.model_id,
        population=economy.population(year=WINDOW_YEARS[0]),
        versions=VERSIONS,
        inputs=OPTIONS,
        years=years,
    )
    insert_budget_window_report(connection, report)
    # Earlier than anything else the test's transaction creates, whose rows
    # all get the transaction's time.
    for table, condition in (
        ("budget_window_runs", "id = :window_run_id"),
        ("report_runs", "budget_window_run_id = :window_run_id"),
    ):
        connection.execute(
            text(
                f"UPDATE public.{table} "  # noqa: S608  # fixed table names
                f"SET created_at = created_at - interval '1 hour' WHERE {condition}"
            ),
            {"window_run_id": report.window_run_id},
        )
    if window_status is not ReportRunStatus.PENDING:
        ended = window_status in {ReportRunStatus.SUCCEEDED, ReportRunStatus.FAILED}
        for table, condition in (
            ("budget_window_runs", "id = :window_run_id"),
            ("report_runs", "budget_window_run_id = :window_run_id"),
            (
                "simulation_runs",
                "id IN (SELECT baseline_simulation_run_id FROM public.report_runs "
                "WHERE budget_window_run_id = :window_run_id UNION "
                "SELECT reform_simulation_run_id FROM public.report_runs "
                "WHERE budget_window_run_id = :window_run_id)",
            ),
        ):
            connection.execute(
                text(
                    f"UPDATE public.{table} SET status = :status, "  # noqa: S608  # fixed table names
                    "completed_at = CASE WHEN :ended THEN now() END "
                    f"WHERE {condition}"
                ),
                {
                    "status": window_status.value,
                    "ended": ended,
                    "window_run_id": report.window_run_id,
                },
            )
    return StoredWindow(
        report_id=report.id, window_run_id=report.window_run_id, years=years
    )


def _store_on(connection: Connection) -> EconomyRunStore:
    """Return a store whose transactions are savepoints of ``connection``."""

    @contextmanager
    def savepoint() -> Iterator[Connection]:
        with connection.begin_nested():
            yield connection

    return EconomyRunStore(savepoint)


def _row(connection: Connection, table_name: str, row_id: UUID) -> dict:
    row = connection.execute(
        text(f"SELECT * FROM public.{table_name} WHERE id = :id"),  # noqa: S608  # fixed table names
        {"id": row_id},
    ).mappings()
    return dict(row.one())


def _window_rerun_of(
    stored: StoredWindow, later: EconomyCatalog, *, recalculate: bool = False
) -> NewWindowRerun:
    return NewWindowRerun(
        report_id=stored.report_id,
        window_run_id=uuid4(),
        population=later.population(year=WINDOW_YEARS[0]),
        versions=LATER_VERSIONS,
        recalculate=recalculate,
        years=tuple(
            WindowRerunYear(
                year=window_year.year,
                report_run_id=uuid4(),
                baseline=RerunSide(
                    simulation_id=window_year.baseline.simulation_id,
                    simulation_run_id=uuid4(),
                    reused=False,
                ),
                reform=RerunSide(
                    simulation_id=window_year.reform.simulation_id,
                    simulation_run_id=uuid4(),
                    reused=False,
                ),
            )
            for window_year in stored.years
        ),
    )


def test_a_budget_windows_definition_names_each_years_simulations(
    public_api_database_url,
) -> None:
    with rolled_back_transaction(public_api_database_url) as connection:
        economy = insert_economy_catalog(connection)
        stored = _stored_window(connection, economy)
        act_as_the_service(connection)

        definition = read_report_definition(connection, stored.report_id)

    assert definition is not None
    assert (definition.kind, definition.year, definition.budget_window_size) == (
        "budget_window",
        2026,
        3,
    )
    assert definition.window_years == tuple(
        WindowYearDefinition(
            year=window_year.year,
            baseline_simulation_id=window_year.baseline.simulation_id,
            reform_simulation_id=window_year.reform.simulation_id,
        )
        for window_year in stored.years
    )


def test_a_definitions_years_come_from_the_newest_window_run(
    public_api_database_url,
) -> None:
    """A year that reused another report's run names that report's simulation."""
    with rolled_back_transaction(public_api_database_url) as connection:
        economy = insert_economy_catalog(connection)
        later = insert_later_catalog(connection, economy)
        stored = _stored_window(connection, economy)
        other_2027 = _stored_window(connection, economy).years[1]
        rerun = _window_rerun_of(stored, later)
        years = list(rerun.years)
        years[1] = replace(
            years[1],
            baseline=RerunSide(
                simulation_id=other_2027.baseline.simulation_id,
                simulation_run_id=other_2027.baseline.simulation_run_id,
                reused=True,
            ),
        )
        act_as_the_service(connection)
        insert_window_rerun(connection, replace(rerun, years=tuple(years)))

        definition = read_report_definition(connection, stored.report_id)

    assert definition is not None
    assert [year.baseline_simulation_id for year in definition.window_years] == [
        stored.years[0].baseline.simulation_id,
        other_2027.baseline.simulation_id,
        stored.years[2].baseline.simulation_id,
    ]


def test_a_window_rerun_creates_a_window_run_and_each_years_runs(
    public_api_database_url,
) -> None:
    with rolled_back_transaction(public_api_database_url) as connection:
        economy = insert_economy_catalog(connection)
        later = insert_later_catalog(connection, economy)
        stored = _stored_window(connection, economy)
        reused = insert_run_of(
            connection,
            stored.years[1].baseline.simulation_id,
            later.population(year=2027),
            RunShape(versions=LATER_VERSIONS),
        )
        rerun = _window_rerun_of(stored, later, recalculate=True)
        years = list(rerun.years)
        years[1] = replace(
            years[1],
            baseline=RerunSide(
                simulation_id=reused.simulation_id,
                simulation_run_id=reused.simulation_run_id,
                reused=True,
            ),
        )
        rerun = replace(rerun, years=tuple(years))
        act_as_the_service(connection)

        insert_window_rerun(connection, rerun)
        window_run = _row(connection, "budget_window_runs", rerun.window_run_id)
        year_runs = [
            _row(connection, "report_runs", year.report_run_id) for year in rerun.years
        ]
        reform_run_2028 = _row(
            connection, "simulation_runs", rerun.years[2].reform.simulation_run_id
        )
        earlier_window = _row(connection, "budget_window_runs", stored.window_run_id)

    assert {
        key: window_run[key]
        for key in (
            "report_id",
            "tax_benefit_model_version_id",
            "trigger",
            "recalculate",
            "status",
        )
    } == {
        "report_id": stored.report_id,
        "tax_benefit_model_version_id": later.catalog.version_id,
        "trigger": ReportRunTrigger.MANUAL,
        "recalculate": True,
        "status": ReportRunStatus.PENDING,
    }
    assert [
        (
            run["year"],
            run["budget_window_run_id"],
            run["trigger"],
            run["status"],
            run["policyengine_version"],
        )
        for run in year_runs
    ] == [
        (
            year,
            rerun.window_run_id,
            ReportRunTrigger.MANUAL,
            ReportRunStatus.PENDING,
            LATER_POLICYENGINE_VERSION,
        )
        for year in WINDOW_YEARS
    ]
    assert year_runs[1]["baseline_simulation_run_id"] == reused.simulation_run_id
    assert (
        reform_run_2028["simulation_id"],
        reform_run_2028["year"],
        reform_run_2028["tax_benefit_model_version_id"],
        reform_run_2028["status"],
    ) == (
        stored.years[2].reform.simulation_id,
        2028,
        later.catalog.version_id,
        SimulationStatus.PENDING,
    )
    assert earlier_window["status"] == ReportRunStatus.SUCCEEDED


@pytest.mark.parametrize("status", [ReportRunStatus.PENDING, ReportRunStatus.RUNNING])
def test_a_window_rerun_is_refused_while_a_window_run_is_under_way(
    public_api_database_url, status: ReportRunStatus
) -> None:
    with rolled_back_transaction(public_api_database_url) as connection:
        economy = insert_economy_catalog(connection)
        later = insert_later_catalog(connection, economy)
        stored = _stored_window(connection, economy, window_status=status)
        act_as_the_service(connection)

        with pytest.raises(RunInProgressError) as window_refused:
            insert_window_rerun(connection, _window_rerun_of(stored, later))
        # A single-year re-run of the report checks the window runs too.
        with pytest.raises(RunInProgressError) as report_refused:
            insert_report_rerun(
                connection,
                NewReportRerun(
                    report_id=stored.report_id,
                    run_id=uuid4(),
                    population=later.population(),
                    versions=LATER_VERSIONS,
                    baseline=RerunSide(
                        simulation_id=stored.years[0].baseline.simulation_id,
                        simulation_run_id=uuid4(),
                        reused=False,
                    ),
                    reform=RerunSide(
                        simulation_id=stored.years[0].reform.simulation_id,
                        simulation_run_id=uuid4(),
                        reused=False,
                    ),
                ),
            )

    assert window_refused.value.run_id == stored.window_run_id
    assert report_refused.value.run_id == stored.window_run_id


def test_a_window_reruns_inputs_name_each_years_own_reusable_runs(
    public_api_database_url,
) -> None:
    with rolled_back_transaction(public_api_database_url) as connection:
        economy = insert_economy_catalog(connection)
        later = insert_later_catalog(connection, economy)
        stored = _stored_window(connection, economy)
        later_shape = RunShape(versions=LATER_VERSIONS)
        reused_2027 = insert_run_of(
            connection,
            stored.years[1].baseline.simulation_id,
            later.population(year=2027),
            later_shape,
        )
        failed_reform_2027 = insert_run_of(
            connection,
            stored.years[1].reform.simulation_id,
            later.population(year=2027),
            replace(later_shape, status=SimulationStatus.FAILED),
        )
        insert_report_run_of(
            connection, stored.report_id, (reused_2027, failed_reform_2027)
        )
        act_as_the_service(connection)
        years = _definition_years(stored)

        inputs = _store_on(connection).read_window_rerun_inputs(
            LATER_REQUEST,
            baseline=CURRENT_LAW,
            reform=CURRENT_LAW,
            years=years,
            reuse=LATER_REUSE,
        )
        forced = _store_on(connection).read_window_rerun_inputs(
            LATER_REQUEST,
            baseline=CURRENT_LAW,
            reform=CURRENT_LAW,
            years=years,
            reuse=None,
        )

    assert inputs.catalog.catalog_version_id == later.catalog.version_id
    assert inputs.reusable_runs == {
        2026: (None, None),
        2027: (
            ReusableRun(
                simulation_id=stored.years[1].baseline.simulation_id,
                simulation_run_id=reused_2027.simulation_run_id,
            ),
            None,
        ),
        2028: (None, None),
    }
    assert forced.reusable_runs == dict.fromkeys(WINDOW_YEARS, (None, None))


def _definition_years(stored: StoredWindow) -> tuple[WindowYearDefinition, ...]:
    """Return the stored window's years as its definition names them."""
    return tuple(
        WindowYearDefinition(
            year=window_year.year,
            baseline_simulation_id=window_year.baseline.simulation_id,
            reform_simulation_id=window_year.reform.simulation_id,
        )
        for window_year in stored.years
    )


def test_a_window_rerun_is_created_through_the_store(public_api_database_url) -> None:
    with rolled_back_transaction(public_api_database_url) as connection:
        economy = insert_economy_catalog(connection)
        later = insert_later_catalog(connection, economy)
        stored = _stored_window(connection, economy)
        rerun = _window_rerun_of(stored, later)
        act_as_the_service(connection)
        store = _store_on(connection)

        store.create_window_rerun(rerun)
        with pytest.raises(RunInProgressError) as refused:
            store.create_window_rerun(_window_rerun_of(stored, later))

        assert _row(connection, "budget_window_runs", rerun.window_run_id)[
            "trigger"
        ] == (ReportRunTrigger.MANUAL)
    assert refused.value.run_id == rerun.window_run_id


def test_a_window_rerun_refuses_years_that_are_not_its_window() -> None:
    """Its years run one at a time from its first year, at most 75 of them."""
    year = WindowRerunYear(
        year=2026,
        report_run_id=uuid4(),
        baseline=RerunSide(
            simulation_id=uuid4(), simulation_run_id=uuid4(), reused=False
        ),
        reform=RerunSide(
            simulation_id=uuid4(), simulation_run_id=uuid4(), reused=False
        ),
    )
    rerun = NewWindowRerun(
        report_id=uuid4(),
        window_run_id=uuid4(),
        population=EconomyPopulation(
            tax_benefit_model_version_id=uuid4(),
            region_id=uuid4(),
            dataset_id=uuid4(),
            year=2026,
        ),
        versions=LATER_VERSIONS,
        recalculate=False,
        years=(year,),
    )

    for years in (
        (),
        (replace(year, year=2027),),
        (year, replace(year, year=2028)),
        tuple(replace(year, year=2026 + offset) for offset in range(76)),
    ):
        with pytest.raises(ValueError, match="consecutive years"):
            replace(rerun, years=years)


def test_a_year_naming_no_reform_is_left_out_of_a_windows_definition(
    public_api_database_url,
) -> None:
    with rolled_back_transaction(public_api_database_url) as connection:
        economy = insert_economy_catalog(connection)
        stored = _stored_window(connection, economy)
        connection.execute(
            text(
                "UPDATE public.report_runs SET reform_simulation_run_id = NULL "
                "WHERE id = :id"
            ),
            {"id": stored.years[1].report_run_id},
        )
        act_as_the_service(connection)

        definition = read_report_definition(connection, stored.report_id)

    assert definition is not None
    assert [year.year for year in definition.window_years] == [2026, 2028]


def test_a_window_rerun_of_a_deleted_report_or_year_simulation_is_refused(
    public_api_database_url,
) -> None:
    with rolled_back_transaction(public_api_database_url) as connection:
        economy = insert_economy_catalog(connection)
        later = insert_later_catalog(connection, economy)
        stored = _stored_window(connection, economy)
        act_as_the_service(connection)
        rerun = _window_rerun_of(stored, later)
        gone_year = replace(
            rerun.years[2],
            reform=RerunSide(
                simulation_id=uuid4(), simulation_run_id=uuid4(), reused=False
            ),
        )

        with pytest.raises(RerunRecordDeletedError), connection.begin_nested():
            insert_window_rerun(connection, replace(rerun, report_id=uuid4()))
        # A report deleted with a year's simulation: the window run is
        # inserted first, so the report's own key refuses it.
        with pytest.raises(RerunRecordDeletedError), connection.begin_nested():
            insert_window_rerun(
                connection,
                replace(rerun, report_id=uuid4(), years=(*rerun.years[:2], gone_year)),
            )
        with (
            pytest.raises(EconomyRecordReferenceError) as refused,
            connection.begin_nested(),
        ):
            insert_window_rerun(
                connection, replace(rerun, years=(*rerun.years[:2], gone_year))
            )

    assert (refused.value.table_name, refused.value.column_name) == (
        "simulation_runs",
        "simulation_id",
    )


def test_a_window_rerun_is_refused_while_a_year_still_runs_after_its_window_ended(
    public_api_database_url,
) -> None:
    with rolled_back_transaction(public_api_database_url) as connection:
        economy = insert_economy_catalog(connection)
        later = insert_later_catalog(connection, economy)
        stored = _stored_window(
            connection, economy, window_status=ReportRunStatus.FAILED
        )
        still_running = stored.years[2].report_run_id
        connection.execute(
            text(
                "UPDATE public.report_runs SET status = 'running', "
                "started_at = now(), completed_at = NULL WHERE id = :id"
            ),
            {"id": still_running},
        )
        act_as_the_service(connection)

        with pytest.raises(RunInProgressError) as refused:
            insert_window_rerun(connection, _window_rerun_of(stored, later))

    assert refused.value.run_id == still_running


@pytest.mark.parametrize(
    "status",
    [ReportRunStatus.PENDING, ReportRunStatus.RUNNING, ReportRunStatus.SUCCEEDED],
)
def test_a_forced_window_rerun_is_an_extra_run_beside_any_run_on_its_versions(
    public_api_database_url, status: ReportRunStatus
) -> None:
    """Answer 3: every year's report run and simulation runs are marked forced."""
    with rolled_back_transaction(public_api_database_url) as connection:
        economy = insert_economy_catalog(connection)
        stored = _stored_window(connection, economy, window_status=status)
        rerun = replace(
            _window_rerun_of(stored, economy, recalculate=True), versions=VERSIONS
        )
        act_as_the_service(connection)

        insert_window_rerun(connection, rerun)
        window_run = _row(connection, "budget_window_runs", rerun.window_run_id)
        year_runs = [
            _row(connection, "report_runs", year.report_run_id) for year in rerun.years
        ]
        simulation_runs = [
            _row(connection, "simulation_runs", side.simulation_run_id)
            for year in rerun.years
            for side in (year.baseline, year.reform)
        ]

    assert (window_run["recalculate"], window_run["status"]) == (
        True,
        ReportRunStatus.PENDING,
    )
    assert all(run["forced"] for run in year_runs)
    assert len(simulation_runs) == 2 * len(WINDOW_YEARS)
    assert all(run["forced"] for run in simulation_runs)
