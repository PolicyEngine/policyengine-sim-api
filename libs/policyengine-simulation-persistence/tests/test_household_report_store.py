"""The household report store: each read and write in a transaction of its own.

A household report calculates one stored household under a baseline policy
and, optionally, a reform. These run against a database the Public API's own
v2 migration chain built (``PUBLIC_API_SCHEMA_DATABASE_URL``); each store
transaction is a savepoint of one connection acting as the simulation
service's role, which the test rolls back.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from conftest import act_as_the_service, rolled_back_transaction
from policyengine_simulation_persistence import (
    EconomyRecordOutcomeUnknownError,
    EconomyRecordReferenceError,
    HouseholdReportSide,
    HouseholdReportStore,
    MissingPolicyParametersError,
    NewHouseholdReport,
    ReportRunStatus,
    ReportSideError,
    RunVersions,
    UnknownCatalogVersionError,
    UnknownHouseholdError,
    UnknownPolicyError,
)
from public_api_rows import (
    StoredValue,
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

POLICYENGINE_VERSION = "6.17.1"
VERSIONS = RunVersions(
    country_package_version="1.764.6", policyengine_version=POLICYENGINE_VERSION
)
START = datetime(2026, 1, 1, tzinfo=UTC)
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
OPTIONS = {"add_missing_variables": True}


def _store_on(connection: Connection) -> HouseholdReportStore:
    """Return a store whose transactions are savepoints of ``connection``."""

    @contextmanager
    def savepoint() -> Iterator[Connection]:
        with connection.begin_nested():
            yield connection

    return HouseholdReportStore(savepoint)


def _catalog(connection: Connection) -> CatalogVersion:
    return insert_catalog_version(
        connection,
        country="us",
        model_id=insert_model(connection, "us"),
        policyengine_version=POLICYENGINE_VERSION,
        parameter_names=(CTC_AMOUNT,),
    )


def _household(connection: Connection, *, default_year: int | None = 2026) -> UUID:
    return insert_household(
        connection, country="us", default_year=default_year, document=DOCUMENT
    )


def _new_report(
    catalog: CatalogVersion,
    household_id: UUID,
    *,
    reform_policy_id: UUID | None = None,
    with_reform: bool = True,
) -> NewHouseholdReport:
    return NewHouseholdReport(
        id=uuid4(),
        run_id=uuid4(),
        country="us",
        label="A larger child tax credit",
        description="For one family",
        tax_benefit_model_id=catalog.model_id,
        tax_benefit_model_version_id=catalog.version_id,
        household_id=household_id,
        year=2026,
        versions=VERSIONS,
        inputs=OPTIONS,
        baseline=HouseholdReportSide(
            simulation_id=uuid4(), simulation_run_id=uuid4(), policy_id=None
        ),
        reform=(
            HouseholdReportSide(
                simulation_id=uuid4(),
                simulation_run_id=uuid4(),
                policy_id=reform_policy_id,
            )
            if with_reform
            else None
        ),
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


def test_a_household_reports_inputs_are_read_together(public_api_database_url) -> None:
    with rolled_back_transaction(public_api_database_url) as connection:
        catalog = _catalog(connection)
        household_id = _household(connection)
        policy_id = insert_policy(
            connection, catalog, [StoredValue(CTC_AMOUNT, 3_000, START)]
        )
        act_as_the_service(connection)

        inputs = _store_on(connection).read_inputs(
            country="us",
            household_id=household_id,
            policy_ids={"baseline": None, "reform": policy_id},
            policyengine_version=POLICYENGINE_VERSION,
        )

    assert inputs.catalog_version_id == catalog.version_id
    assert inputs.tax_benefit_model_id == catalog.model_id
    assert inputs.household.id == household_id
    assert inputs.household.default_year == 2026
    assert inputs.household.document == DOCUMENT
    assert inputs.reforms == {
        "baseline": {},
        "reform": {CTC_AMOUNT: {"2026-01-01.2200-12-31": 3_000}},
    }


def test_each_missing_input_is_refused(public_api_database_url) -> None:
    with rolled_back_transaction(public_api_database_url) as connection:
        catalog = _catalog(connection)
        household_id = _household(connection)
        act_as_the_service(connection)
        store = _store_on(connection)
        missing_policy_id = uuid4()

        with pytest.raises(UnknownCatalogVersionError):
            store.read_inputs(
                country="us",
                household_id=household_id,
                policy_ids={"baseline": None},
                policyengine_version="0.0.1",
            )
        with pytest.raises(UnknownHouseholdError):
            store.read_inputs(
                country="us",
                household_id=uuid4(),
                policy_ids={"baseline": None},
                policyengine_version=POLICYENGINE_VERSION,
            )
        with pytest.raises(ReportSideError) as unknown_policy:
            store.read_inputs(
                country="us",
                household_id=household_id,
                policy_ids={"baseline": None, "reform": missing_policy_id},
                policyengine_version=POLICYENGINE_VERSION,
            )

    assert catalog.policyengine_version == POLICYENGINE_VERSION
    assert unknown_policy.value.side == "reform"
    assert isinstance(unknown_policy.value.error, UnknownPolicyError)


def test_a_policy_setting_a_parameter_the_version_lacks_is_refused_on_its_side(
    public_api_database_url,
) -> None:
    with rolled_back_transaction(public_api_database_url) as connection:
        catalog = _catalog(connection)
        household_id = _household(connection)
        # A policy saved on an older catalog version, naming a parameter the
        # running version lacks.
        older = insert_catalog_version(
            connection,
            country="us",
            model_id=catalog.model_id,
            policyengine_version="6.0.0",
            parameter_names=("gov.removed.parameter",),
        )
        policy_id = insert_policy(
            connection, older, [StoredValue("gov.removed.parameter", 1, START)]
        )
        act_as_the_service(connection)

        with pytest.raises(ReportSideError) as refused:
            _store_on(connection).read_inputs(
                country="us",
                household_id=household_id,
                policy_ids={"baseline": policy_id, "reform": None},
                policyengine_version=POLICYENGINE_VERSION,
            )

    assert refused.value.side == "baseline"
    assert isinstance(refused.value.error, MissingPolicyParametersError)
    assert refused.value.error.parameter_names == ("gov.removed.parameter",)


def test_a_household_report_is_created_with_its_simulations_and_pending_runs(
    public_api_database_url,
) -> None:
    with rolled_back_transaction(public_api_database_url) as connection:
        catalog = _catalog(connection)
        household_id = _household(connection)
        policy_id = insert_policy(connection, catalog)
        act_as_the_service(connection)
        report = _new_report(catalog, household_id, reform_policy_id=policy_id)
        assert report.reform is not None

        _store_on(connection).create_report(report)
        stored_report = _row(connection, "reports", report.id)
        stored_run = _row(connection, "report_runs", report.run_id)
        simulations = [
            _row(connection, "simulations", side.simulation_id)
            for side in (report.baseline, report.reform)
        ]
        simulation_runs = [
            _row(connection, "simulation_runs", side.simulation_run_id)
            for side in (report.baseline, report.reform)
        ]

    assert report.simulation_run_ids == (
        report.baseline.simulation_run_id,
        report.reform.simulation_run_id,
    )
    assert {
        key: stored_report[key]
        for key in (
            "label",
            "description",
            "country",
            "type",
            "tax_benefit_model_id",
            "policy_id",
            "baseline_simulation_id",
            "reform_simulation_id",
            "household_id",
            "dataset_id",
            "region_id",
            "year",
            "budget_window_size",
            "inputs",
        )
    } == {
        "label": "A larger child tax credit",
        "description": "For one family",
        "country": "us",
        "type": "household",
        "tax_benefit_model_id": catalog.model_id,
        "policy_id": policy_id,
        "baseline_simulation_id": report.baseline.simulation_id,
        "reform_simulation_id": report.reform.simulation_id,
        "household_id": household_id,
        "dataset_id": None,
        "region_id": None,
        "year": 2026,
        "budget_window_size": None,
        "inputs": OPTIONS,
    }
    assert (stored_run["status"], stored_run["trigger"]) == ("pending", "initial")
    assert stored_run["modal_call_id"] is None
    assert (
        stored_run["baseline_simulation_run_id"],
        stored_run["reform_simulation_run_id"],
    ) == report.simulation_run_ids
    assert (
        stored_run["country_package_version"],
        stored_run["policyengine_version"],
    ) == ("1.764.6", POLICYENGINE_VERSION)
    assert [simulation["simulation_type"] for simulation in simulations] == [
        "household",
        "household",
    ]
    assert [simulation["policy_id"] for simulation in simulations] == [
        None,
        policy_id,
    ]
    for simulation in simulations:
        assert simulation["household_id"] == household_id
        assert simulation["tax_benefit_model_version_id"] == catalog.version_id
        assert (simulation["dataset_id"], simulation["region_id"]) == (None, None)
        assert (simulation["dynamic_id"], simulation["year"]) == (None, 2026)
    for run in simulation_runs:
        assert (run["status"], run["modal_call_id"]) == ("pending", None)
        assert (run["dataset_id"], run["region_id"], run["year"]) == (None, None, 2026)
        assert run["household_result"] is None
        assert run["policyengine_version"] == POLICYENGINE_VERSION


def test_a_report_of_the_baseline_alone_names_no_reform(
    public_api_database_url,
) -> None:
    with rolled_back_transaction(public_api_database_url) as connection:
        catalog = _catalog(connection)
        household_id = _household(connection)
        act_as_the_service(connection)
        report = _new_report(catalog, household_id, with_reform=False)

        _store_on(connection).create_report(report)
        stored_report = _row(connection, "reports", report.id)
        stored_run = _row(connection, "report_runs", report.run_id)

    assert report.simulation_run_ids == (report.baseline.simulation_run_id,)
    assert stored_report["reform_simulation_id"] is None
    assert stored_report["policy_id"] is None
    assert stored_run["reform_simulation_run_id"] is None


def test_a_report_run_records_its_call_or_is_failed_with_its_runs(
    public_api_database_url,
) -> None:
    with rolled_back_transaction(public_api_database_url) as connection:
        catalog = _catalog(connection)
        household_id = _household(connection)
        act_as_the_service(connection)
        store = _store_on(connection)
        recorded, refused = (_new_report(catalog, household_id) for _ in range(2))
        store.create_report(recorded)
        store.create_report(refused)

        status = store.record_report_call(recorded.run_id, "fc-household")
        recorded_not_failed = store.fail_unstarted_report(
            recorded.run_id, recorded.simulation_run_ids, "modal_call_refused: late"
        )
        failed = store.fail_unstarted_report(
            refused.run_id, refused.simulation_run_ids, "modal_call_refused: no"
        )
        recorded_run = _row(connection, "report_runs", recorded.run_id)
        refused_run = _row(connection, "report_runs", refused.run_id)
        refused_simulation_runs = [
            _row(connection, "simulation_runs", run_id)
            for run_id in refused.simulation_run_ids
        ]

    assert (status, recorded_not_failed, failed) == (
        ReportRunStatus.PENDING,
        False,
        True,
    )
    assert (recorded_run["status"], recorded_run["modal_call_id"]) == (
        "pending",
        "fc-household",
    )
    assert (refused_run["status"], refused_run["error_message"]) == (
        "failed",
        "modal_call_refused: no",
    )
    assert [run["status"] for run in refused_simulation_runs] == ["failed", "failed"]


def test_a_report_naming_a_missing_household_is_refused_by_its_key(
    public_api_database_url,
) -> None:
    with rolled_back_transaction(public_api_database_url) as connection:
        catalog = _catalog(connection)
        act_as_the_service(connection)

        with pytest.raises(EconomyRecordReferenceError) as refused:
            _store_on(connection).create_report(_new_report(catalog, uuid4()))

    assert refused.value.column_name == "household_id"


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
    def __init__(self) -> None:
        self.statements: list[object] = []

    def execute(self, statement, *arguments):
        self.statements.append(statement)


def test_a_create_whose_commit_fails_may_have_been_written() -> None:
    connection = _RecordingConnection()
    store = HouseholdReportStore(lambda: _CommitFails(connection))
    catalog_rows = {"model_id": uuid4(), "version_id": uuid4()}

    with pytest.raises(EconomyRecordOutcomeUnknownError) as raised:
        store.create_report(
            NewHouseholdReport(
                id=uuid4(),
                run_id=uuid4(),
                country="us",
                label="Lost commit",
                description=None,
                tax_benefit_model_id=catalog_rows["model_id"],
                tax_benefit_model_version_id=catalog_rows["version_id"],
                household_id=uuid4(),
                year=2026,
                versions=VERSIONS,
                inputs=OPTIONS,
                baseline=HouseholdReportSide(uuid4(), uuid4(), None),
                reform=None,
            )
        )

    assert isinstance(raised.value.__cause__, OperationalError)
    assert connection.statements
