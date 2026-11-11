"""The simulation service's reads and writes for a household calculation.

These run against a database the Public API's own v2 migration chain built
(``PUBLIC_API_SCHEMA_DATABASE_URL``), as the simulation service's role, each
in a transaction rolled back afterwards. The Public API's rows a job names are
inserted first, as the superuser.
"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from conftest import act_as_the_service, rolled_back_transaction
from policyengine_simulation_persistence import (
    HouseholdJobReferenceError,
    HouseholdJobStatus,
    NewHouseholdJob,
    fail_unstarted_household_job,
    household_jobs,
    insert_household_job,
    read_catalog_version_id,
    read_household_job,
    read_stored_household,
    record_household_call,
)
from public_api_rows import (
    CatalogVersion,
    insert_catalog_version,
    insert_household,
    insert_model,
    insert_policy,
)
from sqlalchemy import text, update

if TYPE_CHECKING:
    from uuid import UUID

    from sqlalchemy import Connection

pytestmark = pytest.mark.public_api_schema

POLICYENGINE_VERSION = "6.17.1"
COUNTRY_PACKAGE_VERSION = "2.2.1"
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


def _catalog(connection: Connection, country: str = "us") -> CatalogVersion:
    return insert_catalog_version(
        connection,
        country=country,
        model_id=insert_model(connection, country),
        policyengine_version=POLICYENGINE_VERSION,
    )


def _new_job(
    catalog: CatalogVersion,
    household_id: UUID,
    *,
    policy_id: UUID | None = None,
) -> NewHouseholdJob:
    return NewHouseholdJob(
        id=uuid4(),
        country=catalog.country,
        household_id=household_id,
        policy_id=policy_id,
        year=2026,
        request_data={"country": catalog.country, "household_id": str(household_id)},
        tax_benefit_model_version_id=catalog.version_id,
        country_package_version=COUNTRY_PACKAGE_VERSION,
        policyengine_version=POLICYENGINE_VERSION,
    )


def _stored_job(connection: Connection) -> NewHouseholdJob:
    """Insert, as the superuser, the rows a job names; return a job naming them."""
    catalog = _catalog(connection)
    household_id = insert_household(
        connection, country="us", default_year=2026, document=DOCUMENT
    )
    return _new_job(catalog, household_id, policy_id=insert_policy(connection, catalog))


def test_a_catalog_version_is_found_by_country_and_policyengine_version(
    public_api_database_url,
) -> None:
    with rolled_back_transaction(public_api_database_url) as connection:
        catalog = _catalog(connection)
        _catalog(connection, "uk")
        act_as_the_service(connection)

        assert (
            read_catalog_version_id(
                connection, country="us", policyengine_version=POLICYENGINE_VERSION
            )
            == catalog.version_id
        )
        assert (
            read_catalog_version_id(
                connection, country="us", policyengine_version="6.16.1"
            )
            is None
        )


def test_a_household_is_found_in_its_own_country_only(public_api_database_url) -> None:
    with rolled_back_transaction(public_api_database_url) as connection:
        household_id = insert_household(
            connection, country="us", default_year=2026, document=DOCUMENT
        )
        act_as_the_service(connection)

        stored = read_stored_household(
            connection, country="us", household_id=household_id
        )
        assert (
            read_stored_household(connection, country="uk", household_id=household_id)
            is None
        )
        assert (
            read_stored_household(connection, country="us", household_id=uuid4())
            is None
        )

    assert stored is not None
    assert (stored.id, stored.country, stored.default_year) == (
        household_id,
        "us",
        2026,
    )
    assert stored.document == DOCUMENT


def test_a_job_is_created_pending_with_no_call(public_api_database_url) -> None:
    with rolled_back_transaction(public_api_database_url) as connection:
        new_job = _stored_job(connection)
        act_as_the_service(connection)

        insert_household_job(connection, new_job)
        job = read_household_job(connection, new_job.id)
        dynamic_id = connection.execute(
            text("SELECT dynamic_id FROM public.household_jobs WHERE id = :id"),
            {"id": new_job.id},
        ).scalar_one()

    assert job is not None
    assert job.status is HouseholdJobStatus.PENDING
    assert job.modal_call_id is None
    assert dynamic_id is None
    assert (
        job.id,
        job.country,
        job.household_id,
        job.policy_id,
        job.year,
        job.tax_benefit_model_version_id,
        job.country_package_version,
        job.policyengine_version,
        job.request_data,
    ) == (
        new_job.id,
        "us",
        new_job.household_id,
        new_job.policy_id,
        2026,
        new_job.tax_benefit_model_version_id,
        COUNTRY_PACKAGE_VERSION,
        POLICYENGINE_VERSION,
        new_job.request_data,
    )
    assert (job.result, job.error_message, job.started_at, job.completed_at) == (
        None,
        None,
        None,
        None,
    )


def test_an_unknown_job_reads_as_none(public_api_database_url) -> None:
    with rolled_back_transaction(public_api_database_url) as connection:
        act_as_the_service(connection)

        assert read_household_job(connection, uuid4()) is None


def test_a_call_is_recorded_once_while_the_job_is_pending_without_one(
    public_api_database_url,
) -> None:
    with rolled_back_transaction(public_api_database_url) as connection:
        new_job = _stored_job(connection)
        act_as_the_service(connection)
        insert_household_job(connection, new_job)

        first = record_household_call(
            connection, job_id=new_job.id, modal_call_id="fc-first"
        )
        second = record_household_call(
            connection, job_id=new_job.id, modal_call_id="fc-second"
        )
        job = read_household_job(connection, new_job.id)

    assert (first, second) == (HouseholdJobStatus.PENDING, HouseholdJobStatus.PENDING)
    assert job is not None
    assert job.modal_call_id == "fc-first"


def test_a_job_the_function_already_claimed_keeps_its_claim(
    public_api_database_url,
) -> None:
    """The household function may claim the job before its call ID is recorded."""
    with rolled_back_transaction(public_api_database_url) as connection:
        new_job = _stored_job(connection)
        act_as_the_service(connection)
        insert_household_job(connection, new_job)
        connection.execute(text("RESET ROLE"))
        connection.execute(
            update(household_jobs)
            .where(household_jobs.c.id == new_job.id)
            .values(status=HouseholdJobStatus.RUNNING, modal_call_id="fc-claimed")
        )
        act_as_the_service(connection)

        status = record_household_call(
            connection, job_id=new_job.id, modal_call_id="fc-claimed"
        )
        failed = fail_unstarted_household_job(
            connection, job_id=new_job.id, error_message="Modal refused the call."
        )
        job = read_household_job(connection, new_job.id)

    assert status is HouseholdJobStatus.RUNNING
    assert failed is False
    assert job is not None
    assert (job.status, job.modal_call_id, job.error_message) == (
        HouseholdJobStatus.RUNNING,
        "fc-claimed",
        None,
    )


def test_a_job_whose_call_was_refused_fails_at_once(public_api_database_url) -> None:
    with rolled_back_transaction(public_api_database_url) as connection:
        new_job = _stored_job(connection)
        act_as_the_service(connection)
        insert_household_job(connection, new_job)

        failed = fail_unstarted_household_job(
            connection, job_id=new_job.id, error_message="Modal refused the call."
        )
        failed_again = fail_unstarted_household_job(
            connection, job_id=new_job.id, error_message="A second refusal."
        )
        job = read_household_job(connection, new_job.id)

    assert (failed, failed_again) == (True, False)
    assert job is not None
    assert job.status is HouseholdJobStatus.FAILED
    assert job.error_message == "Modal refused the call."
    assert job.completed_at is not None
    assert job.modal_call_id is None


def test_a_job_whose_call_was_recorded_is_not_failed_as_refused(
    public_api_database_url,
) -> None:
    with rolled_back_transaction(public_api_database_url) as connection:
        new_job = _stored_job(connection)
        act_as_the_service(connection)
        insert_household_job(connection, new_job)
        record_household_call(connection, job_id=new_job.id, modal_call_id="fc-1")

        failed = fail_unstarted_household_job(
            connection, job_id=new_job.id, error_message="Modal refused the call."
        )
        job = read_household_job(connection, new_job.id)

    assert failed is False
    assert job is not None
    assert job.status is HouseholdJobStatus.PENDING


@pytest.mark.parametrize(
    "missing_column",
    ["household_id", "policy_id", "tax_benefit_model_version_id"],
)
def test_a_job_naming_a_row_that_does_not_exist_names_its_column(
    public_api_database_url, missing_column
) -> None:
    """The database refuses the row with SQLSTATE 23503, which names the key."""
    with rolled_back_transaction(public_api_database_url) as connection:
        stored = _stored_job(connection)
        new_job = replace(stored, **{missing_column: uuid4()})
        act_as_the_service(connection)

        with (
            pytest.raises(HouseholdJobReferenceError) as raised,
            connection.begin_nested(),
        ):
            insert_household_job(connection, new_job)

    assert raised.value.column_name == missing_column
