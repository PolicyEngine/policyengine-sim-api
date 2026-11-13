"""Find household reports and their runs by key, or create them (answer 8).

Identical household report requests, together or apart, end on the same
simulation, report and run rows, as economy ones do. These run against a
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
from policyengine_simulation_persistence import (
    KEY_VERSION,
    FoundHouseholdReport,
    FoundSide,
    HouseholdReportDefinition,
    HouseholdReportStore,
    HouseholdRerunSide,
    KeyedHouseholdReport,
    NewHouseholdReportRerun,
    ProposedSide,
    ReportRunStatus,
    RunVersions,
    SimulationChoice,
    SimulationStatus,
    find_or_create_household_report,
    insert_household_report_rerun,
)
from public_api_rows import (
    CatalogVersion,
    insert_catalog_version,
    insert_household,
    insert_model,
    insert_policy,
)
from sqlalchemy import text

if TYPE_CHECKING:
    from collections.abc import Iterator

    from sqlalchemy import Connection

pytestmark = pytest.mark.public_api_schema

VERSIONS = RunVersions(country_package_version="1.764.6", policyengine_version="6.17.1")
CURRENT_LAW = SimulationChoice(policy_id=None, dynamic_id=None)
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


@dataclass(frozen=True)
class _StoredInputs:
    """What a household report names: a catalog version, a household and a policy."""

    catalog: CatalogVersion
    household_id: UUID
    reform_policy_id: UUID

    @property
    def reform(self) -> SimulationChoice:
        return SimulationChoice(policy_id=self.reform_policy_id, dynamic_id=None)


def _stored_inputs(connection: Connection) -> _StoredInputs:
    """Insert the US model's catalog version, a household and a policy."""
    catalog = insert_catalog_version(
        connection,
        country="us",
        model_id=insert_model(connection, "us"),
        policyengine_version=VERSIONS.policyengine_version,
    )
    return _StoredInputs(
        catalog=catalog,
        household_id=insert_household(
            connection, country="us", default_year=2026, document=DOCUMENT
        ),
        reform_policy_id=insert_policy(connection, catalog),
    )


