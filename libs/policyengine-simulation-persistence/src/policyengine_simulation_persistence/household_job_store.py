"""The household calculation store: each read and write in a transaction of its own.

The Simulation Entrypoint creates a household job in three steps, each its
own transaction, with the Modal call started between them: it reads the
inputs, creates the job, then records the call or fails the job. Later reads
return the job as it stands. ``HouseholdJobStore`` runs the reads and writes
of ``household_job_records`` and ``policy_reform`` that way, each in an ORM
session of its own.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from policyengine_simulation_persistence.household_job_records import (
    fail_unstarted_household_job,
    insert_household_job,
    read_catalog_version_id,
    read_household_job,
    read_stored_household,
    record_household_call,
)
from policyengine_simulation_persistence.policy_reform import read_simulation_reform
from policyengine_simulation_persistence.store_transactions import (
    TransactionFactory,
    bounded_transactions,
    create_in_transaction,
)

if TYPE_CHECKING:
    from uuid import UUID

    from sqlalchemy.orm import Session, sessionmaker

    from policyengine_simulation_persistence.household_job_records import (
        HouseholdJobRecord,
        NewHouseholdJob,
        StoredHousehold,
    )
    from policyengine_simulation_persistence.policy_reform import PolicyReform
    from policyengine_simulation_persistence.tables import HouseholdJobStatus


class UnknownCatalogVersionError(LookupError):
    """The country has no catalog version for the .py version."""


class UnknownHouseholdError(LookupError):
    """API v1 has no such household in the country."""


@dataclass(frozen=True)
class HouseholdCalculationInputs:
    """What a household calculation is read from.

    ``catalog_version_id`` is the country's catalog version for the .py
    version the calculation runs on. ``reform`` holds the policy's and the
    dynamic's values together, by parameter name and period, empty for
    current law with no dynamic; ``dynamic_parameters`` names those the
    dynamic sets.
    """

    catalog_version_id: UUID
    household: StoredHousehold
    reform: PolicyReform
    dynamic_parameters: frozenset[str] = frozenset()


class HouseholdJobStore:
    """Read and write household jobs, each operation in its own transaction."""

    def __init__(self, transaction: TransactionFactory) -> None:
        """Open each session with ``transaction``, which commits on success."""
        self._transaction = transaction

    @classmethod
    def from_session_factory(
        cls,
        session_factory: sessionmaker[Session],
        *,
        statement_timeout_milliseconds: int,
    ) -> HouseholdJobStore:
        """Use ``session_factory``, bounding each statement to the given time."""
        return cls(
            bounded_transactions(
                session_factory,
                statement_timeout_milliseconds=statement_timeout_milliseconds,
            )
        )

    def read_inputs(
        self,
        *,
        country: str,
        household_id: UUID,
        policy_id: UUID | None,
        dynamic_id: UUID | None,
        policyengine_version: str,
    ) -> HouseholdCalculationInputs:
        """Read the catalog version, household, policy and dynamic together.

        The policy's and the dynamic's values are combined, as an economy
        simulation's are (``read_simulation_reform``). Raise
        UnknownCatalogVersionError, UnknownHouseholdError, or the readers'
        UnknownPolicyError, UnknownDynamicError (a dynamic of another
        country included), MissingPolicyParametersError or
        OverlappingReformError.
        """
        with self._transaction() as session:
            catalog_version_id = read_catalog_version_id(
                session, country=country, policyengine_version=policyengine_version
            )
            if catalog_version_id is None:
                msg = f"{country} has no catalog version for .py {policyengine_version}"
                raise UnknownCatalogVersionError(msg)
            household = read_stored_household(
                session, country=country, household_id=household_id
            )
            if household is None:
                msg = f"{country} has no household {household_id}"
                raise UnknownHouseholdError(msg)
            reform = read_simulation_reform(
                session,
                country=country,
                policy_id=policy_id,
                dynamic_id=dynamic_id,
                catalog_version_id=catalog_version_id,
            )
        return HouseholdCalculationInputs(
            catalog_version_id=catalog_version_id,
            household=household,
            reform=reform.values,
            dynamic_parameters=reform.dynamic_parameters,
        )

    def create_job(self, job: NewHouseholdJob) -> None:
        """Create the job ``pending`` with no call; see ``insert_household_job``.

        An error while the row is flushed leaves nothing written and
        propagates, a refused reference as HouseholdJobReferenceError. An
        error once it is flushed is the commit's, after which the job may
        exist: EconomyRecordOutcomeUnknownError, caused by the commit's error.
        """
        create_in_transaction(
            self._transaction,
            lambda session: insert_household_job(session, job),
            commit_failure=(
                "the household job's commit failed after its row was inserted"
            ),
        )

    def record_call(
        self, job_id: UUID, modal_call_id: str
    ) -> HouseholdJobStatus | None:
        """Record the job's call ID while it is pending with none; return its status."""
        with self._transaction() as session:
            return record_household_call(
                session, job_id=job_id, modal_call_id=modal_call_id
            )

    def fail_unstarted(self, job_id: UUID, error_message: str) -> bool:
        """Fail the job while it is pending with no call ID; return whether it was."""
        with self._transaction() as session:
            return fail_unstarted_household_job(
                session, job_id=job_id, error_message=error_message
            )

    def read(self, job_id: UUID) -> HouseholdJobRecord | None:
        """Return the job as it stands, or None when there is no such job."""
        with self._transaction() as session:
            return read_household_job(session, job_id)
