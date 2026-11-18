"""Find economy reports, simulations and their runs by key, or create them.

Identical requests, together or apart, end on the same simulation, report
and run rows (C1 · 24), and only the request that inserted a run starts its
Modal call. These run against a database the Public API's own v2 migration
chain built (``PUBLIC_API_SCHEMA_DATABASE_URL``), as the simulation
service's role, most in a transaction rolled back afterwards; the tests of
two transactions commit their rows under a catalog of their own, which
``conftest`` deletes afterwards.
"""

from __future__ import annotations

import threading
from dataclasses import replace
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

import pytest
from committed_rows import (
    LOCK_WAIT_TIMEOUT_SECONDS,
    run_together,
    wait_until_another_backend_waits_for,
)
from conftest import (
    act_as_the_service,
    economy_store_on,
    in_session,
    rolled_back_transaction,
)
from economy_rows import (
    CURRENT_LAW,
    VERSIONS,
    EconomyCatalog,
    insert_economy_catalog,
)
from policyengine_simulation_persistence import (
    KEY_VERSION,
    EconomyRecordReferenceError,
    EconomyReportDefinition,
    EconomySimulationDefinition,
    KeyedEconomyReport,
    KeyedEconomySimulation,
    NewReportRerun,
    ProposedSide,
    ReportRun,
    ReportRunStatus,
    RerunSide,
    SimulationChoice,
    SimulationRun,
    SimulationStatus,
    find_or_create_economy_report,
    find_or_create_economy_simulation,
    insert_report_rerun,
    read_report_definition,
)
from public_api_rows import insert_policy
from sqlalchemy import event, func, insert, select, text, update

if TYPE_CHECKING:
    from sqlalchemy import Connection

pytestmark = pytest.mark.public_api_schema

NATIONAL_SPM = {"geography_kind": "national"}


def _keyed_report(
    economy: EconomyCatalog,
    *,
    reform: SimulationChoice = CURRENT_LAW,
    spm: dict[str, Any] | None = None,
) -> KeyedEconomyReport:
    """Return a request's report on ``economy``, with fresh IDs to propose."""
    definition = EconomyReportDefinition(
        country=economy.catalog.country,
        baseline=CURRENT_LAW,
        reform=reform,
        dataset_name="populace_us_2024",
        region_code="us",
        year=2026,
        budget_window_size=None,
        spm=spm,
    )
    return KeyedEconomyReport(
        definition=definition,
        report_id=uuid4(),
        run_id=uuid4(),
        baseline=ProposedSide(simulation_id=uuid4(), simulation_run_id=uuid4()),
        reform=ProposedSide(simulation_id=uuid4(), simulation_run_id=uuid4()),
        tax_benefit_model_id=economy.catalog.model_id,
        population=economy.population(),
        versions=VERSIONS,
        inputs={} if spm is None else {"spm": spm},
    )


