"""The household calculation store: each read and write in a transaction of its own.

These run against a database the Public API's own v2 migration chain built
(``PUBLIC_API_SCHEMA_DATABASE_URL``). The store opens each transaction as an
ORM session from the factory it is given; here each session's transaction is
a savepoint of one connection, acting as the simulation service's role,
which the test rolls back.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from conftest import act_as_the_service, rolled_back_transaction, session_factory_on
from policyengine_simulation_persistence import (
    EconomyRecordOutcomeUnknownError,
    HouseholdJobReferenceError,
    HouseholdJobStatus,
    HouseholdJobStore,
    NewHouseholdJob,
    UnknownCatalogVersionError,
    UnknownHouseholdError,
    UnknownPolicyError,
    create_session_factory,
    create_simulation_engine,
)
from public_api_rows import (
    StoredValue,
    insert_catalog_version,
    insert_household,
    insert_model,
    insert_policy,
)
from sqlalchemy.exc import OperationalError
from transaction_fakes import (
    CommitFailingTransaction,
    FlushFailingTransaction,
    RecordingSession,
)

if TYPE_CHECKING:
    from sqlalchemy import Connection

pytestmark = pytest.mark.public_api_schema

STATEMENT_TIMEOUT_MILLISECONDS = 5_000
POLICYENGINE_VERSION = "6.17.1"
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


def _store_on(connection: Connection) -> HouseholdJobStore:
    """Return a store whose transactions are savepoints of ``connection``."""
    return HouseholdJobStore.from_session_factory(
        session_factory_on(connection),
        statement_timeout_milliseconds=STATEMENT_TIMEOUT_MILLISECONDS,
    )


def test_the_inputs_are_read_together(public_api_database_url) -> None:
    with rolled_back_transaction(public_api_database_url) as connection:
        catalog = insert_catalog_version(
            connection,
            country="us",
            model_id=insert_model(connection, "us"),
            policyengine_version=POLICYENGINE_VERSION,
            parameter_names=[CTC_AMOUNT],
        )
        household_id = insert_household(
            connection, country="us", default_year=2026, document=DOCUMENT
        )
        policy_id = insert_policy(
            connection,
            catalog,
            [StoredValue(CTC_AMOUNT, 3_000, datetime(2026, 1, 1, tzinfo=UTC))],
        )
        act_as_the_service(connection)
        store = _store_on(connection)

        with_policy = store.read_inputs(
            country="us",
            household_id=household_id,
            policy_id=policy_id,
            policyengine_version=POLICYENGINE_VERSION,
        )
        current_law = store.read_inputs(
            country="us",
            household_id=household_id,
            policy_id=None,
            policyengine_version=POLICYENGINE_VERSION,
        )

    assert with_policy.catalog_version_id == catalog.version_id
    assert with_policy.household.id == household_id
    assert with_policy.household.default_year == 2026
    assert with_policy.household.document == DOCUMENT
    assert with_policy.reform == {CTC_AMOUNT: {"2026-01-01.2200-12-31": 3_000}}
    assert current_law.reform == {}


def test_each_missing_input_is_named(public_api_database_url) -> None:
    with rolled_back_transaction(public_api_database_url) as connection:
        insert_catalog_version(
            connection,
            country="us",
            model_id=insert_model(connection, "us"),
            policyengine_version=POLICYENGINE_VERSION,
        )
        household_id = insert_household(
            connection, country="us", default_year=2026, document=DOCUMENT
        )
        missing_id = uuid4()
        act_as_the_service(connection)
        store = _store_on(connection)

        with pytest.raises(UnknownCatalogVersionError) as no_version:
            store.read_inputs(
                country="us",
                household_id=household_id,
                policy_id=None,
                policyengine_version="6.16.1",
            )
        with pytest.raises(UnknownHouseholdError) as no_household:
            store.read_inputs(
                country="us",
                household_id=missing_id,
                policy_id=None,
                policyengine_version=POLICYENGINE_VERSION,
            )
        with pytest.raises(UnknownPolicyError) as no_policy:
            store.read_inputs(
                country="us",
                household_id=household_id,
                policy_id=missing_id,
                policyengine_version=POLICYENGINE_VERSION,
            )

    assert str(no_version.value) == "us has no catalog version for .py 6.16.1"
    assert str(no_household.value) == f"us has no household {missing_id}"
    assert str(no_policy.value) == f"us has no policy {missing_id}"


def test_a_job_is_created_recorded_and_read_back(public_api_database_url) -> None:
    with rolled_back_transaction(public_api_database_url) as connection:
        catalog = insert_catalog_version(
            connection,
            country="uk",
            model_id=insert_model(connection, "uk"),
            policyengine_version=POLICYENGINE_VERSION,
        )
        household_id = insert_household(
            connection, country="uk", default_year=2027, document=DOCUMENT
        )
        act_as_the_service(connection)
        store = _store_on(connection)
        job_id = uuid4()

        store.create_job(
            NewHouseholdJob(
                id=job_id,
                country="uk",
                household_id=household_id,
                policy_id=None,
                year=2027,
                request_data={"country": "uk"},
                tax_benefit_model_version_id=catalog.version_id,
                country_package_version="2.174.1",
                policyengine_version=POLICYENGINE_VERSION,
            )
        )
        status = store.record_call(job_id, "fc-1")
        job = store.read(job_id)
        unknown = store.read(uuid4())

    assert status is HouseholdJobStatus.PENDING
    assert job is not None
    assert (job.status, job.modal_call_id, job.year) == (
        HouseholdJobStatus.PENDING,
        "fc-1",
        2027,
    )
    assert unknown is None


def test_a_refused_call_fails_its_job(public_api_database_url) -> None:
    with rolled_back_transaction(public_api_database_url) as connection:
        catalog = insert_catalog_version(
            connection,
            country="us",
            model_id=insert_model(connection, "us"),
            policyengine_version=POLICYENGINE_VERSION,
        )
        household_id = insert_household(
            connection, country="us", default_year=2026, document=DOCUMENT
        )
        act_as_the_service(connection)
        store = _store_on(connection)
        job_id = uuid4()
        store.create_job(
            NewHouseholdJob(
                id=job_id,
                country="us",
                household_id=household_id,
                policy_id=None,
                year=2026,
                request_data={},
                tax_benefit_model_version_id=catalog.version_id,
                country_package_version="2.2.1",
                policyengine_version=POLICYENGINE_VERSION,
            )
        )

        failed = store.fail_unstarted(job_id, "Modal refused the call.")
        job = store.read(job_id)

    assert failed is True
    assert job is not None
    assert (job.status, job.error_message) == (
        HouseholdJobStatus.FAILED,
        "Modal refused the call.",
    )


def _job_naming_no_rows() -> NewHouseholdJob:
    """Return a job whose household and catalog version are random IDs."""
    return NewHouseholdJob(
        id=uuid4(),
        country="us",
        household_id=uuid4(),
        policy_id=None,
        year=2026,
        request_data={"country": "us"},
        tax_benefit_model_version_id=uuid4(),
        country_package_version="2.2.1",
        policyengine_version=POLICYENGINE_VERSION,
    )


def test_a_job_whose_commit_fails_may_have_been_created() -> None:
    """PostgreSQL may have applied the commit before the connection failed."""
    session = RecordingSession()
    store = HouseholdJobStore(lambda: CommitFailingTransaction(session))

    with pytest.raises(EconomyRecordOutcomeUnknownError) as raised:
        store.create_job(_job_naming_no_rows())

    assert isinstance(raised.value.__cause__, OperationalError)
    assert session.added
    assert session.flushed


def test_a_job_whose_flush_fails_was_not_created() -> None:
    store = HouseholdJobStore(FlushFailingTransaction)

    with pytest.raises(OperationalError):
        store.create_job(_job_naming_no_rows())


def test_a_job_naming_missing_rows_is_refused_before_its_commit(
    public_api_database_url,
) -> None:
    """The keys refuse the insert itself, in a transaction of its own.

    A key checked only at commit would make the refusal look like a lost
    commit; this database's keys refuse the statement.
    """
    engine = create_simulation_engine(public_api_database_url)
    try:
        store = HouseholdJobStore.from_session_factory(
            create_session_factory(engine),
            statement_timeout_milliseconds=STATEMENT_TIMEOUT_MILLISECONDS,
        )

        with pytest.raises(HouseholdJobReferenceError):
            store.create_job(_job_naming_no_rows())
    finally:
        engine.dispose()
