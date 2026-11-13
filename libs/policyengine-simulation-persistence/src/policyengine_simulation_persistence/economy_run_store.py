"""The economy store: each read and write in a transaction of its own.

The Simulation Entrypoint creates an economy report, or a standalone economy
simulation, in three steps, each its own transaction, with the Modal call
started between them: it reads the request's inputs, creates the rows, then
records the call or fails the run it started. ``EconomyRunStore`` runs the
reads and writes of ``economy_run_records`` and ``policy_reform`` that way.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy.exc import SQLAlchemyError

from policyengine_simulation_persistence.budget_window_records import (
    insert_budget_window_report,
)
from policyengine_simulation_persistence.economy_inputs import (
    BudgetWindowInputs,
    EconomyCatalogRows,
    EconomyReportInputs,
    EconomyRequest,
    EconomySimulationInputs,
    ReportSide,
    ReportSideError,
    RunReuse,
    SideRefusal,
    SimulationInputs,
    UnknownDatasetError,
    UnknownRegionError,
    read_catalog_rows,
    read_report_sides,
    read_simulation_values,
    read_window_sides,
    search_by_definition,
    search_own_runs,
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
    insert_economy_report,
    insert_economy_simulation,
    record_report_run_call,
    record_simulation_run_call,
)
from policyengine_simulation_persistence.engine import bounded_transaction
from policyengine_simulation_persistence.keyed_economy_records import (
    find_or_create_economy_report,
    find_or_create_economy_simulation,
)
from policyengine_simulation_persistence.keyed_window_records import (
    find_or_create_budget_window,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from contextlib import AbstractContextManager
    from uuid import UUID

    from sqlalchemy import Connection, Engine

    from policyengine_simulation_persistence.budget_window_records import (
        NewBudgetWindowReport,
    )
    from policyengine_simulation_persistence.economy_rerun_records import (
        NewReportRerun,
        NewSimulationRerun,
        NewWindowRerun,
        ReportDefinition,
        SideDefinition,
        SimulationDefinition,
        WindowYearDefinition,
    )
    from policyengine_simulation_persistence.economy_run_reads import (
        EconomyReportState,
        EconomySimulationState,
    )
    from policyengine_simulation_persistence.economy_run_records import (
        NewEconomyReport,
        NewEconomySimulation,
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

type TransactionFactory = Callable[[], AbstractContextManager[Connection]]


class EconomyRecordOutcomeUnknownError(RuntimeError):
    """A create's statements ran, but its commit failed: the rows may exist.

    PostgreSQL may have applied the commit before the connection failed, so
    the caller must not answer that nothing was created.
    """


class EconomyRunStore:
    """Read and write economy simulations and reports, each in its own transaction."""

    def __init__(self, transaction: TransactionFactory) -> None:
        """Open each transaction with ``transaction``, which commits on success."""
        self._transaction = transaction

    @classmethod
    def from_engine(
        cls, engine: Engine, *, statement_timeout_milliseconds: int
    ) -> EconomyRunStore:
        """Use ``engine``, bounding each statement to the given time."""

        def transaction() -> AbstractContextManager[Connection]:
            return bounded_transaction(
                engine, statement_timeout_milliseconds=statement_timeout_milliseconds
            )

        return cls(transaction)

    def read_simulation_inputs(
        self, request: EconomyRequest, choice: SimulationChoice
    ) -> EconomySimulationInputs:
        """Read a standalone simulation's catalog rows and values together.

        Raise UnknownCatalogVersionError, UnknownRegionError or
        UnknownDatasetError, or the policy reader's UnknownPolicyError,
        UnknownDynamicError or MissingPolicyParametersError, or
        OverlappingReformError.
        """
        with self._transaction() as connection:
            catalog = read_catalog_rows(connection, request)
            simulation = read_simulation_values(
                connection, request.country, catalog.catalog_version_id, choice
            )
        return EconomySimulationInputs(catalog=catalog, simulation=simulation)

    def read_report_inputs(
        self,
        request: EconomyRequest,
        *,
        baseline: SimulationChoice,
        reform: SimulationChoice,
        reuse: RunReuse | None,
    ) -> EconomyReportInputs:
        """Read a report's catalog rows, both sides' values and reusable runs.

        With ``reuse``, each side gets the run ``find_reusable_run`` finds,
        and the reform never gets the baseline's. Raise
        UnknownCatalogVersionError, UnknownRegionError or UnknownDatasetError,
        or ReportSideError naming the side whose policy or dynamic is refused.
        """
        with self._transaction() as connection:
            catalog = read_catalog_rows(connection, request)
            search = (
                None
                if reuse is None
                else search_by_definition(
                    connection, catalog.population(request.year), reuse
                )
            )
            return read_report_sides(
                connection,
                request,
                catalog,
                baseline=baseline,
                reform=reform,
                search=search,
            )

    def read_report_definition(self, report_id: UUID) -> ReportDefinition | None:
        """Return what the stored report calculates, or None when there is none."""
        with self._transaction() as connection:
            return read_report_definition(connection, report_id)

    def read_simulation_definition(
        self, simulation_id: UUID
    ) -> SimulationDefinition | None:
        """Return what the stored simulation calculates, or None when there is none."""
        with self._transaction() as connection:
            return read_simulation_definition(connection, simulation_id)

    def read_report_rerun_inputs(
        self,
        request: EconomyRequest,
        *,
        baseline: SideDefinition,
        reform: SideDefinition,
        reuse: RunReuse | None,
    ) -> EconomyReportInputs:
        """Read a report re-run's catalog rows, both sides' values and reusable runs.

        ``request`` names the stored report's region code and dataset name,
        found in the catalog version of the current .py version. With
        ``reuse``, each side gets the run ``find_reusable_run_of`` finds among
        its own simulation's runs, and the reform never gets the baseline's.
        Raise the errors ``read_report_inputs`` raises.
        """
        with self._transaction() as connection:
            catalog = read_catalog_rows(connection, request)
            search = (
                None
                if reuse is None
                else search_own_runs(
                    connection,
                    catalog.population(request.year),
                    reuse,
                    {
                        "baseline": baseline.simulation_id,
                        "reform": reform.simulation_id,
                    },
                )
            )
            return read_report_sides(
                connection,
                request,
                catalog,
                baseline=baseline.choice,
                reform=reform.choice,
                search=search,
            )

    def read_window_rerun_inputs(
        self,
        request: EconomyRequest,
        *,
        baseline: SimulationChoice,
        reform: SimulationChoice,
        years: Sequence[WindowYearDefinition],
        reuse: RunReuse | None,
    ) -> BudgetWindowInputs:
        """Read a window re-run's catalog rows, both sides' values and each year's runs.

        ``request.year`` is the window's first year. With ``reuse``, each
        year's sides get the runs ``find_reusable_run_of`` finds among that
        year's own simulations' runs, and the reform never gets the
        baseline's. Raise the errors ``read_report_inputs`` raises.
        """
        with self._transaction() as connection:
            catalog = read_catalog_rows(connection, request)
            return read_window_sides(
                connection,
                request,
                catalog,
                baseline=baseline,
                reform=reform,
                searches=(
                    (
                        window_year.year,
                        None
                        if reuse is None
                        else search_own_runs(
                            connection,
                            catalog.population(window_year.year),
                            reuse,
                            {
                                "baseline": window_year.baseline_simulation_id,
                                "reform": window_year.reform_simulation_id,
                            },
                        ),
                    )
                    for window_year in years
                ),
            )

    def create_window_rerun(self, rerun: NewWindowRerun) -> FoundBudgetWindow:
        """Join or create the window's run and its years; see ``insert_window_rerun``.

        Raise EconomyRecordOutcomeUnknownError as ``create_report`` does.
        """
        return self._create(lambda connection: insert_window_rerun(connection, rerun))

    def create_report_rerun(self, rerun: NewReportRerun) -> FoundReportRun:
        """Join or create the report's run; see ``insert_report_rerun``.

        Raise ``insert_report_rerun``'s errors, and
        EconomyRecordOutcomeUnknownError as ``create_report`` does.
        """
        return self._create(lambda connection: insert_report_rerun(connection, rerun))

    def create_simulation_rerun(self, rerun: NewSimulationRerun) -> FoundSimulationRun:
        """Join or create the simulation's run; see ``insert_simulation_rerun``.

        Raise EconomyRecordOutcomeUnknownError as ``create_report`` does.
        """
        return self._create(
            lambda connection: insert_simulation_rerun(connection, rerun)
        )

    def read_budget_window_inputs(
        self,
        request: EconomyRequest,
        *,
        baseline: SimulationChoice,
        reform: SimulationChoice,
        reuse: RunReuse | None,
        years: Sequence[int],
    ) -> BudgetWindowInputs:
        """Read a window's catalog rows and both sides' values, then each year's runs.

        ``request.year`` is the window's first year. With ``reuse``, each
        year's sides get the runs ``find_reusable_run`` finds for that year,
        and the reform never gets the baseline's. Raise the errors
        ``read_report_inputs`` raises.
        """
        with self._transaction() as connection:
            catalog = read_catalog_rows(connection, request)
            return read_window_sides(
                connection,
                request,
                catalog,
                baseline=baseline,
                reform=reform,
                searches=(
                    (
                        year,
                        None
                        if reuse is None
                        else search_by_definition(
                            connection, catalog.population(year), reuse
                        ),
                    )
                    for year in years
                ),
            )

    def create_budget_window_report(self, report: NewBudgetWindowReport) -> None:
        """Create the window's report, its budget-window run and each year's runs.

        Raise ``insert_budget_window_report``'s errors, and
        EconomyRecordOutcomeUnknownError when the commit fails after the
        rows were inserted.
        """
        self._create(lambda connection: insert_budget_window_report(connection, report))

    def find_or_create_report(self, report: KeyedEconomyReport) -> FoundEconomyReport:
        """Find the report by key, with its run, or create them.

        See ``find_or_create_economy_report``; raise its errors, and
        EconomyRecordOutcomeUnknownError when the commit fails after the
        rows were inserted.
        """
        return self._create(
            lambda connection: find_or_create_economy_report(connection, report)
        )

    def find_or_create_budget_window(
        self, window: KeyedBudgetWindow
    ) -> FoundBudgetWindow:
        """Find the window by key, with its window run, or create them.

        See ``find_or_create_budget_window``; raise as
        ``find_or_create_report`` does.
        """
        return self._create(
            lambda connection: find_or_create_budget_window(connection, window)
        )

    def find_or_create_simulation(
        self, simulation: KeyedEconomySimulation
    ) -> FoundEconomySimulation:
        """Find the simulation by key, with its run, or create them.

        Raise as ``find_or_create_report`` does.
        """
        return self._create(
            lambda connection: find_or_create_economy_simulation(connection, simulation)
        )

    def create_report(self, report: NewEconomyReport) -> None:
        """Create the report, its new simulations and its runs.

        Raise ``insert_economy_report``'s errors, and
        EconomyRecordOutcomeUnknownError when the commit fails after the
        rows were inserted.
        """
        self._create(lambda connection: insert_economy_report(connection, report))

    def create_simulation(self, simulation: NewEconomySimulation) -> None:
        """Create the simulation and its run; see ``insert_economy_simulation``.

        Raise EconomyRecordOutcomeUnknownError as ``create_report`` does.
        """
        self._create(
            lambda connection: insert_economy_simulation(connection, simulation)
        )

    def _create[Created](self, insert: Callable[[Connection], Created]) -> Created:
        """Run ``insert`` in a transaction, telling a failed commit apart.

        Return what ``insert`` returns. An error from ``insert`` leaves
        nothing written and propagates. An error once it has run is the
        commit's, after which the rows may exist:
        EconomyRecordOutcomeUnknownError.
        """
        inserted = False
        try:
            with self._transaction() as connection:
                created = insert(connection)
                inserted = True
        except SQLAlchemyError as error:
            if not inserted:
                raise
            msg = "the create's commit failed after its rows were inserted"
            raise EconomyRecordOutcomeUnknownError(msg) from error
        return created

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
        """Fail the report run, and its new simulation runs, while it is unstarted."""
        with self._transaction() as connection:
            return fail_unstarted_report_run(
                connection,
                report_run_id=report_run_id,
                simulation_run_ids=simulation_run_ids,
                error_message=error_message,
            )

    def record_simulation_call(
        self, simulation_run_id: UUID, modal_call_id: str
    ) -> SimulationStatus | None:
        """Record the simulation run's call ID while it is pending with none."""
        with self._transaction() as connection:
            return record_simulation_run_call(
                connection,
                simulation_run_id=simulation_run_id,
                modal_call_id=modal_call_id,
            )

    def fail_unstarted_simulation(
        self, simulation_run_id: UUID, error_message: str
    ) -> bool:
        """Fail the simulation run while it is pending with no call ID."""
        with self._transaction() as connection:
            return fail_unstarted_simulation_run(
                connection,
                simulation_run_id=simulation_run_id,
                error_message=error_message,
            )

    def read_report(self, report_id: UUID) -> EconomyReportState | None:
        """Return the report with its newest run, or None when there is none."""
        with self._transaction() as connection:
            return read_economy_report(connection, report_id)

    def read_simulation(self, simulation_id: UUID) -> EconomySimulationState | None:
        """Return the simulation with its newest run, or None when there is none."""
        with self._transaction() as connection:
            return read_economy_simulation(connection, simulation_id)


__all__ = [
    "BudgetWindowInputs",
    "EconomyCatalogRows",
    "EconomyRecordOutcomeUnknownError",
    "EconomyReportInputs",
    "EconomyRequest",
    "EconomyRunStore",
    "EconomySimulationInputs",
    "ReportSide",
    "ReportSideError",
    "RunReuse",
    "SideRefusal",
    "SimulationInputs",
    "TransactionFactory",
    "UnknownDatasetError",
    "UnknownRegionError",
]
