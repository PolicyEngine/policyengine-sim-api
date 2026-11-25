"""Find a simulation run's cliff calculation, or insert it, and start or fail it.

A request asking for cliffs (``include_cliffs``) gets one cliff calculation
per simulation run it names that has none that hasn't failed (C1 · 24's
answer 4b, the ``cliff_impacts`` design). These run against a database
API v1's own v2 migration chain built, as the simulation service's role.
Most run in a transaction rolled back afterwards; the test of two
transactions commits its rows under a catalog of a country no other test
uses, and deletes them afterwards.
"""

from __future__ import annotations

import threading
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
    insert_simulation_run,
)
from policyengine_simulation_persistence import (
    CliffImpact,
    EconomyRecordReferenceError,
    EconomyReportDefinition,
    EconomyRunStore,
    FoundReportRun,
    KeyedEconomyReport,
    NewReportRerun,
    ProposedCliffs,
    ProposedSide,
    Report,
    ReportRun,
    ReportRunStatus,
    RerunSide,
    SimulationChoice,
    SimulationRun,
    SimulationStatus,
    find_or_create_economy_report,
    find_or_insert_cliff_calculations,
    find_or_insert_report_run_cliffs,
)
from public_api_rows import insert_policy
from sqlalchemy import event, func, insert, select, text, update
from sqlalchemy.exc import IntegrityError

if TYPE_CHECKING:
    from policyengine_simulation_persistence import FoundEconomyReport
    from sqlalchemy import Connection

pytestmark = pytest.mark.public_api_schema


def _keyed_report(
    economy: EconomyCatalog,
    *,
    reform: SimulationChoice = CURRENT_LAW,
    cliffs: ProposedCliffs | None = None,
) -> KeyedEconomyReport:
    """Return a request's report on ``economy``, with fresh IDs to propose."""
    return KeyedEconomyReport(
        definition=EconomyReportDefinition(
            country=economy.catalog.country,
            baseline=CURRENT_LAW,
            reform=reform,
            dataset_name="populace_us_2024",
            region_code="us",
            year=2026,
            budget_window_size=None,
            spm=None,
        ),
        report_id=uuid4(),
        run_id=uuid4(),
        baseline=ProposedSide(simulation_id=uuid4(), simulation_run_id=uuid4()),
        reform=ProposedSide(simulation_id=uuid4(), simulation_run_id=uuid4()),
        tax_benefit_model_id=economy.catalog.model_id,
        population=economy.population(),
        versions=VERSIONS,
        inputs={},
        cliffs=cliffs,
    )


def _new_cliffs() -> ProposedCliffs:
    return ProposedCliffs(baseline_id=uuid4(), reform_id=uuid4())


def _calculations_of(connection: Connection, *simulation_run_ids: UUID) -> int:
    return connection.execute(
        select(func.count()).where(
            CliffImpact.simulation_run_id.in_(simulation_run_ids)
        )
    ).scalar_one()


def _seed_simulation_run(connection: Connection) -> UUID:
    """Insert a catalog, a simulation and its pending run; return the run's ID."""
    economy = insert_economy_catalog(connection)
    return insert_simulation_run(connection, economy.population()).simulation_run_id


def _finish_as_the_worker(
    connection: Connection, cliff_result_id: UUID, values: dict[str, Any]
) -> None:
    """End a cliff calculation as the worker or the sweep would, as the superuser."""
    connection.execute(text("RESET ROLE"))
    connection.execute(
        update(CliffImpact)
        .where(CliffImpact.id == cliff_result_id)
        .values(completed_at=text("now()"), **values)
    )
    act_as_the_service(connection)


def _cliff_row(connection: Connection, cliff_result_id: UUID) -> Any:
    return connection.execute(
        select(
            CliffImpact.status,
            CliffImpact.modal_call_id,
            CliffImpact.error_message,
            CliffImpact.completed_at,
        ).where(CliffImpact.id == cliff_result_id)
    ).one()


def test_a_runs_cliff_calculation_is_inserted_pending_once_and_then_joined(
    public_api_database_url,
) -> None:
    with rolled_back_transaction(public_api_database_url) as connection:
        run_id = _seed_simulation_run(connection)
        act_as_the_service(connection)
        proposed_id = uuid4()

        (first,) = in_session(
            connection, find_or_insert_cliff_calculations, {run_id: proposed_id}
        ).values()
        (again,) = in_session(
            connection, find_or_insert_cliff_calculations, {run_id: uuid4()}
        ).values()

        assert (first.id, first.inserted, first.status) == (
            proposed_id,
            True,
            SimulationStatus.PENDING,
        )
        assert first.simulation_run_id == again.simulation_run_id == run_id
        assert (again.id, again.inserted, again.status) == (
            proposed_id,
            False,
            SimulationStatus.PENDING,
        )
        assert _cliff_row(connection, proposed_id).modal_call_id is None


