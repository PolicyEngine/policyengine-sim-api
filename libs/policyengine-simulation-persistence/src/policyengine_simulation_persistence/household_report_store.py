"""The household report store: each read and write in a transaction of its own.

The Simulation Entrypoint creates a household report in three steps, each
its own transaction, with the household function's call started between
them: it reads the report's inputs, finds or creates the rows by key, then
records the call on a report run it created or fails the run. A stored
report's re-run takes the same steps, joining or creating the report's new
runs instead. ``HouseholdReportStore`` runs the reads and writes of
``keyed_household_records``, ``household_job_records``, ``policy_reform``,
``economy_rerun_records``' household report re-run and
``economy_run_records``' report run writes that way, each in an ORM session
of its own.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from policyengine_simulation_persistence.economy_inputs import SIDE_ERRORS
from policyengine_simulation_persistence.economy_rerun_records import (
    insert_household_report_rerun,
)
from policyengine_simulation_persistence.economy_run_records import (
    fail_unstarted_report_run,
    read_model_id,
    record_report_run_call,
)
from policyengine_simulation_persistence.economy_run_store import ReportSideError
from policyengine_simulation_persistence.household_job_records import (
    read_catalog_version_id,
    read_stored_household,
)
from policyengine_simulation_persistence.household_job_store import (
    UnknownCatalogVersionError,
    UnknownHouseholdError,
)
from policyengine_simulation_persistence.keyed_household_records import (
    find_or_create_household_report,
)
from policyengine_simulation_persistence.policy_reform import read_simulation_reform
from policyengine_simulation_persistence.store_transactions import (
    TransactionFactory,
    bounded_transactions,
    create_in_transaction,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence
    from uuid import UUID

    from sqlalchemy.orm import Session, sessionmaker

    from policyengine_simulation_persistence.economy_rerun_records import (
        NewHouseholdReportRerun,
    )
    from policyengine_simulation_persistence.economy_run_records import (
        SimulationChoice,
    )
    from policyengine_simulation_persistence.economy_run_store import ReportSide
    from policyengine_simulation_persistence.household_job_records import (
        StoredHousehold,
    )
    from policyengine_simulation_persistence.keyed_household_records import (
        FoundHouseholdReport,
        KeyedHouseholdReport,
    )
    from policyengine_simulation_persistence.policy_reform import SimulationReform
    from policyengine_simulation_persistence.shared_rows import FoundReportRun
    from policyengine_simulation_persistence.tables import ReportRunStatus


@dataclass(frozen=True)
class HouseholdReportInputs:
    """What a household report is read from.

    ``catalog_version_id`` is the country's catalog version for the .py
    version the report runs on, and ``tax_benefit_model_id`` its model.
    ``reforms`` holds each requested side's policy and dynamic values
    together, with the parameters its dynamic sets.
    """

    catalog_version_id: UUID
    tax_benefit_model_id: UUID
    household: StoredHousehold
    reforms: dict[ReportSide, SimulationReform]


def _side_reform(
    session: Session,
    side: ReportSide,
    *,
    country: str,
    choice: SimulationChoice,
    catalog_version_id: UUID,
) -> SimulationReform:
    """Return one side's policy and dynamic values; raise ReportSideError naming it."""
    try:
        return read_simulation_reform(
            session,
            country=country,
            policy_id=choice.policy_id,
            dynamic_id=choice.dynamic_id,
            catalog_version_id=catalog_version_id,
        )
    except SIDE_ERRORS as error:
        raise ReportSideError(side, error) from error


class HouseholdReportStore:
    """Read and write household reports, each operation in its own transaction."""

    def __init__(self, transaction: TransactionFactory) -> None:
        """Open each session with ``transaction``, which commits on success."""
        self._transaction = transaction

    @classmethod
    def from_session_factory(
        cls,
        session_factory: sessionmaker[Session],
        *,
        statement_timeout_milliseconds: int,
    ) -> HouseholdReportStore:
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
        choices: Mapping[ReportSide, SimulationChoice],
        policyengine_version: str,
    ) -> HouseholdReportInputs:
        """Read the catalog version, household and each side's values together.

        ``choices`` names each side's stored policy and dynamic, None for
        none, whose values are combined as an economy simulation's are
        (``read_simulation_reform``). Raise UnknownCatalogVersionError or
        UnknownHouseholdError, or ReportSideError naming the side whose
        policy or dynamic is missing in the country (UnknownPolicyError,
        UnknownDynamicError), sets parameters the catalog version lacks
        (MissingPolicyParametersError) or sets one the other also sets
        (OverlappingReformError).
        """
        with self._transaction() as session:
            catalog_version_id = read_catalog_version_id(
                session, country=country, policyengine_version=policyengine_version
            )
            model_id = read_model_id(session, country=country)
            if catalog_version_id is None or model_id is None:
                msg = f"{country} has no catalog version for .py {policyengine_version}"
                raise UnknownCatalogVersionError(msg)
            household = read_stored_household(
                session, country=country, household_id=household_id
            )
            if household is None:
                msg = f"{country} has no household {household_id}"
                raise UnknownHouseholdError(msg)
            reforms: dict[ReportSide, SimulationReform] = {
                side: _side_reform(
                    session,
                    side,
                    country=country,
                    choice=choice,
                    catalog_version_id=catalog_version_id,
                )
                for side, choice in choices.items()
            }
        return HouseholdReportInputs(
            catalog_version_id=catalog_version_id,
            tax_benefit_model_id=model_id,
            household=household,
            reforms=reforms,
        )

    def find_or_create_report(
        self, report: KeyedHouseholdReport
    ) -> FoundHouseholdReport:
        """Find the report by key, with its run, or create them.

        See ``find_or_create_household_report``; raise its errors, and
        EconomyRecordOutcomeUnknownError when the commit fails after the
        rows were inserted.
        """
        return self._create(
            lambda session: find_or_create_household_report(session, report)
        )

    def create_report_rerun(self, rerun: NewHouseholdReportRerun) -> FoundReportRun:
        """Join a stored report's run on the re-run's versions, or create it.

        See ``insert_household_report_rerun``; raise its errors, and
        EconomyRecordOutcomeUnknownError as ``find_or_create_report`` does.
        """
        return self._create(
            lambda session: insert_household_report_rerun(session, rerun)
        )

    def _create[Created](self, insert_rows: Callable[[Session], Created]) -> Created:
        """Insert the rows in a transaction; tell a lost commit from a refusal."""
        return create_in_transaction(
            self._transaction,
            insert_rows,
            commit_failure="the create's commit failed after its rows were inserted",
        )

    def record_report_call(
        self, report_run_id: UUID, modal_call_id: str
    ) -> ReportRunStatus | None:
        """Record the report run's call ID while it is pending with none."""
        with self._transaction() as session:
            return record_report_run_call(
                session, report_run_id=report_run_id, modal_call_id=modal_call_id
            )

    def fail_unstarted_report(
        self,
        report_run_id: UUID,
        simulation_run_ids: Sequence[UUID],
        error_message: str,
    ) -> bool:
        """Fail the report run, and its simulation runs, while it is unstarted."""
        with self._transaction() as session:
            return fail_unstarted_report_run(
                session,
                report_run_id=report_run_id,
                simulation_run_ids=simulation_run_ids,
                error_message=error_message,
            )
