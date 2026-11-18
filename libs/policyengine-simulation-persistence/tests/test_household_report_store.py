"""The household report store: each read and write in a transaction of its own.

A household report calculates one stored household under a baseline policy
and, optionally, a reform. These run against a database the Public API's own
v2 migration chain built (``PUBLIC_API_SCHEMA_DATABASE_URL``); each store
transaction is an ORM session in a savepoint of one connection acting as the
simulation service's role, which the test rolls back.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from conftest import act_as_the_service, rolled_back_transaction, session_factory_on
from household_report_rows import insert_household_report_rows
from policyengine_simulation_persistence import (
    HouseholdReportStore,
    MissingPolicyParametersError,
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

if TYPE_CHECKING:
    from uuid import UUID

    from public_api_rows import CatalogVersion
    from sqlalchemy import Connection

pytestmark = pytest.mark.public_api_schema

STATEMENT_TIMEOUT_MILLISECONDS = 5_000
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


def _store_on(connection: Connection) -> HouseholdReportStore:
    """Return a store whose transactions are savepoints of ``connection``."""
    return HouseholdReportStore.from_session_factory(
        session_factory_on(connection),
        statement_timeout_milliseconds=STATEMENT_TIMEOUT_MILLISECONDS,
    )


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


def test_a_report_run_records_its_call_or_is_failed_with_its_runs(
    public_api_database_url,
) -> None:
    with rolled_back_transaction(public_api_database_url) as connection:
        catalog = _catalog(connection)
        household_id = _household(connection)
        recorded, refused = (
            insert_household_report_rows(
                connection,
                catalog,
                household_id=household_id,
                year=2026,
                versions=VERSIONS,
            )
            for _ in range(2)
        )
        act_as_the_service(connection)
        store = _store_on(connection)

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