def test_each_run_named_gets_its_own_cliff_calculation(
    public_api_database_url,
) -> None:
    with rolled_back_transaction(public_api_database_url) as connection:
        economy = insert_economy_catalog(connection)
        baseline_run_id = insert_simulation_run(
            connection, economy.population()
        ).simulation_run_id
        reform_run_id = insert_simulation_run(
            connection, economy.population()
        ).simulation_run_id
        act_as_the_service(connection)
        proposed = {baseline_run_id: uuid4(), reform_run_id: uuid4()}

        found = in_session(connection, find_or_insert_cliff_calculations, proposed)

        assert {run_id: (each.id, each.inserted) for run_id, each in found.items()} == {
            run_id: (proposed_id, True) for run_id, proposed_id in proposed.items()
        }


def test_calculations_are_inserted_in_simulation_run_id_order(
    public_api_database_url,
) -> None:
    """So two transactions never wait for each other in opposite orders."""
    with rolled_back_transaction(public_api_database_url) as connection:
        economy = insert_economy_catalog(connection)
        run_ids = sorted(
            insert_simulation_run(connection, economy.population()).simulation_run_id
            for _ in range(3)
        )
        act_as_the_service(connection)
        inserted: list[UUID] = []

        def record_the_insert(
            _connection, _cursor, statement, parameters, _context, _executemany
        ) -> None:
            if statement.lstrip().startswith("INSERT INTO public.cliff_impacts"):
                inserted.append(parameters["simulation_run_id"])

        event.listen(connection, "before_cursor_execute", record_the_insert)
        try:
            in_session(
                connection,
                find_or_insert_cliff_calculations,
                {run_id: uuid4() for run_id in reversed(run_ids)},
            )
        finally:
            event.remove(connection, "before_cursor_execute", record_the_insert)

        assert inserted == run_ids


def test_a_calculation_naming_a_missing_run_names_the_refusing_column(
    public_api_database_url,
) -> None:
    with rolled_back_transaction(public_api_database_url) as connection:
        act_as_the_service(connection)
        missing_runs = FoundReportRun(
            id=uuid4(),
            inserted=False,
            status=ReportRunStatus.PENDING,
            baseline_simulation_run_id=uuid4(),
            reform_simulation_run_id=uuid4(),
        )

        with pytest.raises(EconomyRecordReferenceError) as refused:
            in_session(
                connection,
                find_or_insert_report_run_cliffs,
                missing_runs,
                _new_cliffs(),
            )

    assert (refused.value.table_name, refused.value.column_name) == (
        "cliff_impacts",
        "simulation_run_id",
    )


def test_a_failed_cliff_calculation_is_never_joined(public_api_database_url) -> None:
    with rolled_back_transaction(public_api_database_url) as connection:
        run_id = _seed_simulation_run(connection)
        act_as_the_service(connection)
        failed_id = uuid4()
        in_session(connection, find_or_insert_cliff_calculations, {run_id: failed_id})
        _finish_as_the_worker(
            connection,
            failed_id,
            {"status": "failed", "error_message": "cliff_calculation_failed: x"},
        )

        (after_failure,) = in_session(
            connection, find_or_insert_cliff_calculations, {run_id: uuid4()}
        ).values()

        assert after_failure.inserted is True
        assert after_failure.id != failed_id
        assert after_failure.status is SimulationStatus.PENDING


def test_a_succeeded_cliff_calculation_is_the_runs_result_and_is_joined(
    public_api_database_url,
) -> None:
    with rolled_back_transaction(public_api_database_url) as connection:
        run_id = _seed_simulation_run(connection)
        act_as_the_service(connection)
        result_id = uuid4()
        in_session(connection, find_or_insert_cliff_calculations, {run_id: result_id})
        _finish_as_the_worker(
            connection,
            result_id,
            {"status": "succeeded", "cliff_gap": 1.5e9, "cliff_share": 0.04},
        )

        (found,) = in_session(
            connection, find_or_insert_cliff_calculations, {run_id: uuid4()}
        ).values()

        assert (found.id, found.inserted, found.status) == (
            result_id,
            False,
            SimulationStatus.SUCCEEDED,
        )


