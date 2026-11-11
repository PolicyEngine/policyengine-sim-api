"""The simulation service's reads and writes for a household calculation.

A household calculation is one ``household_jobs`` row, which the service
creates and a household function on Modal calculates. The service reads the
stored household and the catalog version from the Public API's tables, creates
the job ``pending`` with no Modal call ID, starts the call, then records the
call's ID, or fails the job at once when Modal refuses the call. Both updates
apply only while the job is still ``pending`` with no call ID: the household
function may already have claimed it, and the stuck-run sweep fails a pending
job no call was recorded for. The household function writes the rest:
``running``, then the result and ``succeeded`` or ``failed``.

Every function takes a connection, so the caller chooses the transactions.
Each update sets only the columns the service's role may update, with
``updated_at``, whose automatic update the Public API's models make on their
own side only.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from sqlalchemy import func, insert, select, update
from sqlalchemy.exc import IntegrityError

from policyengine_simulation_persistence.input_tables import (
    MODEL_NAME_PREFIX,
    households,
    tax_benefit_model_versions,
    tax_benefit_models,
)
from policyengine_simulation_persistence.tables import (
    HouseholdJobStatus,
    household_jobs,
)

if TYPE_CHECKING:
    from datetime import datetime
    from uuid import UUID

    from sqlalchemy import ColumnElement, Connection

# PostgreSQL's SQLSTATE for a row naming one that does not exist.
FOREIGN_KEY_VIOLATION = "23503"
# Each of the job's foreign keys, by name, and the column it constrains.
_KEY_COLUMNS = {
    str(constraint.name): constraint.column_keys[0]
    for constraint in household_jobs.foreign_key_constraints
}


@dataclass(frozen=True)
class StoredHousehold:
    """A household as the Public API stores it.

    ``document`` lists the people and each group entity as records with an
    ``id`` and ``values``, each person naming its groups, and may hold a saved
    SPM selection (``spm``). ``default_year`` is the year it describes, or
    None for a household saved without one.
    """

    id: UUID
    country: str
    default_year: int | None
    document: dict[str, Any]


@dataclass(frozen=True)
class NewHouseholdJob:
    """A household job to create, ``pending`` and with no Modal call yet.

    ``request_data`` holds the request as the service received it.
    """

    id: UUID
    country: str
    household_id: UUID
    policy_id: UUID | None
    year: int
    request_data: dict[str, Any]
    tax_benefit_model_version_id: UUID
    country_package_version: str
    policyengine_version: str


@dataclass(frozen=True)
class HouseholdJobRecord:
    """A household job as it stands.

    ``result`` is the household function's outcome document, set once it
    has finished: the calculated household, or the failure with its reason.
    ``error_message`` summarizes a failure in one line.
    """

    id: UUID
    status: HouseholdJobStatus
    country: str
    household_id: UUID
    policy_id: UUID | None
    year: int
    request_data: dict[str, Any]
    tax_benefit_model_version_id: UUID
    country_package_version: str
    policyengine_version: str
    modal_call_id: str | None
    result: dict[str, Any] | None
    error_message: str | None
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None


class HouseholdJobReferenceError(LookupError):
    """The database refused a job naming a row that does not exist.

    ``column_name`` is the job's column whose foreign key refused it, or None
    when the database named a key the job's table doesn't have.
    """

    def __init__(self, column_name: str | None) -> None:
        """Keep the column whose key refused the job."""
        self.column_name = column_name
        super().__init__(
            f"the household job's {column_name or 'reference'} names no row"
        )


def read_catalog_version_id(
    connection: Connection, *, country: str, policyengine_version: str
) -> UUID | None:
    """Return the id of the country's catalog version for a .py version, or None."""
    return connection.execute(
        select(tax_benefit_model_versions.c.id)
        .join(
            tax_benefit_models,
            tax_benefit_models.c.id == tax_benefit_model_versions.c.model_id,
        )
        .where(
            tax_benefit_models.c.name == f"{MODEL_NAME_PREFIX}{country}",
            tax_benefit_model_versions.c.version == policyengine_version,
        )
    ).scalar_one_or_none()


def read_stored_household(
    connection: Connection, *, country: str, household_id: UUID
) -> StoredHousehold | None:
    """Return the country's stored household, or None when it has no such one."""
    row = connection.execute(
        select(households.c.default_year, households.c.household_data).where(
            households.c.id == household_id, households.c.country_id == country
        )
    ).one_or_none()
    if row is None:
        return None
    return StoredHousehold(
        id=household_id,
        country=country,
        default_year=row.default_year,
        document=row.household_data,
    )