def _keyed_report(
    stored: _StoredInputs, *, reform: SimulationChoice | None
) -> KeyedHouseholdReport:
    """Return a request's household report, with fresh IDs to propose."""
    return KeyedHouseholdReport(
        definition=HouseholdReportDefinition(
            country="us",
            baseline=CURRENT_LAW,
            reform=reform,
            household_id=stored.household_id,
            year=2026,
        ),
        report_id=uuid4(),
        run_id=uuid4(),
        baseline=ProposedSide(simulation_id=uuid4(), simulation_run_id=uuid4()),
        reform=(
            None
            if reform is None
            else ProposedSide(simulation_id=uuid4(), simulation_run_id=uuid4())
        ),
        tax_benefit_model_id=stored.catalog.model_id,
        tax_benefit_model_version_id=stored.catalog.version_id,
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


def _fail_report_run(connection: Connection, run_id: UUID) -> None:
    """Fail a report run as the superuser, as the household function would."""
    connection.execute(text("RESET ROLE"))
    connection.execute(
        text(
            "UPDATE public.report_runs SET status = 'failed', completed_at = now() "
            "WHERE id = :id"
        ),
        {"id": run_id},
    )
    act_as_the_service(connection)


def test_a_keyed_household_report_is_created_with_its_simulations_and_first_run(
    public_api_database_url,
) -> None:
    with rolled_back_transaction(public_api_database_url) as connection:
        stored = _stored_inputs(connection)
        request = _keyed_report(stored, reform=stored.reform)
        act_as_the_service(connection)

        found = find_or_create_household_report(connection, request)
        report = _row(connection, "reports", found.report_id)
        report_run = _row(connection, "report_runs", found.run.id)
        sides = {
            "baseline": found.baseline,
            "reform": found.reform,
        }
        stored_sides = {
            side: (
                _row(connection, "simulations", found_side.simulation_id),
                _row(connection, "simulation_runs", found_side.simulation_run_id),
            )
            for side, found_side in sides.items()
            if found_side is not None
        }

    assert request.reform is not None
    assert (found.report_id, found.report_inserted) == (request.report_id, True)
    assert (found.run.id, found.run.inserted, found.run.status) == (
        request.run_id,
        True,
        ReportRunStatus.PENDING,
    )
    assert found.baseline == FoundSide(
        simulation_id=request.baseline.simulation_id,
        simulation_run_id=request.baseline.simulation_run_id,
        run_inserted=True,
    )
    assert found.reform == FoundSide(
        simulation_id=request.reform.simulation_id,
        simulation_run_id=request.reform.simulation_run_id,
        run_inserted=True,
    )
    definition = request.definition
    assert {
        name: report[name]
        for name in (
            "type",
            "country",
            "household_id",
            "policy_id",
            "dataset_id",
            "region_id",
            "year",
            "budget_window_size",
            "inputs",
            "key_version",
            "definition_key",
            "dataset_independent_key",
            "label",
            "description",
            "user_id",
        )
    } == {
        "type": "household",
        "country": "us",
        "household_id": stored.household_id,
        "policy_id": stored.reform_policy_id,
        "dataset_id": None,
        "region_id": None,
        "year": 2026,
        "budget_window_size": None,
        "inputs": {},
        "key_version": KEY_VERSION,
        "definition_key": definition.definition_key,
        "dataset_independent_key": definition.dataset_independent_key,
        # Answer 6: a shared report holds no request's label or user.
        "label": None,
        "description": None,
        "user_id": None,
    }
    assert (report["baseline_simulation_id"], report["reform_simulation_id"]) == (
        request.baseline.simulation_id,
        request.reform.simulation_id,
    )
    assert report_run["trigger"] is None
    assert report_run["forced"] is False
    assert (
        report_run["baseline_simulation_run_id"],
        report_run["reform_simulation_run_id"],
    ) == (request.baseline.simulation_run_id, request.reform.simulation_run_id)
    for side, (simulation, simulation_run) in stored_sides.items():
        assert simulation["simulation_type"] == "household"
        assert simulation["household_id"] == stored.household_id
        assert (simulation["dataset_id"], simulation["region_id"]) == (None, None)
        assert simulation["key_version"] == KEY_VERSION
        side_key = definition.simulation(side).definition_key
        assert simulation["definition_key"] == side_key
        assert simulation_run["status"] == SimulationStatus.PENDING
        assert (simulation_run["year"], simulation_run["forced"]) == (2026, False)
        assert simulation_run["tax_benefit_model_version_id"] == (
            stored.catalog.version_id
        )
        assert (
            simulation_run["country_package_version"],
            simulation_run["policyengine_version"],
        ) == (VERSIONS.country_package_version, VERSIONS.policyengine_version)


def test_an_identical_household_request_ends_on_the_same_rows_and_inserts_nothing(
    public_api_database_url,
) -> None:
    with rolled_back_transaction(public_api_database_url) as connection:
        stored = _stored_inputs(connection)
        act_as_the_service(connection)
        first = find_or_create_household_report(
            connection, _keyed_report(stored, reform=stored.reform)
        )
        counts = [
            _count(connection, name)
            for name in ("simulations", "reports", "simulation_runs", "report_runs")
        ]

        again = find_or_create_household_report(
            connection, _keyed_report(stored, reform=stored.reform)
        )
        counts_again = [
            _count(connection, name)
            for name in ("simulations", "reports", "simulation_runs", "report_runs")
        ]

    assert (again.report_id, again.report_inserted) == (first.report_id, False)
    assert (again.run.id, again.run.inserted) == (first.run.id, False)
    assert again.baseline == replace(first.baseline, run_inserted=False)
    assert again.reform == replace(first.reform, run_inserted=False)
    assert counts_again == counts


def test_a_household_policy_compared_with_itself_names_one_simulation_run_twice(
    public_api_database_url,
) -> None:
    """Its two sides are one simulation, so one run, with the baseline's ID."""
    with rolled_back_transaction(public_api_database_url) as connection:
        stored = _stored_inputs(connection)
        request = _keyed_report(stored, reform=CURRENT_LAW)
        act_as_the_service(connection)

        found = find_or_create_household_report(connection, request)
        report_run = _row(connection, "report_runs", found.run.id)

    assert found.reform is not None
    assert found.baseline.simulation_id == found.reform.simulation_id
    assert found.baseline.simulation_run_id == found.reform.simulation_run_id
    assert found.baseline.simulation_run_id == request.baseline.simulation_run_id
    assert report_run["baseline_simulation_run_id"] == (
        report_run["reform_simulation_run_id"]
    )


def test_a_household_report_of_the_baseline_alone_names_no_reform(
    public_api_database_url,
) -> None:
    with rolled_back_transaction(public_api_database_url) as connection:
        stored = _stored_inputs(connection)
        request = _keyed_report(stored, reform=None)
        act_as_the_service(connection)

        found = find_or_create_household_report(connection, request)
        again = find_or_create_household_report(
            connection, _keyed_report(stored, reform=None)
        )
        report = _row(connection, "reports", found.report_id)
        report_run = _row(connection, "report_runs", found.run.id)

    assert found.reform is None
    assert found.run.reform_simulation_run_id is None
    assert (report["reform_simulation_id"], report["policy_id"]) == (None, None)
    assert report_run["reform_simulation_run_id"] is None
    assert (again.report_id, again.run.id, again.reform) == (
        found.report_id,
        found.run.id,
        None,
    )


def test_a_household_report_sharing_a_side_names_that_sides_run_under_way(
    public_api_database_url,
) -> None:
    """Answer 5: a report against current law joins current law's pending run."""
    with rolled_back_transaction(public_api_database_url) as connection:
        stored = _stored_inputs(connection)
        act_as_the_service(connection)
        with_reform = find_or_create_household_report(
            connection, _keyed_report(stored, reform=stored.reform)
        )

        alone = find_or_create_household_report(
            connection, _keyed_report(stored, reform=None)
        )

    assert alone.report_id != with_reform.report_id
    assert alone.run.inserted is True
    assert alone.baseline == replace(with_reform.baseline, run_inserted=False)


def test_a_household_report_whose_run_failed_gets_a_new_run_on_its_simulations(
    public_api_database_url,
) -> None:
    """Answer 2: a failed report run never counts; its simulation runs still do."""
    with rolled_back_transaction(public_api_database_url) as connection:
        stored = _stored_inputs(connection)
        act_as_the_service(connection)
        first = find_or_create_household_report(
            connection, _keyed_report(stored, reform=stored.reform)
        )
        _fail_report_run(connection, first.run.id)

        second = find_or_create_household_report(
            connection, _keyed_report(stored, reform=stored.reform)
        )

    assert second.report_id == first.report_id
    assert (second.run.inserted, second.run.id == first.run.id) == (True, False)
    assert second.baseline == replace(first.baseline, run_inserted=False)
    assert second.reform == replace(first.reform, run_inserted=False)


def test_the_household_store_finds_or_creates_a_report_in_a_transaction(
    public_api_database_url,
) -> None:
    with rolled_back_transaction(public_api_database_url) as connection:
        stored = _stored_inputs(connection)
        act_as_the_service(connection)

        @contextmanager
        def savepoint() -> Iterator[Connection]:
            with connection.begin_nested():
                yield connection

        store = HouseholdReportStore(savepoint)
        found = store.find_or_create_report(_keyed_report(stored, reform=None))
        again = store.find_or_create_report(_keyed_report(stored, reform=None))

    assert (found.run.inserted, again.run.inserted) == (True, False)
    assert again.run.id == found.run.id


def test_a_request_proposing_a_reform_side_for_a_report_without_one_is_refused() -> (
    None
):
    stored = _StoredInputs(
        catalog=CatalogVersion(
            country="us",
            model_id=uuid4(),
            version_id=uuid4(),
            policyengine_version=VERSIONS.policyengine_version,
        ),
        household_id=uuid4(),
        reform_policy_id=uuid4(),
    )
    request = _keyed_report(stored, reform=None)

    with pytest.raises(ValueError, match="reform side"):
        replace(
            request,
            reform=ProposedSide(simulation_id=uuid4(), simulation_run_id=uuid4()),
        )


def _rerun_of(
    found: FoundHouseholdReport, stored: _StoredInputs, *, forced: bool = False
) -> NewHouseholdReportRerun:
    """Return a re-run of a keyed report on its versions, proposing new run IDs."""
    return NewHouseholdReportRerun(
        report_id=found.report_id,
        run_id=uuid4(),
        tax_benefit_model_version_id=stored.catalog.version_id,
        year=2026,
        versions=VERSIONS,
        baseline=HouseholdRerunSide(
            simulation_id=found.baseline.simulation_id, simulation_run_id=uuid4()
        ),
        reform=(
            None
            if found.reform is None
            else HouseholdRerunSide(
                simulation_id=found.reform.simulation_id, simulation_run_id=uuid4()
            )
        ),
        forced=forced,
    )


def test_a_keyed_household_reports_rerun_joins_its_run_on_the_versions(
    public_api_database_url,
) -> None:
    """Answers 3 and 8: a re-run joins the run under way, creating nothing."""
    with rolled_back_transaction(public_api_database_url) as connection:
        stored = _stored_inputs(connection)
        act_as_the_service(connection)
        found = find_or_create_household_report(
            connection, _keyed_report(stored, reform=stored.reform)
        )
        counts = [
            _count(connection, name) for name in ("simulation_runs", "report_runs")
        ]

        joined = insert_household_report_rerun(connection, _rerun_of(found, stored))
        counts_after = [
            _count(connection, name) for name in ("simulation_runs", "report_runs")
        ]

    assert joined == replace(found.run, inserted=False)
    assert counts_after == counts


def test_a_keyed_household_rerun_after_a_failed_run_names_its_simulations_runs(
    public_api_database_url,
) -> None:
    """Each side joins its simulation's run on the versions, as a create's does."""
    with rolled_back_transaction(public_api_database_url) as connection:
        stored = _stored_inputs(connection)
        act_as_the_service(connection)
        found = find_or_create_household_report(
            connection, _keyed_report(stored, reform=stored.reform)
        )
        _fail_report_run(connection, found.run.id)
        rerun = _rerun_of(found, stored)

        created = insert_household_report_rerun(connection, rerun)
        report_run = _row(connection, "report_runs", created.id)

    assert found.reform is not None
    assert (created.id, created.inserted) == (rerun.run_id, True)
    assert (
        created.baseline_simulation_run_id,
        created.reform_simulation_run_id,
    ) == (found.baseline.simulation_run_id, found.reform.simulation_run_id)
    assert (report_run["trigger"], report_run["forced"]) == (None, False)


def test_a_forced_keyed_household_rerun_is_an_extra_run_beside_the_one_under_way(
    public_api_database_url,
) -> None:
    """Answer 3: never joined, never refused, on new runs of both sides."""
    with rolled_back_transaction(public_api_database_url) as connection:
        stored = _stored_inputs(connection)
        act_as_the_service(connection)
        found = find_or_create_household_report(
            connection, _keyed_report(stored, reform=stored.reform)
        )
        rerun = _rerun_of(found, stored, forced=True)

        created = insert_household_report_rerun(connection, rerun)
        report_run = _row(connection, "report_runs", created.id)
        side_runs = [
            _row(connection, "simulation_runs", run_id)
            for run_id in rerun.simulation_run_ids
        ]

    assert (created.id, created.inserted) == (rerun.run_id, True)
    assert report_run["forced"] is True
    assert [side_run["forced"] for side_run in side_runs] == [True, True]