def test_two_requests_at_once_insert_one_cliff_calculation(
    committed_engine, committed_catalog
) -> None:
    with committed_engine.begin() as connection:
        run_id = insert_simulation_run(
            connection, committed_catalog.population()
        ).simulation_run_id
    first_has_inserted = threading.Event()
    found: dict[str, Any] = {}

    def insert_then_commit_once_the_other_waits() -> None:
        with committed_engine.begin() as connection:
            act_as_the_service(connection)
            found["first"] = in_session(
                connection, find_or_insert_cliff_calculations, {run_id: uuid4()}
            )[run_id]
            holder_pid = connection.execute(
                text("SELECT pg_backend_pid()")
            ).scalar_one()
            first_has_inserted.set()
            wait_until_another_backend_waits_for(committed_engine, holder_pid)

    def insert_once_the_first_has() -> None:
        assert first_has_inserted.wait(LOCK_WAIT_TIMEOUT_SECONDS)
        with committed_engine.begin() as connection:
            act_as_the_service(connection)
            found["second"] = in_session(
                connection, find_or_insert_cliff_calculations, {run_id: uuid4()}
            )[run_id]

    run_together(
        (insert_then_commit_once_the_other_waits, insert_once_the_first_has),
        released=first_has_inserted,
    )

    assert found["first"].inserted is True
    assert (found["second"].id, found["second"].inserted) == (found["first"].id, False)
    with committed_engine.begin() as connection:
        rows = connection.execute(
            select(CliffImpact.id).where(CliffImpact.simulation_run_id == run_id)
        ).all()
    assert [row.id for row in rows] == [found["first"].id]


def test_a_report_created_asking_for_cliffs_inserts_its_runs_calculations_with_it(
    public_api_database_url,
) -> None:
    """In the report's own transaction, so a failure writes neither (round 1)."""
    with rolled_back_transaction(public_api_database_url) as connection:
        economy = insert_economy_catalog(connection)
        policy_id = insert_policy(connection, economy.catalog)
        act_as_the_service(connection)
        store = economy_store_on(connection)
        reform = SimulationChoice(policy_id, None)
        proposed = _new_cliffs()

        created = store.find_or_create_report(
            _keyed_report(economy, reform=reform, cliffs=proposed)
        )
        joined = store.find_or_create_report(
            _keyed_report(economy, reform=reform, cliffs=_new_cliffs())
        )

    assert created.cliffs is not None
    assert (created.cliffs.baseline.id, created.cliffs.baseline.inserted) == (
        proposed.baseline_id,
        True,
    )
    assert (created.cliffs.reform.id, created.cliffs.reform.inserted) == (
        proposed.reform_id,
        True,
    )
    assert created.cliffs.reform.status is SimulationStatus.PENDING
    # An identical request joins the report run and its runs' calculations.
    assert joined.run.inserted is False
    assert joined.cliffs is not None
    assert (joined.cliffs.baseline.id, joined.cliffs.baseline.inserted) == (
        proposed.baseline_id,
        False,
    )
    assert joined.cliffs.reform.id == proposed.reform_id


def test_two_identical_creates_asking_for_cliffs_at_once_insert_one_each(
    committed_engine, committed_catalog
) -> None:
    """The second waits on the first's rows, then joins its run and calculations.

    The first holds its transaction open until the second waits on its rows.
    """
    economy = committed_catalog
    first_has_inserted = threading.Event()
    found: dict[str, FoundEconomyReport] = {}

    def create(connection: Connection, name: str) -> None:
        act_as_the_service(connection)
        found[name] = in_session(
            connection,
            find_or_create_economy_report,
            _keyed_report(economy, cliffs=_new_cliffs()),
        )

    def first_request() -> None:
        with committed_engine.begin() as connection:
            create(connection, "first")
            holder_pid = connection.execute(
                text("SELECT pg_backend_pid()")
            ).scalar_one()
            first_has_inserted.set()
            wait_until_another_backend_waits_for(committed_engine, holder_pid)

    def second_request() -> None:
        assert first_has_inserted.wait(LOCK_WAIT_TIMEOUT_SECONDS)
        with committed_engine.begin() as connection:
            create(connection, "second")

    run_together((first_request, second_request), released=first_has_inserted)

    first, second = found["first"].cliffs, found["second"].cliffs
    assert first is not None
    assert second is not None
    assert (first.baseline.inserted, second.baseline.inserted) == (True, False)
    assert second.baseline.id == first.baseline.id
    assert second.reform.id == first.reform.id
    with committed_engine.begin() as connection:
        calculations = _calculations_of(connection, first.baseline.simulation_run_id)
    assert calculations == 1