def _keyed_simulation(
    economy: EconomyCatalog, choice: SimulationChoice = CURRENT_LAW
) -> KeyedEconomySimulation:
    return KeyedEconomySimulation(
        definition=EconomySimulationDefinition(
            country=economy.catalog.country,
            choice=choice,
            dataset_name="populace_us_2024",
            region_code="us",
            year=2026,
            spm=None,
        ),
        simulation_id=uuid4(),
        run_id=uuid4(),
        population=economy.population(),
        versions=VERSIONS,
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


def _set_status(
    connection: Connection, table_name: str, row_id: UUID, status: str
) -> None:
    """Set a run's status as the superuser, as a worker or the sweep would."""
    table = (
        ReportRun.__table__ if table_name == "report_runs" else SimulationRun.__table__
    )
    ended = status in {"succeeded", "failed"}
    connection.execute(text("RESET ROLE"))
    connection.execute(
        update(table)
        .where(table.c.id == row_id)
        .values(status=status, completed_at=func.now() if ended else None)
    )
    act_as_the_service(connection)


def test_a_keyed_report_is_created_with_its_simulations_and_first_run(
    public_api_database_url,
) -> None:
    with rolled_back_transaction(public_api_database_url) as connection:
        economy = insert_economy_catalog(connection)
        policy_id = insert_policy(connection, economy.catalog)
        request = _keyed_report(
            economy,
            reform=SimulationChoice(policy_id=policy_id, dynamic_id=None),
            spm=NATIONAL_SPM,
        )
        act_as_the_service(connection)

        found = in_session(connection, find_or_create_economy_report, request)
        report = _row(connection, "reports", found.report_id)
        run = _row(connection, "report_runs", found.run.id)
        baseline = _row(connection, "simulations", found.baseline.simulation_id)
        reform = _row(connection, "simulations", found.reform.simulation_id)
        reform_run = _row(connection, "simulation_runs", found.reform.simulation_run_id)

    definition = request.definition
    assert (found.report_id, found.report_inserted) == (request.report_id, True)
    assert (found.run.id, found.run.inserted) == (request.run_id, True)
    assert found.run.status is ReportRunStatus.PENDING
    assert found.baseline.simulation_id == request.baseline.simulation_id
    assert found.reform.simulation_id == request.reform.simulation_id
    assert found.baseline.run_inserted
    assert found.reform.run_inserted
    assert (found.baseline.simulation_run_id, found.reform.simulation_run_id) == (
        request.baseline.simulation_run_id,
        request.reform.simulation_run_id,
    )
    assert {
        name: report[name]
        for name in (
            "key_version",
            "definition_key",
            "dataset_independent_key",
            "baseline_simulation_id",
            "reform_simulation_id",
            "policy_id",
            "inputs",
            "type",
            "budget_window_size",
            "label",
            "description",
            "user_id",
        )
    } == {
        "key_version": KEY_VERSION,
        "definition_key": definition.definition_key,
        "dataset_independent_key": definition.dataset_independent_key,
        "baseline_simulation_id": request.baseline.simulation_id,
        "reform_simulation_id": request.reform.simulation_id,
        "policy_id": policy_id,
        "inputs": {"spm": NATIONAL_SPM},
        "type": "economy",
        "budget_window_size": None,
        # Answer 6: a shared report holds no request's label or user.
        "label": None,
        "description": None,
        "user_id": None,
    }
    assert (baseline["key_version"], baseline["definition_key"]) == (
        KEY_VERSION,
        definition.simulation("baseline").definition_key,
    )
    assert reform["definition_key"] == definition.simulation("reform").definition_key
    # Answer 6: the trigger is each user's record's, not the shared run's.
    assert (run["status"], run["forced"], run["trigger"]) == (
        ReportRunStatus.PENDING,
        False,
        None,
    )
    assert (
        reform_run["status"],
        reform_run["forced"],
        reform_run["modal_call_id"],
    ) == (
        SimulationStatus.PENDING,
        False,
        None,
    )


def test_an_identical_request_ends_on_the_same_rows_and_inserts_nothing(
    public_api_database_url,
) -> None:
    with rolled_back_transaction(public_api_database_url) as connection:
        economy = insert_economy_catalog(connection)
        act_as_the_service(connection)
        first = in_session(
            connection,
            find_or_create_economy_report,
            _keyed_report(economy, spm=NATIONAL_SPM),
        )
        counts = [
            _count(connection, name)
            for name in ("simulations", "reports", "simulation_runs", "report_runs")
        ]

        again = in_session(
            connection,
            find_or_create_economy_report,
            _keyed_report(economy, spm=NATIONAL_SPM),
        )
        counts_after = [
            _count(connection, name)
            for name in ("simulations", "reports", "simulation_runs", "report_runs")
        ]

    assert (again.report_id, again.run.id) == (first.report_id, first.run.id)
    assert (again.baseline.simulation_run_id, again.reform.simulation_run_id) == (
        first.baseline.simulation_run_id,
        first.reform.simulation_run_id,
    )
    assert (again.report_inserted, again.run.inserted) == (False, False)
    assert not again.baseline.run_inserted
    assert not again.reform.run_inserted
    assert counts_after == counts


def test_the_same_policy_on_both_sides_names_one_simulation_run_twice(
    public_api_database_url,
) -> None:
    """Current law against current law, as the staging check sends."""
    with rolled_back_transaction(public_api_database_url) as connection:
        economy = insert_economy_catalog(connection)
        request = _keyed_report(economy)
        act_as_the_service(connection)

        found = in_session(connection, find_or_create_economy_report, request)
        run = _row(connection, "report_runs", found.run.id)
        simulation_count = _count(connection, "simulations")

    assert found.baseline == found.reform
    assert found.baseline.simulation_run_id == request.baseline.simulation_run_id
    assert found.baseline.run_inserted
    assert run["baseline_simulation_run_id"] == run["reform_simulation_run_id"]
    assert simulation_count == 1


def test_a_report_whose_run_failed_gets_a_new_run_joining_its_simulation_runs(
    public_api_database_url,
) -> None:
    """Answer 2: a failed run never counts; the unfailed simulation runs do."""
    with rolled_back_transaction(public_api_database_url) as connection:
        economy = insert_economy_catalog(connection)
        policy_id = insert_policy(connection, economy.catalog)
        reform = SimulationChoice(policy_id=policy_id, dynamic_id=None)
        act_as_the_service(connection)
        first = in_session(
            connection,
            find_or_create_economy_report,
            _keyed_report(economy, reform=reform),
        )
        _set_status(
            connection, "simulation_runs", first.baseline.simulation_run_id, "succeeded"
        )
        _set_status(
            connection, "simulation_runs", first.reform.simulation_run_id, "failed"
        )
        _set_status(connection, "report_runs", first.run.id, "failed")

        second = in_session(
            connection,
            find_or_create_economy_report,
            _keyed_report(economy, reform=reform),
        )

    assert second.report_id == first.report_id
    assert second.run.inserted is True
    assert second.run.id != first.run.id
    assert second.baseline.simulation_run_id == first.baseline.simulation_run_id
    assert second.baseline.run_inserted is False
    assert second.reform.run_inserted is True
    assert second.reform.simulation_run_id != first.reform.simulation_run_id


def test_a_forced_run_is_never_joined_by_a_create(public_api_database_url) -> None:
    """Answer 3: a forced re-run's runs are an extra run nobody joins."""
    with rolled_back_transaction(public_api_database_url) as connection:
        economy = insert_economy_catalog(connection)
        act_as_the_service(connection)
        first = in_session(
            connection, find_or_create_economy_report, _keyed_report(economy)
        )
        _set_status(
            connection, "simulation_runs", first.baseline.simulation_run_id, "failed"
        )
        _set_status(connection, "report_runs", first.run.id, "failed")
        forced_simulation_run_id = uuid4()
        connection.execute(
            insert(SimulationRun).values(
                id=forced_simulation_run_id,
                simulation_id=first.baseline.simulation_id,
                year=2026,
                tax_benefit_model_version_id=economy.catalog.version_id,
                country_package_version=VERSIONS.country_package_version,
                policyengine_version=VERSIONS.policyengine_version,
                dataset_id=economy.dataset_id,
                region_id=economy.region_id,
                status=SimulationStatus.PENDING,
                forced=True,
            )
        )
        connection.execute(
            insert(ReportRun).values(
                id=uuid4(),
                report_id=first.report_id,
                baseline_simulation_run_id=forced_simulation_run_id,
                reform_simulation_run_id=forced_simulation_run_id,
                country_package_version=VERSIONS.country_package_version,
                policyengine_version=VERSIONS.policyengine_version,
                status=ReportRunStatus.PENDING,
                trigger="manual",
                forced=True,
            )
        )

        second = in_session(
            connection, find_or_create_economy_report, _keyed_report(economy)
        )

    assert second.run.inserted is True
    assert second.baseline.run_inserted is True
    assert forced_simulation_run_id not in {
        second.baseline.simulation_run_id,
        second.reform.simulation_run_id,
    }


def test_another_spm_selection_is_another_report_with_other_simulations(
    public_api_database_url,
) -> None:
    """Answer 4c: the SPM selection changes a simulation's own numbers."""
    with rolled_back_transaction(public_api_database_url) as connection:
        economy = insert_economy_catalog(connection)
        act_as_the_service(connection)

        without = in_session(
            connection, find_or_create_economy_report, _keyed_report(economy)
        )
        national = in_session(
            connection,
            find_or_create_economy_report,
            _keyed_report(economy, spm=NATIONAL_SPM),
        )

    assert national.report_id != without.report_id
    assert national.baseline.simulation_id != without.baseline.simulation_id
    assert national.run.inserted is True


def test_a_report_sharing_a_side_with_another_names_that_sides_run_under_way(
    public_api_database_url,
) -> None:
    """Answer 5: a pending or running simulation run is joined."""
    with rolled_back_transaction(public_api_database_url) as connection:
        economy = insert_economy_catalog(connection)
        first_policy = insert_policy(connection, economy.catalog)
        second_policy = insert_policy(connection, economy.catalog)
        act_as_the_service(connection)
        first = in_session(
            connection,
            find_or_create_economy_report,
            _keyed_report(economy, reform=SimulationChoice(first_policy, None)),
        )
        _set_status(
            connection, "simulation_runs", first.baseline.simulation_run_id, "running"
        )

        second = in_session(
            connection,
            find_or_create_economy_report,
            _keyed_report(economy, reform=SimulationChoice(second_policy, None)),
        )

    assert second.report_id != first.report_id
    assert second.run.inserted is True
    assert second.baseline.simulation_id == first.baseline.simulation_id
    assert second.baseline.simulation_run_id == first.baseline.simulation_run_id
    assert second.baseline.run_inserted is False
    assert second.reform.run_inserted is True


def test_a_keyed_simulation_joins_its_run_or_creates_it(
    public_api_database_url,
) -> None:
    with rolled_back_transaction(public_api_database_url) as connection:
        economy = insert_economy_catalog(connection)
        act_as_the_service(connection)
        report = in_session(
            connection, find_or_create_economy_report, _keyed_report(economy)
        )

        # The report's current-law simulation is this request's simulation.
        joined = in_session(
            connection, find_or_create_economy_simulation, _keyed_simulation(economy)
        )
        _set_status(
            connection, "simulation_runs", report.baseline.simulation_run_id, "failed"
        )
        request = _keyed_simulation(economy)
        created = in_session(connection, find_or_create_economy_simulation, request)
        simulation = _row(connection, "simulations", created.simulation_id)

    assert (joined.simulation_id, joined.run.id) == (
        report.baseline.simulation_id,
        report.baseline.simulation_run_id,
    )
    assert (joined.simulation_inserted, joined.run.inserted) == (False, False)
    assert joined.run.status is SimulationStatus.PENDING
    assert created.simulation_id == report.baseline.simulation_id
    assert (created.run.id, created.run.inserted) == (request.run_id, True)
    assert simulation["definition_key"] == request.definition.definition_key


def test_two_identical_requests_at_once_end_on_the_same_rows_and_start_one_call(
    committed_engine, committed_catalog
) -> None:
    """The first holds its transaction open until the second waits on its rows.

    Each request starts a Modal call only for a report run it inserted, as
    the service does; exactly one does.
    """
    economy = committed_catalog
    first_has_inserted = threading.Event()
    found: dict[str, Any] = {}
    started_calls: list[UUID] = []

    def create_and_start(connection: Connection, name: str) -> None:
        act_as_the_service(connection)
        result = in_session(
            connection, find_or_create_economy_report, _keyed_report(economy)
        )
        found[name] = result
        if result.run.inserted:
            started_calls.append(result.run.id)

    def first_request() -> None:
        with committed_engine.begin() as connection:
            create_and_start(connection, "first")
            holder_pid = connection.execute(select(func.pg_backend_pid())).scalar_one()
            first_has_inserted.set()
            wait_until_another_backend_waits_for(committed_engine, holder_pid)

    def second_request() -> None:
        assert first_has_inserted.wait(LOCK_WAIT_TIMEOUT_SECONDS)
        with committed_engine.begin() as connection:
            create_and_start(connection, "second")

    run_together((first_request, second_request), released=first_has_inserted)

    first, second = found["first"], found["second"]
    assert (second.report_id, second.run.id) == (first.report_id, first.run.id)
    assert second.baseline == replace(first.baseline, run_inserted=False)
    assert started_calls == [first.run.id]


def test_a_request_losing_the_report_run_to_another_joins_it_and_keeps_nothing(
    committed_engine, committed_catalog
) -> None:
    """Another request inserts the report run between this one's lookup and insert."""
    economy = committed_catalog
    with committed_engine.begin() as connection:
        act_as_the_service(connection)
        first = in_session(
            connection, find_or_create_economy_report, _keyed_report(economy)
        )
    with committed_engine.begin() as connection:
        connection.execute(
            update(SimulationRun)
            .where(SimulationRun.id == first.baseline.simulation_run_id)
            .values(status=SimulationStatus.FAILED, completed_at=func.now())
        )
        connection.execute(
            update(ReportRun)
            .where(ReportRun.id == first.run.id)
            .values(status=ReportRunStatus.FAILED, completed_at=func.now())
        )
    competing: list[UUID] = []
    request = _keyed_report(economy)

    def create_a_competing_run_first(
        _connection, _cursor, statement, _parameters, _context, _executemany
    ) -> None:
        if competing or not statement.lstrip().startswith(
            "INSERT INTO public.report_runs"
        ):
            return
        # As the superuser, naming the first run's simulation runs, so it waits
        # on nothing this transaction holds.
        run_id = uuid4()
        with committed_engine.begin() as other:
            other.execute(
                insert(ReportRun).values(
                    id=run_id,
                    report_id=first.report_id,
                    baseline_simulation_run_id=first.baseline.simulation_run_id,
                    reform_simulation_run_id=first.reform.simulation_run_id,
                    country_package_version=VERSIONS.country_package_version,
                    policyengine_version=VERSIONS.policyengine_version,
                    status=ReportRunStatus.PENDING,
                    trigger="initial",
                )
            )
        competing.append(run_id)

    with committed_engine.begin() as connection:
        act_as_the_service(connection)
        event.listen(connection, "before_cursor_execute", create_a_competing_run_first)
        try:
            joined = in_session(connection, find_or_create_economy_report, request)
        finally:
            event.remove(
                connection, "before_cursor_execute", create_a_competing_run_first
            )
        kept = connection.execute(
            select(func.count())
            .select_from(SimulationRun)
            .where(SimulationRun.id == request.baseline.simulation_run_id)
        ).scalar_one()

    assert (joined.run.id, joined.run.inserted) == (competing[0], False)
    assert joined.baseline.run_inserted is False
    assert kept == 0


def test_the_store_finds_or_creates_reports_and_simulations_in_a_transaction(
    public_api_database_url,
) -> None:
    with rolled_back_transaction(public_api_database_url) as connection:
        economy = insert_economy_catalog(connection)
        act_as_the_service(connection)
        store = economy_store_on(connection)

        created = store.find_or_create_report(_keyed_report(economy))
        joined = store.find_or_create_report(_keyed_report(economy))
        simulation = store.find_or_create_simulation(_keyed_simulation(economy))

    assert created.run.inserted is True
    assert (joined.run.id, joined.run.inserted) == (created.run.id, False)
    assert (simulation.simulation_id, simulation.run.id) == (
        created.baseline.simulation_id,
        created.baseline.simulation_run_id,
    )


def test_a_keyed_reports_rerun_joins_each_sides_run_on_its_versions(
    public_api_database_url,
) -> None:
    """Answers 4 and 5: a side joins its simulation's unfailed run, in any state."""
    with rolled_back_transaction(public_api_database_url) as connection:
        economy = insert_economy_catalog(connection)
        policy_id = insert_policy(connection, economy.catalog)
        act_as_the_service(connection)
        created = in_session(
            connection,
            find_or_create_economy_report,
            _keyed_report(economy, reform=SimulationChoice(policy_id, None)),
        )
        _set_status(
            connection,
            "simulation_runs",
            created.baseline.simulation_run_id,
            "succeeded",
        )
        _set_status(connection, "report_runs", created.run.id, "failed")
        definition = in_session(connection, read_report_definition, created.report_id)
        rerun = NewReportRerun(
            report_id=created.report_id,
            run_id=uuid4(),
            population=economy.population(),
            versions=VERSIONS,
            baseline=RerunSide(created.baseline.simulation_id, uuid4()),
            reform=RerunSide(created.reform.simulation_id, uuid4()),
        )

        found = in_session(connection, insert_report_rerun, rerun)

    assert definition is not None
    assert definition.keyed is True
    assert (found.id, found.inserted) == (rerun.run_id, True)
    assert (found.baseline_simulation_run_id, found.reform_simulation_run_id) == (
        created.baseline.simulation_run_id,
        created.reform.simulation_run_id,
    )


def test_a_keyed_rerun_with_one_simulation_on_both_sides_inserts_one_run(
    public_api_database_url,
) -> None:
    """Current law against current law: one new run, named on both sides."""
    with rolled_back_transaction(public_api_database_url) as connection:
        economy = insert_economy_catalog(connection)
        act_as_the_service(connection)
        created = in_session(
            connection, find_or_create_economy_report, _keyed_report(economy)
        )
        _set_status(
            connection, "simulation_runs", created.baseline.simulation_run_id, "failed"
        )
        _set_status(connection, "report_runs", created.run.id, "failed")
        simulation_id = created.baseline.simulation_id
        rerun = NewReportRerun(
            report_id=created.report_id,
            run_id=uuid4(),
            population=economy.population(),
            versions=VERSIONS,
            baseline=RerunSide(simulation_id, uuid4()),
            reform=RerunSide(simulation_id, uuid4()),
        )

        found = in_session(connection, insert_report_rerun, rerun)
        runs = connection.execute(
            select(func.count())
            .select_from(SimulationRun)
            .where(SimulationRun.simulation_id == simulation_id)
        ).scalar_one()

    assert found.inserted is True
    assert (
        found.baseline_simulation_run_id
        == found.reform_simulation_run_id
        == rerun.baseline.simulation_run_id
    )
    assert runs == 2


@pytest.mark.parametrize(
    ("missing_row", "column_name"),
    [
        ("region", "region_id"),
        ("dataset", "dataset_id"),
        ("dynamic", "dynamic_id"),
        ("policy", "policy_id"),
    ],
)
def test_a_report_naming_a_missing_row_names_the_column_that_refused_it(
    public_api_database_url, missing_row: str, column_name: str
) -> None:
    """The service answers each such column with its own refusal code."""
    with rolled_back_transaction(public_api_database_url) as connection:
        economy = insert_economy_catalog(connection)
        report = _keyed_report(
            economy,
            reform={
                "dynamic": SimulationChoice(policy_id=None, dynamic_id=uuid4()),
                "policy": SimulationChoice(policy_id=uuid4(), dynamic_id=None),
            }.get(missing_row, CURRENT_LAW),
        )
        if missing_row in {"region", "dataset"}:
            report = replace(
                report,
                population=replace(report.population, **{column_name: uuid4()}),
            )
        act_as_the_service(connection)

        with pytest.raises(EconomyRecordReferenceError) as refused:
            in_session(connection, find_or_create_economy_report, report)

    assert (refused.value.table_name, refused.value.column_name) == (
        "simulations",
        column_name,
    )
