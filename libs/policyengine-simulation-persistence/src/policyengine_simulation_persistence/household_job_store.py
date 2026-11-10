"""The household calculation store: each read and write in a transaction of its own.

The Simulation Entrypoint creates a household job in three steps, each its
own transaction, with the Modal call started between them: it reads the
inputs, creates the job, then records the call or fails the job. Later reads
return the job as it stands. ``HouseholdJobStore`` runs the reads and writes
of ``household_job_records`` and ``policy_reform`` that way.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from policyengine_simulation_persistence.engine import bounded_transaction
from policyengine_simulation_persistence.household_job_records import (
    fail_unstarted_household_job,
    insert_household_job,
    read_catalog_version_id,
    read_household_job,
    read_stored_household,
    record_household_call,
)
from policyengine_simulation_persistence.policy_reform import read_policy_reform

if TYPE_CHECKING:
    from collections.abc import Callable
    from contextlib import AbstractContextManager
    from uuid import UUID

    from sqlalchemy import Connection, Engine

    from policyengine_simulation_persistence.household_job_records import (
        HouseholdJobRecord,
        NewHouseholdJob,
        StoredHousehold,
    )
    from policyengine_simulation_persistence.policy_reform import PolicyReform
    from policyengine_simulation_persistence.tables import HouseholdJobStatus

type TransactionFactory = Callable[[], AbstractContextManager[Connection]]


class UnknownCatalogVersionError(LookupError):
    """The country has no catalog version for the .py version."""


class UnknownHouseholdError(LookupError):
    """The Public API has no such household in the country."""


@dataclass(frozen=True)
class HouseholdCalculationInputs:
    """What a household calculation is read from.

    ``catalog_version_id`` is the country's catalog version for the .py
    version the calculation runs on; ``reform`` holds the policy's values by
    parameter name and period, empty for current law.
    """

    catalog_version_id: UUID
    household: StoredHousehold
    reform: PolicyReform


class HouseholdJobStore:
    """Read and write household jobs, each operation in its own transaction."""

    def __init__(self, transaction: TransactionFactory) -> None:
        """Open each transaction with ``transaction``, which commits on success."""
        self._transaction = transaction

    @classmethod
    def from_engine(
        cls, engine: Engine, *, statement_timeout_milliseconds: int
    ) -> HouseholdJobStore:
        """Use ``engine``, bounding each statement to the given time."""

        def transaction() -> AbstractContextManager[Connection]:
            return bounded_transaction(
                engine, statement_timeout_milliseconds=statement_timeout_milliseconds
            )

        return cls(transaction)

    def read_inputs(
        self,
        *,
        country: str,
        household_id: UUID,
        policy_id: UUID | None,
        policyengine_version: str,
    ) -> HouseholdCalculationInputs:
        """Read the catalog version, household and policy in one transaction.

        Raise UnknownCatalogVersionError, UnknownHouseholdError, or the
        policy reader's UnknownPolicyError or MissingPolicyParametersError.
        """
        with self._transaction() as connection:
            catalog_version_id = read_catalog_version_id(
                connection, country=country, policyengine_version=policyengine_version
            )
            if catalog_version_id is None:
                msg = f"{country} has no catalog version for .py {policyengine_version}"
                raise UnknownCatalogVersionError(msg)
            household = read_stored_household(
                connection, country=country, household_id=household_id
            )
            if household is None:
                msg = f"{country} has no household {household_id}"
                raise UnknownHouseholdError(msg)
            reform = (
                {}
                if policy_id is None
                else read_policy_reform(
                    connection,
                    country=country,
                    policy_id=policy_id,
                    catalog_version_id=catalog_version_id,
                )
            )
        return HouseholdCalculationInputs(
            catalog_version_id=catalog_version_id, household=household, reform=reform
        )

    def create_job(self, job: NewHouseholdJob) -> None:
        """Create the job ``pending`` with no call; see ``insert_household_job``."""
        with self._transaction() as connection:
            insert_household_job(connection, job)

    def record_call(
        self, job_id: UUID, modal_call_id: str
    ) -> HouseholdJobStatus | None:
        """Record the job's call ID while it is pending with none; return its status."""
        with self._transaction() as connection:
            return record_household_call(
                connection, job_id=job_id, modal_call_id=modal_call_id
            )

    def fail_unstarted(self, job_id: UUID, error_message: str) -> bool:
        """Fail the job while it is pending with no call ID; return whether it was."""
        with self._transaction() as connection:
            return fail_unstarted_household_job(
                connection, job_id=job_id, error_message=error_message
            )

    def read(self, job_id: UUID) -> HouseholdJobRecord | None:
        """Return the job as it stands, or None when there is no such job."""
        with self._transaction() as connection:
            return read_household_job(connection, job_id)