def test_a_request_losing_the_report_run_adds_the_winners_runs_calculations(
    committed_engine, committed_catalog
) -> None:
    """Its savepoint takes back its simulation run; the cliffs follow the winner."""
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
    proposed = _new_cliffs()
    request = _keyed_report(economy, cliffs=proposed)

    def create_a_competing_run_first(
        _connection, _cursor, statement, _parameters, _context, _executemany
    ) -> None:
        if competing or not statement.lstrip().startswith(
            "INSERT INTO public.report_runs"
        ):
            return
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
        on_the_proposed_run = _calculations_of(
            connection, request.baseline.simulation_run_id
        )

    assert (joined.run.id, joined.run.inserted) == (competing[0], False)
    assert joined.cliffs is not None
    assert joined.cliffs.baseline.id == proposed.baseline_id
    assert joined.cliffs.baseline.simulation_run_id == first.baseline.simulation_run_id
    assert on_the_proposed_run == 0


def test_a_report_not_asking_for_cliffs_inserts_no_calculation(
    public_api_database_url,
) -> None:
    with rolled_back_transaction(public_api_database_url) as connection:
        economy = insert_economy_catalog(connection)
        act_as_the_service(connection)

        created = economy_store_on(connection).find_or_create_report(
            _keyed_report(economy)
        )

        assert created.cliffs is None
        assert _calculations_of(connection, created.baseline.simulation_run_id) == 0


def test_one_run_on_both_sides_gets_one_calculation(public_api_database_url) -> None:
    with rolled_back_transaction(public_api_database_url) as connection:
        economy = insert_economy_catalog(connection)
        act_as_the_service(connection)
        proposed = _new_cliffs()

        created = economy_store_on(connection).find_or_create_report(
            _keyed_report(economy, cliffs=proposed)
        )

        assert created.cliffs is not None
        assert created.cliffs.baseline == created.cliffs.reform
        assert created.cliffs.baseline.id == proposed.baseline_id
        assert _calculations_of(connection, created.baseline.simulation_run_id) == 1


def test_a_report_whose_calculations_cannot_be_inserted_writes_nothing(
    public_api_database_url,
) -> None:
    """Review round 1: the database refusing a calculation refuses the report."""
    with rolled_back_transaction(public_api_database_url) as connection:
        economy = insert_economy_catalog(connection)
        other_run_id = insert_simulation_run(
            connection, economy.population()
        ).simulation_run_id
        act_as_the_service(connection)
        taken_id = uuid4()
        in_session(
            connection, find_or_insert_cliff_calculations, {other_run_id: taken_id}
        )
        report = _keyed_report(
            economy, cliffs=ProposedCliffs(baseline_id=taken_id, reform_id=uuid4())
        )

        with pytest.raises(IntegrityError):
            economy_store_on(connection).find_or_create_report(report)

        written = connection.execute(
            select(func.count()).where(
                Report.definition_key == report.definition.definition_key
            )
        ).scalar_one()
        assert written == 0


def _stored_report_with_a_failed_run(
    connection: Connection,
    economy: EconomyCatalog,
    store: EconomyRunStore,
    reform: SimulationChoice = CURRENT_LAW,
) -> FoundEconomyReport:
    """Create a keyed report and fail its run, so a re-run creates a new one."""
    created = store.find_or_create_report(_keyed_report(economy, reform=reform))
    connection.execute(text("RESET ROLE"))
    connection.execute(
        text(
            "UPDATE public.report_runs SET status = 'failed', completed_at = now() "
            "WHERE id = :id"
        ),
        {"id": created.run.id},
    )
    act_as_the_service(connection)
    return created


def _rerun_of(
    economy: EconomyCatalog,
    created: FoundEconomyReport,
    *,
    forced: bool = False,
) -> NewReportRerun:
    return NewReportRerun(
        report_id=created.report_id,
        run_id=uuid4(),
        population=economy.population(),
        versions=VERSIONS,
        baseline=RerunSide(created.baseline.simulation_id, uuid4()),
        reform=RerunSide(created.reform.simulation_id, uuid4()),
        forced=forced,
    )


