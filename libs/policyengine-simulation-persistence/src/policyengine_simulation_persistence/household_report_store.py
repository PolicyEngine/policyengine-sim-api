"""The household report store: each read and write in a transaction of its own.

The Simulation Entrypoint creates a household report in three steps, each
its own transaction, with the household function's call started between
them: it reads the report's inputs, creates the rows, then records the call
on the report run or fails the run. A stored report's re-run takes the same
steps, creating the report's new runs instead. ``HouseholdReportStore`` runs
the reads and writes of ``household_report_records``,
``household_job_records``, ``policy_reform``, ``economy_rerun_records``'
household report re-run and ``economy_run_records``' report run writes that
way.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy.exc import SQLAlchemyError

from policyengine_simulation_persistence.economy_rerun_records import (
    insert_household_report_rerun,
)
from policyengine_simulation_persistence.economy_run_records import (
    fail_unstarted_report_run,
    read_model_id,
    record_report_run_call,
)
from policyengine_simulation_persistence.economy_run_store import (
    EconomyRecordOutcomeUnknownError,
    ReportSideError,
)
from policyengine_simulation_persistence.engine import bounded_transaction
from policyengine_simulation_persistence.household_job_records import (
    read_catalog_version_id,
    read_stored_household,
)
from policyengine_simulation_persistence.household_job_store import (
    UnknownCatalogVersionError,
    UnknownHouseholdError,
)
from policyengine_simulation_persistence.household_report_records import (
    insert_household_report,
)
from policyengine_simulation_persistence.policy_reform import (
    MissingPolicyParametersError,
    UnknownPolicyError,
    read_policy_reform,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence
    from contextlib import AbstractContextManager
    from uuid import UUID

    from sqlalchemy import Connection, Engine

    from policyengine_simulation_persistence.economy_rerun_records import (
        NewHouseholdReportRerun,
    )
    from policyengine_simulation_persistence.economy_run_store import ReportSide
    from policyengine_simulation_persistence.household_job_records import (
        StoredHousehold,
    )
    from policyengine_simulation_persistence.household_report_records import (
        NewHouseholdReport,
    )
    from policyengine_simulation_persistence.policy_reform import PolicyReform
    from policyengine_simulation_persistence.tables import ReportRunStatus

type TransactionFactory = Callable[[], AbstractContextManager[Connection]]


@dataclass(frozen=True)
class HouseholdReportInputs:
    """What a household report is read from.

    ``catalog_version_id`` is the country's catalog version for the .py
    version the report runs on, and ``tax_benefit_model_id`` its model.
    ``reforms`` holds each requested side's policy values by parameter name
    and period, empty for current law.
    """

    catalog_version_id: UUID
    tax_benefit_model_id: UUID
    household: StoredHousehold
    reforms: dict[ReportSide, PolicyReform]


def _side_reform(
    connection: Connection,
    side: ReportSide,
    *,
    country: str,
    policy_id: UUID | None,
    catalog_version_id: UUID,
) -> PolicyReform:
    """Return one side's policy values; raise ReportSideError naming the side."""
    if policy_id is None:
        return {}
    try:
        return read_policy_reform(
            connection,
            country=country,
            policy_id=policy_id,
            catalog_version_id=catalog_version_id,
        )
    except (UnknownPolicyError, MissingPolicyParametersError) as error:
        raise ReportSideError(side, error) from error


class HouseholdReportStore:
    """Read and write household reports, each operation in its own transaction."""

    def __init__(self, transaction: TransactionFactory) -> None:
        """Open each transaction with ``transaction``, which commits on success."""
        self._transaction = transaction

    @classmethod
    def from_engine(
        cls, engine: Engine, *, statement_timeout_milliseconds: int
    ) -> HouseholdReportStore:
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
        policy_ids: Mapping[ReportSide, UUID | None],
        policyengine_version: str,
    ) -> HouseholdReportInputs:
        """Read the catalog version, the household and each side's policy together.

        ``policy_ids`` names each side's stored policy, None for current law.
        Raise UnknownCatalogVersionError or UnknownHouseholdError, or
        ReportSideError naming the side whose policy is missing
        (UnknownPolicyError) or sets parameters the catalog version lacks
        (MissingPolicyParametersError).
        """
        with self._transaction() as connection:
            catalog_version_id = read_catalog_version_id(
                connection, country=country, policyengine_version=policyengine_version
            )
            model_id = read_model_id(connection, country=country)
            if catalog_version_id is None or model_id is None:
                msg = f"{country} has no catalog version for .py {policyengine_version}"
                raise UnknownCatalogVersionError(msg)
            household = read_stored_household(
                connection, country=country, household_id=household_id
            )
            if household is None:
                msg = f"{country} has no household {household_id}"
                raise UnknownHouseholdError(msg)
            reforms: dict[ReportSide, PolicyReform] = {
                side: _side_reform(
                    connection,
                    side,
                    country=country,
                    policy_id=policy_id,
                    catalog_version_id=catalog_version_id,
                )
                for side, policy_id in policy_ids.items()
            }
        return HouseholdReportInputs(
            catalog_version_id=catalog_version_id,
            tax_benefit_model_id=model_id,
            household=household,
            reforms=reforms,
        )

    def create_report(self, report: NewHouseholdReport) -> None:
        """Create the report, its simulations and its runs.

        Raise ``insert_household_report``'s errors, and
        EconomyRecordOutcomeUnknownError when the commit fails after the
        rows were inserted: they may exist.
        """
        self._create(lambda connection: insert_household_report(connection, report))

    def create_report_rerun(self, rerun: NewHouseholdReportRerun) -> None:
        """Create a stored report's new run and each side's new simulation run.

        Raise ``insert_household_report_rerun``'s errors, and
        EconomyRecordOutcomeUnknownError as ``create_report`` does.
        """
        self._create(
            lambda connection: insert_household_report_rerun(connection, rerun)
        )

    def _create(self, insert_rows: Callable[[Connection], None]) -> None:
        """Insert the rows in a transaction; tell a lost commit from a refusal."""
        inserted = False
        try:
            with self._transaction() as connection:
                insert_rows(connection)
                inserted = True
        except SQLAlchemyError as error:
            if not inserted:
                raise
            msg = "the create's commit failed after its rows were inserted"
            raise EconomyRecordOutcomeUnknownError(msg) from error

    def record_report_call(
        self, report_run_id: UUID, modal_call_id: str
    ) -> ReportRunStatus | None:
        """Record the report run's call ID while it is pending with none."""
        with self._transaction() as connection:
            return record_report_run_call(
                connection, report_run_id=report_run_id, modal_call_id=modal_call_id
            )

    def fail_unstarted_report(
        self,
        report_run_id: UUID,
        simulation_run_ids: Sequence[UUID],
        error_message: str,
    ) -> bool:
        """Fail the report run, and its simulation runs, while it is unstarted."""
        with self._transaction() as connection:
            return fail_unstarted_report_run(
                connection,
                report_run_id=report_run_id,
                simulation_run_ids=simulation_run_ids,
                error_message=error_message,
            )
