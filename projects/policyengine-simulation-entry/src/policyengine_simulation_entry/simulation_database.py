"""The Simulation Entrypoint's connection to its own ``simulation`` schema."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING

from policyengine_simulation_persistence import (
    BudgetWindowRunStore,
    CatalogReader,
    EconomyResultLabels,
    ReportStore,
    RunStore,
    RunTransitions,
    SimulationStore,
    create_simulation_engine,
    read_economy_comparison,
    read_schema_revision,
)

from policyengine_simulation_entry.budget_window_reads import BudgetWindowReportReads
from policyengine_simulation_entry.budget_window_reports import BudgetWindowReports
from policyengine_simulation_entry.budget_window_routes import (
    BudgetWindowRouteServices,
)
from policyengine_simulation_entry.economy_recorders import economy_result_recorders
from policyengine_simulation_entry.economy_reports import EconomyReports
from policyengine_simulation_entry.economy_routes import (
    EconomyReportReads,
    EconomyRouteServices,
)
from policyengine_simulation_entry.modal_calls import (
    ModalCallDispatcher,
    ModalCallInspector,
)
from policyengine_simulation_entry.run_dispatch import RunDispatchService
from policyengine_simulation_entry.run_recording import RunRecorder

if TYPE_CHECKING:
    from uuid import UUID

    from policyengine_simulation_contract.macro_output import SingleYearMacroOutput
    from sqlalchemy import Engine


@dataclass(frozen=True)
class DatabaseResultReader:
    """Rebuild a comparison's result from its rows, with UK area names."""

    engine: Engine
    catalog: CatalogReader

    def read_result(
        self, report_run_id: UUID, catalog_version_id: UUID, country: str
    ) -> SingleYearMacroOutput:
        """Return the result in today's format; raise LookupError if it cannot be."""
        labels = (
            self.catalog.area_labels(catalog_version_id)
            if country == "uk"
            else EconomyResultLabels()
        )
        with self.engine.connect() as connection:
            return read_economy_comparison(connection, report_run_id, labels)


class SimulationDatabase:
    """Run short, blocking database operations off the event loop."""

    def __init__(self, engine: Engine) -> None:
        """Use ``engine``, which connects as the runtime role."""
        self.engine = engine

    @classmethod
    def from_url(cls, database_url: str) -> SimulationDatabase:
        """Build the runtime engine; no connection opens until first use.

        Raise ValueError, without echoing the URL, when it is malformed.
        """
        return cls(create_simulation_engine(database_url))

    def _read_revision(self) -> str | None:
        with self.engine.connect() as connection:
            return read_schema_revision(connection)

    async def read_revision(self) -> str | None:
        """Return the applied migration revision, or None before the first one."""
        return await asyncio.to_thread(self._read_revision)

    def call_recorder(self) -> RunRecorder:
        """Return what records a reported call's outcome on its runs.

        Economy simulation runs and comparison steps have result recorders. A
        finished call of another kind is answered no_result_recorder and left
        for a revision that records it.
        """
        return RunRecorder(
            transitions=RunTransitions(self.engine),
            runs=RunStore(self.engine),
            inspector=ModalCallInspector(),
            recorders=economy_result_recorders(),
        )

    def economy_routes(
        self, *, artifact_environment: str, modal_environment: str
    ) -> EconomyRouteServices:
        """Return the economy path, starting calls in ``modal_environment``.

        ``artifact_environment`` names the deployment in stored files' paths.
        """
        engine = self.engine
        simulations = SimulationStore(engine)
        reports = ReportStore(engine)
        runs = RunStore(engine)
        catalog = CatalogReader(engine)
        transitions = RunTransitions(engine)
        return EconomyRouteServices(
            reports=EconomyReports(
                simulations=simulations,
                reports=reports,
                runs=runs,
                catalog=catalog,
                transitions=transitions,
                dispatch=RunDispatchService(
                    transitions=transitions,
                    dispatcher=ModalCallDispatcher(modal_environment),
                ),
                artifact_environment=artifact_environment,
            ),
            reads=EconomyReportReads(
                simulations=simulations,
                reports=reports,
                runs=runs,
                catalog=catalog,
                results=DatabaseResultReader(engine, catalog),
            ),
        )

    def budget_window_routes(
        self, economy: EconomyRouteServices
    ) -> BudgetWindowRouteServices:
        """Return the budget-window path, running its years on ``economy``'s path."""
        engine = self.engine
        reports = ReportStore(engine)
        windows = BudgetWindowRunStore(engine)
        catalog = CatalogReader(engine)
        return BudgetWindowRouteServices(
            reports=BudgetWindowReports(
                economy=economy.reports,
                reports=reports,
                windows=windows,
                runs=RunStore(engine),
                catalog=catalog,
            ),
            reads=BudgetWindowReportReads(
                simulations=SimulationStore(engine),
                reports=reports,
                windows=windows,
                catalog=catalog,
            ),
        )

    def dispose(self) -> None:
        """Close every pooled connection."""
        self.engine.dispose()