def test_a_rerun_asking_for_cliffs_adds_its_runs_calculations_joined_or_created(
    public_api_database_url,
) -> None:
    """Answer 4b: a re-run on the current versions joins and adds its cliffs."""
    with rolled_back_transaction(public_api_database_url) as connection:
        economy = insert_economy_catalog(connection)
        policy_id = insert_policy(connection, economy.catalog)
        act_as_the_service(connection)
        store = economy_store_on(connection)
        created = _stored_report_with_a_failed_run(
            connection, economy, store, SimulationChoice(policy_id, None)
        )
        proposed = _new_cliffs()

        rerun = store.create_report_rerun_with_cliffs(
            _rerun_of(economy, created), proposed
        )
        joined = store.create_report_rerun_with_cliffs(
            _rerun_of(economy, created), _new_cliffs()
        )

    assert rerun.run.inserted is True
    assert (rerun.cliffs.baseline.id, rerun.cliffs.baseline.inserted) == (
        proposed.baseline_id,
        True,
    )
    assert (rerun.cliffs.reform.id, rerun.cliffs.reform.inserted) == (
        proposed.reform_id,
        True,
    )
    assert rerun.cliffs.baseline.simulation_run_id == (
        rerun.run.baseline_simulation_run_id
    )
    assert rerun.cliffs.reform.simulation_run_id == rerun.run.reform_simulation_run_id
    assert rerun.run.baseline_simulation_run_id != rerun.run.reform_simulation_run_id
    assert (joined.run.id, joined.run.inserted) == (rerun.run.id, False)
    assert (joined.cliffs.baseline.id, joined.cliffs.baseline.inserted) == (
        proposed.baseline_id,
        False,
    )
    assert (joined.cliffs.reform.id, joined.cliffs.reform.inserted) == (
        proposed.reform_id,
        False,
    )


def test_a_forced_rerun_asking_for_cliffs_adds_its_new_runs_calculations(
    public_api_database_url,
) -> None:
    with rolled_back_transaction(public_api_database_url) as connection:
        economy = insert_economy_catalog(connection)
        act_as_the_service(connection)
        store = economy_store_on(connection)
        created = _stored_report_with_a_failed_run(connection, economy, store)
        forced = _rerun_of(economy, created, forced=True)

        rerun = store.create_report_rerun_with_cliffs(forced, _new_cliffs())
        unasked = store.create_report_rerun(_rerun_of(economy, created, forced=True))

        assert rerun.cliffs.baseline.inserted is True
        assert _calculations_of(connection, forced.baseline.simulation_run_id) == 1
        assert (
            _calculations_of(
                connection,
                unasked.baseline_simulation_run_id,
                unasked.reform_simulation_run_id,
            )
            == 0
        )


def test_a_cliff_calculations_call_is_recorded_while_it_is_pending_with_none(
    public_api_database_url,
) -> None:
    with rolled_back_transaction(public_api_database_url) as connection:
        run_id = _seed_simulation_run(connection)
        act_as_the_service(connection)
        store = economy_store_on(connection)
        cliff_result_id = uuid4()
        in_session(
            connection, find_or_insert_cliff_calculations, {run_id: cliff_result_id}
        )

        recorded = store.record_cliff_call(cliff_result_id, "fc-first")
        again = store.record_cliff_call(cliff_result_id, "fc-second")

        assert recorded is SimulationStatus.PENDING
        assert again is SimulationStatus.PENDING
        assert _cliff_row(connection, cliff_result_id).modal_call_id == "fc-first"
        assert store.record_cliff_call(uuid4(), "fc-none") is None


def test_a_cliff_calculation_whose_call_was_refused_is_failed_only_while_unstarted(
    public_api_database_url,
) -> None:
    with rolled_back_transaction(public_api_database_url) as connection:
        economy = insert_economy_catalog(connection)
        refused_run_id = insert_simulation_run(
            connection, economy.population()
        ).simulation_run_id
        started_run_id = insert_simulation_run(
            connection, economy.population()
        ).simulation_run_id
        act_as_the_service(connection)
        store = economy_store_on(connection)
        refused_id, started_id = uuid4(), uuid4()
        in_session(
            connection,
            find_or_insert_cliff_calculations,
            {refused_run_id: refused_id, started_run_id: started_id},
        )
        store.record_cliff_call(started_id, "fc-started")

        failed = store.fail_unstarted_cliff_calculation(
            refused_id, "modal_call_refused: Modal did not accept the call."
        )
        left = store.fail_unstarted_cliff_calculation(
            started_id, "modal_call_refused: Modal did not accept the call."
        )

        assert (failed, left) == (True, False)
        refused_row = _cliff_row(connection, refused_id)
        assert refused_row.status == SimulationStatus.FAILED
        assert refused_row.error_message.startswith("modal_call_refused: ")
        assert refused_row.completed_at is not None
        assert _cliff_row(connection, started_id).status == SimulationStatus.PENDING
