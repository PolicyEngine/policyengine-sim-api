"""The economy store: each read and write in a transaction of its own.

The Simulation Entrypoint creates an economy report, or a standalone economy
simulation, in three steps, each its own transaction, with the Modal call
started between them: it reads the request's inputs, finds or creates the
rows by key, then records the call on a run it created or fails that run.
A stored record's re-run takes the same steps, joining or creating its new
runs instead. ``EconomyRunStore`` runs the reads and writes of
``economy_inputs``, ``keyed_economy_records``, ``keyed_window_records``,
``economy_rerun_records`` and ``economy_run_records`` that way, each in an ORM
session of its own.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from policyengine_simulation_persistence.economy_inputs import (
    BudgetWindowInputs,
    EconomyCatalogRows,
    EconomyReportInputs,
    EconomyRequest,
    EconomySimulationInputs,
    LocalAreaDatasetMismatchError,
    ReportSide,
    ReportSideError,
    SideRefusal,
    SimulationInputs,
    UnknownDatasetError,
    UnknownRegionError,
    read_catalog_rows,
    read_report_sides,
    read_simulation_values,
    read_window_sides,
)
from policyengine_simulation_persistence.economy_rerun_records import (
    insert_report_rerun,
    insert_simulation_rerun,
    insert_window_rerun,
    read_report_definition,
    read_simulation_definition,
)
from policyengine_simulation_persistence.economy_run_reads import (
    read_economy_report,
    read_economy_simulation,
)
from policyengine_simulation_persistence.economy_run_records import (
    SimulationChoice,
    fail_unstarted_report_run,
    fail_unstarted_simulation_run,
    record_report_run_call,
    record_simulation_run_call,
)
from policyengine_simulation_persistence.keyed_economy_records import (
    find_or_create_economy_report,
    find_or_create_economy_simulation,
)
from policyengine_simulation_persistence.keyed_window_records import (
    find_or_create_budget_window,
)
from policyengine_simulation_persistence.store_transactions import (
    EconomyRecordOutcomeUnknownError,
    TransactionFactory,
    bounded_transactions,
    create_in_transaction,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from uuid import UUID

    from sqlalchemy.orm import Session, sessionmaker

    from policyengine_simulation_persistence.economy_rerun_records import (
        NewReportRerun,
        NewSimulationRerun,
        NewWindowRerun,
        ReportDefinition,
        SideDefinition,
        SimulationDefinition,
    )
    from policyengine_simulation_persistence.economy_run_reads import (
        EconomyReportState,
        EconomySimulationState,
    )
    from policyengine_simulation_persistence.keyed_economy_records import (
        FoundEconomyReport,
        FoundEconomySimulation,
        KeyedEconomyReport,
        KeyedEconomySimulation,
    )
    from policyengine_simulation_persistence.keyed_window_records import (
        FoundBudgetWindow,
        KeyedBudgetWindow,
    )
    from policyengine_simulation_persistence.shared_rows import (
        FoundReportRun,
        FoundSimulationRun,
    )
    from policyengine_simulation_persistence.tables import (
        ReportRunStatus,
        SimulationStatus,
    )


class EconomyRunStore:
    """Read and write economy simulations and reports, each in its own transaction."""

    def __init__(self, transaction: TransactionFactory) -> None:
        """Open each session with ``transaction``, which commits on success."""
        self._transaction = transaction

    @classmethod
    def from_session_factory(
        cls,
        session_factory: sessionmaker[Session],
        *,
        statement_timeout_milliseconds: int,
    ) -> EconomyRunStore:
        """Use ``session_factory``, bounding each statement to the given time."""
        return cls(
            bounded_transactions(
                session_factory,
                statement_timeout_milliseconds=statement_timeout_milliseconds,
            )
        )

    def read_simulation_inputs(
        self, request: EconomyRequest, choice: SimulationChoice
    ) -> EconomySimulationInputs:
        """Read a standalone simulation's catalog rows and values together.

        Raise UnknownCatalogVersionError, UnknownRegionError or
        UnknownDatasetError, or the policy reader's UnknownPolicyError,
        UnknownDynamicError or MissingPolicyParametersError, or
        OverlappingReformError.
        """
        with self._transaction() as session:
            catalog = read_catalog_rows(session, request)
            simulation = read_simulation_values(
                session, request.country, catalog.catalog_version_id, choice
            )
        return EconomySimulationInputs(catalog=catalog, simulation=simulation)

    def read_report_inputs(
        self,
        request: EconomyRequest,
        *,
        baseline: SimulationChoice,
        reform: SimulationChoice,
        local_area_dataset_name: str | None = None,
    ) -> EconomyReportInputs:
        """Read a report's catalog rows and both sides' values together.

        With ``local_area_dataset_name``, the dataset the default worker
        calculates local-area reports on, the report is read on the catalog
        version's local-area dataset (``read_local_area_dataset``) in place
        of the region's default. Raise ValueError when ``request`` also names
        a dataset; otherwise UnknownCatalogVersionError, UnknownRegionError
        or UnknownDatasetError, ReportSideError naming the side whose policy
        or dynamic is refused, or LocalAreaDatasetMismatchError when the
        catalog version doesn't give both area types that dataset.
        """
        with self._transaction() as session:
            catalog = read_catalog_rows(
                session, request, local_area_dataset_name=local_area_dataset_name
            )
            return read_report_sides(
                session, request, catalog, baseline=baseline, reform=reform
            )

    def read_report_definition(self, report_id: UUID) -> ReportDefinition | None:
        """Return what the stored report calculates, or None when there is none."""
        with self._transaction() as session:
            return read_report_definition(session, report_id)

    def read_simulation_definition(
        self, simulation_id: UUID
    ) -> SimulationDefinition | None:
        """Return what the stored simulation calculates, or None when there is none."""
        with self._transaction() as session:
            return read_simulation_definition(session, simulation_id)

    def read_report_rerun_inputs(
        self,
        request: EconomyRequest,
        *,
        baseline: SideDefinition,
        reform: SideDefinition,
    ) -> EconomyReportInputs:
        """Read a report re-run's catalog rows and both sides' values.

        ``request`` names the stored report's region code and dataset name,
        found in the catalog version of the current .py version. Raise the
        errors ``read_report_inputs`` raises.
        """
        with self._transaction() as session:
            catalog = read_catalog_rows(session, request)
            return read_report_sides(
                session,
                request,
                catalog,
                baseline=baseline.choice,
                reform=reform.choice,
            )

    def read_window_rerun_inputs(
        self,
        request: EconomyRequest,
        *,
        baseline: SimulationChoice,
        reform: SimulationChoice,
    ) -> BudgetWindowInputs:
        """Read a window re-run's catalog rows and both sides' values.

        ``request.year`` is the window's first year, and ``request`` names
        the stored report's region code and dataset name, found in the
        catalog version of the current .py version. Raise the errors
        ``read_report_inputs`` raises.
        """
        with self._transaction() as session:
            catalog = read_catalog_rows(session, request)
            return read_window_sides(
                session, request, catalog, baseline=baseline, reform=reform
            )

    def create_window_rerun(self, rerun: NewWindowRerun) -> FoundBudgetWindow:
        """Join or create the window's run and its years; see ``insert_window_rerun``.

        Raise EconomyRecordOutcomeUnknownError as ``find_or_create_report`` does.
        """
        return self._create(lambda session: insert_window_rerun(session, rerun))

    def create_report_rerun(self, rerun: NewReportRerun) -> FoundReportRun:
        """Join or create the report's run; see ``insert_report_rerun``.

        Raise ``insert_report_rerun``'s errors, and
        EconomyRecordOutcomeUnknownError as ``find_or_create_report`` does.
        """
        return self._create(lambda session: insert_report_rerun(session, rerun))

    def create_simulation_rerun(self, rerun: NewSimulationRerun) -> FoundSimulationRun:
        """Join or create the simulation's run; see ``insert_simulation_rerun``.

        Raise EconomyRecordOutcomeUnknownError as ``find_or_create_report`` does.
        """
        return self._create(lambda session: insert_simulation_rerun(session, rerun))

    def read_budget_window_inputs(
        self,
        request: EconomyRequest,
        *,
        baseline: SimulationChoice,
        reform: SimulationChoice,
    ) -> BudgetWindowInputs:
        """Read a window's catalog rows and both sides' values together.

        ``request.year`` is the window's first year. Raise the errors
        ``read_report_inputs`` raises.
        """
        with self._transaction() as session:
            catalog = read_catalog_rows(session, request)
            return read_window_sides(
                session, request, catalog, baseline=baseline, reform=reform
            )

    def find_or_create_report(self, report: KeyedEconomyReport) -> FoundEconomyReport:
        """Find the report by key, with its run, or create them.

        See ``find_or_create_economy_report``; raise its errors, and
        EconomyRecordOutcomeUnknownError when the commit fails after the
        rows were inserted.
        """
        return self._create(
            lambda session: find_or_create_economy_report(session, report)
        )

    def find_or_create_budget_window(
        self, window: KeyedBudgetWindow
    ) -> FoundBudgetWindow:
        """Find the window by key, with its window run, or create them.

        See ``find_or_create_budget_window``; raise as
        ``find_or_create_report`` does.
        """
        return self._create(
            lambda session: find_or_create_budget_window(session, window)
        )

    def find_or_create_simulation(
        self, simulation: KeyedEconomySimulation
    ) -> FoundEconomySimulation:
        """Find the simulation by key, with its run, or create them.

        Raise as ``find_or_create_report`` does.
        """
        return self._create(
            lambda session: find_or_create_economy_simulation(session, simulation)
        )

    def _create[Created](self, insert: Callable[[Session], Created]) -> Created:
        """Run ``insert`` in a session, telling a failed commit apart."""
        return create_in_transaction(
            self._transaction,
            insert,
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
        """Fail the report run, and its new simulation runs, while it is unstarted."""
        with self._transaction() as session:
            return fail_unstarted_report_run(
                session,
                report_run_id=report_run_id,
                simulation_run_ids=simulation_run_ids,
                error_message=error_message,
            )

    def record_simulation_call(
        self, simulation_run_id: UUID, modal_call_id: str
    ) -> SimulationStatus | None:
        """Record the simulation run's call ID while it is pending with none."""
        with self._transaction() as session:
            return record_simulation_run_call(
                session,
                simulation_run_id=simulation_run_id,
                modal_call_id=modal_call_id,
            )

    def fail_unstarted_simulation(
        self, simulation_run_id: UUID, error_message: str
    ) -> bool:
        """Fail the simulation run while it is pending with no call ID."""
        with self._transaction() as session:
            return fail_unstarted_simulation_run(
                session,
                simulation_run_id=simulation_run_id,
                error_message=error_message,
            )

    def read_report(self, report_id: UUID) -> EconomyReportState | None:
        """Return the report with its newest run, or None when there is none."""
        with self._transaction() as session:
            return read_economy_report(session, report_id)

    def read_simulation(self, simulation_id: UUID) -> EconomySimulationState | None:
        """Return the simulation with its newest run, or None when there is none."""
        with self._transaction() as session:
            return read_economy_simulation(session, simulation_id)


__all__ = [
    "BudgetWindowInputs",
    "EconomyCatalogRows",
    "EconomyRecordOutcomeUnknownError",
    "EconomyReportInputs",
    "EconomyRequest",
    "EconomyRunStore",
    "EconomySimulationInputs",
    "LocalAreaDatasetMismatchError",
    "ReportSide",
    "ReportSideError",
    "SideRefusal",
    "SimulationInputs",
    "TransactionFactory",
    "UnknownDatasetError",
    "UnknownRegionError",
]
