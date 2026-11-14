"""Find a shared simulation, report or run by its definition or versions, or insert it.

These run against a database the Public API's own v2 migration chain built
(``PUBLIC_API_SCHEMA_DATABASE_URL``), as the simulation service's role. Most
run in a transaction rolled back afterwards. The tests of two transactions
commit their rows, under a catalog of a country no other test uses, and
delete them afterwards.
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
from conftest import act_as_the_service, rolled_back_transaction
from economy_rows import (
    CURRENT_LAW,
    LATER_VERSIONS,
    VERSIONS,
    EconomyCatalog,
    insert_economy_catalog,
)
from policyengine_simulation_persistence import (
    KEY_VERSION,
    ReportRunStatus,
    ReportRunTrigger,
    RunVersions,
    SimulationStatus,
    SimulationType,
    definition_key,
    find_or_insert_report,
    find_or_insert_report_run,
    find_or_insert_simulation,
    find_or_insert_simulation_run,
    simulation_runs,
)
from sqlalchemy import event, insert, text, update

if TYPE_CHECKING:
    from sqlalchemy import Connection

pytestmark = pytest.mark.public_api_schema


def _key(name: str) -> str:
    return definition_key({"test": name})


def _simulation_values(economy: EconomyCatalog, key: str) -> dict[str, Any]:
    return {
        "id": uuid4(),
        "simulation_type": SimulationType.ECONOMY,
        "dataset_id": economy.dataset_id,
        "policy_id": CURRENT_LAW.policy_id,
        "dynamic_id": CURRENT_LAW.dynamic_id,
        "tax_benefit_model_version_id": economy.catalog.version_id,
        "region_id": economy.region_id,
        "year": 2026,
        "key_version": KEY_VERSION,
        "definition_key": key,
    }


def _report_values(
    economy: EconomyCatalog, key: str, simulation_id: UUID
) -> dict[str, Any]:
    return {
        "id": uuid4(),
        "label": "Shared report",
        "country": economy.catalog.country,
        "type": "economy",
        "tax_benefit_model_id": economy.catalog.model_id,
        "baseline_simulation_id": simulation_id,
        "reform_simulation_id": simulation_id,
        "dataset_id": economy.dataset_id,
        "region_id": economy.region_id,
        "year": 2026,
        "inputs": {"include_cliffs": False},
        "key_version": KEY_VERSION,
        "definition_key": key,
        "dataset_independent_key": _key(f"{key} without its dataset"),
    }


def _simulation_run_values(
    economy: EconomyCatalog, simulation_id: UUID, versions: RunVersions = VERSIONS
) -> dict[str, Any]:
    return {
        "id": uuid4(),
        "simulation_id": simulation_id,
        "year": 2026,
        "tax_benefit_model_version_id": economy.catalog.version_id,
        "country_package_version": versions.country_package_version,
        "policyengine_version": versions.policyengine_version,
        "dataset_id": economy.dataset_id,
        "region_id": economy.region_id,
        "status": SimulationStatus.PENDING,
    }


def _report_run_values(
    report_id: UUID, simulation_run_id: UUID, versions: RunVersions = VERSIONS
) -> dict[str, Any]:
    return {
        "id": uuid4(),
        "report_id": report_id,
        "baseline_simulation_run_id": simulation_run_id,
        "reform_simulation_run_id": simulation_run_id,
        "country_package_version": versions.country_package_version,
        "policyengine_version": versions.policyengine_version,
        "status": ReportRunStatus.PENDING,
        "trigger": ReportRunTrigger.INITIAL,
    }


def _fail(connection: Connection, table_name: str, row_id: UUID) -> None:
    """Fail a run as the superuser, as a worker or the sweep would."""
    connection.execute(
        text(
            f"UPDATE public.{table_name} SET status = 'failed', "  # noqa: S608  # fixed table names
            "completed_at = now() WHERE id = :id"
        ),
        {"id": row_id},
    )


def test_a_simulation_is_inserted_once_and_then_found_by_its_key(
    public_api_database_url,
) -> None:
    with rolled_back_transaction(public_api_database_url) as connection:
        economy = insert_economy_catalog(connection)
        act_as_the_service(connection)
        first_values = _simulation_values(economy, _key("simulation"))

        first = find_or_insert_simulation(connection, first_values)
        again = find_or_insert_simulation(
            connection, _simulation_values(economy, _key("simulation"))
        )
        other = find_or_insert_simulation(
            connection, _simulation_values(economy, _key("another simulation"))
        )

        assert (first.id, first.inserted) == (first_values["id"], True)
        assert (again.id, again.inserted) == (first.id, False)
        assert other.inserted is True
        assert other.id != first.id


def test_a_report_is_inserted_once_and_then_found_by_its_key(
    public_api_database_url,
) -> None:
    with rolled_back_transaction(public_api_database_url) as connection:
        economy = insert_economy_catalog(connection)
        act_as_the_service(connection)
        simulation = find_or_insert_simulation(
            connection, _simulation_values(economy, _key("simulation"))
        )
        first_values = _report_values(economy, _key("report"), simulation.id)

        first = find_or_insert_report(connection, first_values)
        again = find_or_insert_report(
            connection, _report_values(economy, _key("report"), simulation.id)
        )

        assert (first.id, first.inserted) == (first_values["id"], True)
        assert (again.id, again.inserted) == (first.id, False)


def test_a_simulation_run_is_joined_on_its_version_set_until_it_fails(
    public_api_database_url,
) -> None:
    with rolled_back_transaction(public_api_database_url) as connection:
        economy = insert_economy_catalog(connection)
        act_as_the_service(connection)
        simulation = find_or_insert_simulation(
            connection, _simulation_values(economy, _key("simulation"))
        )

        first = find_or_insert_simulation_run(
            connection, _simulation_run_values(economy, simulation.id)
        )
        joined = find_or_insert_simulation_run(
            connection, _simulation_run_values(economy, simulation.id)
        )
        later_versions = find_or_insert_simulation_run(
            connection, _simulation_run_values(economy, simulation.id, LATER_VERSIONS)
        )
        connection.execute(text("RESET ROLE"))
        _fail(connection, "simulation_runs", first.id)
        act_as_the_service(connection)
        after_failure = find_or_insert_simulation_run(
            connection, _simulation_run_values(economy, simulation.id)
        )

        assert first.inserted is True
        assert (joined.id, joined.inserted) == (first.id, False)
        assert later_versions.inserted is True
        assert after_failure.inserted is True
        assert after_failure.id not in {first.id, later_versions.id}


def test_a_forced_run_is_never_joined_and_never_takes_a_runs_place(
    public_api_database_url,
) -> None:
    with rolled_back_transaction(public_api_database_url) as connection:
        economy = insert_economy_catalog(connection)
        act_as_the_service(connection)
        simulation = find_or_insert_simulation(
            connection, _simulation_values(economy, _key("simulation"))
        )
        forced_values = _simulation_run_values(economy, simulation.id)
        connection.execute(insert(simulation_runs).values(**forced_values, forced=True))

        first = find_or_insert_simulation_run(
            connection, _simulation_run_values(economy, simulation.id)
        )
        # Another forced run beside an unforced one conflicts with neither.
        connection.execute(
            insert(simulation_runs).values(
                **_simulation_run_values(economy, simulation.id), forced=True
            )
        )

        assert first.inserted is True
        assert first.id != forced_values["id"]
        with pytest.raises(ValueError, match="forced"):
            find_or_insert_simulation_run(
                connection,
                {**_simulation_run_values(economy, simulation.id), "forced": True},
            )


def test_a_report_run_is_joined_on_its_versions_outside_budget_windows(
    public_api_database_url,
) -> None:
    with rolled_back_transaction(public_api_database_url) as connection:
        economy = insert_economy_catalog(connection)
        act_as_the_service(connection)
        simulation = find_or_insert_simulation(
            connection, _simulation_values(economy, _key("simulation"))
        )
        simulation_run = find_or_insert_simulation_run(
            connection, _simulation_run_values(economy, simulation.id)
        )
        report = find_or_insert_report(
            connection, _report_values(economy, _key("report"), simulation.id)
        )

        first = find_or_insert_report_run(
            connection, _report_run_values(report.id, simulation_run.id)
        )
        joined = find_or_insert_report_run(
            connection, _report_run_values(report.id, simulation_run.id)
        )

        assert first.inserted is True
        assert (joined.id, joined.inserted) == (first.id, False)
        assert joined.status is ReportRunStatus.PENDING
        assert joined.baseline_simulation_run_id == simulation_run.id
        assert joined.reform_simulation_run_id == simulation_run.id
        with pytest.raises(ValueError, match="budget window"):
            find_or_insert_report_run(
                connection,
                {
                    **_report_run_values(report.id, simulation_run.id),
                    "budget_window_run_id": uuid4(),
                    "year": 2026,
                },
            )


def test_a_transaction_inserting_a_run_another_has_inserted_waits_and_joins_it(
    committed_engine, committed_catalog
) -> None:
    economy = committed_catalog
    with committed_engine.begin() as connection:
        act_as_the_service(connection)
        simulation = find_or_insert_simulation(
            connection, _simulation_values(economy, _key("shared simulation"))
        )
    first_has_inserted = threading.Event()
    found: dict[str, Any] = {}

    def insert_then_commit_once_the_other_waits() -> None:
        with committed_engine.begin() as connection:
            act_as_the_service(connection)
            found["first"] = find_or_insert_simulation_run(
                connection, _simulation_run_values(economy, simulation.id)
            )
            holder_pid = connection.execute(
                text("SELECT pg_backend_pid()")
            ).scalar_one()
            first_has_inserted.set()
            wait_until_another_backend_waits_for(committed_engine, holder_pid)

    def insert_once_the_first_has() -> None:
        assert first_has_inserted.wait(LOCK_WAIT_TIMEOUT_SECONDS)
        with committed_engine.begin() as connection:
            act_as_the_service(connection)
            found["second"] = find_or_insert_simulation_run(
                connection, _simulation_run_values(economy, simulation.id)
            )

    run_together(
        (insert_then_commit_once_the_other_waits, insert_once_the_first_has),
        released=first_has_inserted,
    )

    assert found["first"].inserted is True
    assert (found["second"].id, found["second"].inserted) == (found["first"].id, False)


def test_a_run_that_fails_between_the_insert_and_the_read_is_inserted_again(
    committed_engine, committed_catalog
) -> None:
    economy = committed_catalog
    with committed_engine.begin() as connection:
        act_as_the_service(connection)
        simulation = find_or_insert_simulation(
            connection, _simulation_values(economy, _key("shared simulation"))
        )
        earlier = find_or_insert_simulation_run(
            connection, _simulation_run_values(economy, simulation.id)
        )

    def fail_the_earlier_run_before_the_read(
        _connection, _cursor, statement, _parameters, _context, _executemany
    ) -> None:
        if statement.lstrip().startswith("SELECT") and "simulation_runs" in statement:
            with committed_engine.begin() as other:
                other.execute(
                    update(simulation_runs)
                    .where(
                        simulation_runs.c.id == earlier.id,
                        simulation_runs.c.status != SimulationStatus.FAILED,
                    )
                    .values(status=SimulationStatus.FAILED, completed_at=text("now()"))
                )

    with committed_engine.begin() as connection:
        act_as_the_service(connection)
        event.listen(
            connection, "before_cursor_execute", fail_the_earlier_run_before_the_read
        )
        try:
            again = find_or_insert_simulation_run(
                connection, _simulation_run_values(economy, simulation.id)
            )
        finally:
            event.remove(
                connection,
                "before_cursor_execute",
                fail_the_earlier_run_before_the_read,
            )

    assert earlier.inserted is True
    assert again.inserted is True
    assert again.id != earlier.id