def insert_household_job(connection: Connection, job: NewHouseholdJob) -> None:
    """Create the job ``pending``, with no Modal call ID and no dynamic.

    Raise HouseholdJobReferenceError when the database refuses a row it
    names. The refusal ends the caller's transaction, or its savepoint.
    """
    try:
        connection.execute(
            insert(household_jobs).values(
                id=job.id,
                country=job.country,
                household_id=job.household_id,
                policy_id=job.policy_id,
                dynamic_id=None,
                year=job.year,
                request_data=job.request_data,
                tax_benefit_model_version_id=job.tax_benefit_model_version_id,
                country_package_version=job.country_package_version,
                policyengine_version=job.policyengine_version,
                status=HouseholdJobStatus.PENDING,
                modal_call_id=None,
            )
        )
    except IntegrityError as error:
        driver_error = error.orig
        if getattr(driver_error, "sqlstate", None) != FOREIGN_KEY_VIOLATION:
            raise
        constraint_name = getattr(
            getattr(driver_error, "diag", None), "constraint_name", None
        )
        raise HouseholdJobReferenceError(
            _KEY_COLUMNS.get(str(constraint_name))
        ) from None


def _pending_without_call(job_id: UUID) -> tuple[ColumnElement[bool], ...]:
    return (
        household_jobs.c.id == job_id,
        household_jobs.c.status == HouseholdJobStatus.PENDING,
        household_jobs.c.modal_call_id.is_(None),
    )


def record_household_call(
    connection: Connection, *, job_id: UUID, modal_call_id: str
) -> HouseholdJobStatus | None:
    """Record the job's Modal call ID while it is pending with none.

    Return the job's status afterwards, or None when there is no such job. A
    job the household function has claimed already keeps its claim.
    """
    recorded = connection.execute(
        update(household_jobs)
        .where(*_pending_without_call(job_id))
        .values(modal_call_id=modal_call_id, updated_at=func.now())
        .returning(household_jobs.c.status)
    ).scalar_one_or_none()
    if recorded is not None:
        return HouseholdJobStatus(recorded)
    status = connection.execute(
        select(household_jobs.c.status).where(household_jobs.c.id == job_id)
    ).scalar_one_or_none()
    return None if status is None else HouseholdJobStatus(status)


def fail_unstarted_household_job(
    connection: Connection, *, job_id: UUID, error_message: str
) -> bool:
    """Fail the job at once while it is pending with no Modal call ID.

    Return whether it was failed: a job a call has been recorded for, or one
    the household function has claimed, is left as it is.
    """
    failed = connection.execute(
        update(household_jobs)
        .where(*_pending_without_call(job_id))
        .values(
            status=HouseholdJobStatus.FAILED,
            error_message=error_message,
            completed_at=func.now(),
            updated_at=func.now(),
        )
        .returning(household_jobs.c.id)
    ).scalar_one_or_none()
    return failed is not None


def read_household_job(
    connection: Connection, job_id: UUID
) -> HouseholdJobRecord | None:
    """Return the household job as it stands, or None when there is no such job."""
    row = connection.execute(
        select(
            household_jobs.c.id,
            household_jobs.c.status,
            household_jobs.c.country,
            household_jobs.c.household_id,
            household_jobs.c.policy_id,
            household_jobs.c.year,
            household_jobs.c.request_data,
            household_jobs.c.tax_benefit_model_version_id,
            household_jobs.c.country_package_version,
            household_jobs.c.policyengine_version,
            household_jobs.c.modal_call_id,
            household_jobs.c.result,
            household_jobs.c.error_message,
            household_jobs.c.created_at,
            household_jobs.c.started_at,
            household_jobs.c.completed_at,
        ).where(household_jobs.c.id == job_id)
    ).one_or_none()
    if row is None:
        return None
    return HouseholdJobRecord(
        id=row.id,
        status=HouseholdJobStatus(row.status),
        country=row.country,
        household_id=row.household_id,
        policy_id=row.policy_id,
        year=row.year,
        request_data=row.request_data,
        tax_benefit_model_version_id=row.tax_benefit_model_version_id,
        country_package_version=row.country_package_version,
        policyengine_version=row.policyengine_version,
        modal_call_id=row.modal_call_id,
        result=row.result,
        error_message=row.error_message,
        created_at=row.created_at,
        started_at=row.started_at,
        completed_at=row.completed_at,
    )
