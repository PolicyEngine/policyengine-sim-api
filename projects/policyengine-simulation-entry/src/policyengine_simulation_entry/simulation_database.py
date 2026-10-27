"""The Simulation Entrypoint's connection to its own ``simulation`` schema."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from policyengine_simulation_persistence import (
    HouseholdCalculationStore,
    RunStore,
    RunTransitions,
    SimulationKind,
    create_simulation_engine,
    read_schema_revision,
)

from policyengine_simulation_entry.household_calculations import (
    HouseholdCalculationService,
)
from policyengine_simulation_entry.household_recording import (
    HouseholdResultRecorder,
)
from policyengine_simulation_entry.household_workers import ManifestHouseholdWorkers
from policyengine_simulation_entry.modal_calls import (
    ModalCallDispatcher,
    ModalCallInspector,
)
from policyengine_simulation_entry.run_dispatch import RunDispatchService
from policyengine_simulation_entry.run_recording import ResultRecorders, RunRecorder

if TYPE_CHECKING:
    from sqlalchemy import Engine


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

        Household calls have a result recorder. A finished call of a kind
        without one (economy runs and comparisons, until their path registers
        them) is answered no_result_recorder and left for the safety check.
        """
        return RunRecorder(
            transitions=RunTransitions(self.engine),
            runs=RunStore(self.engine),
            inspector=ModalCallInspector(),
            recorders=ResultRecorders(
                simulations={SimulationKind.HOUSEHOLD: HouseholdResultRecorder()}
            ),
        )

    def household_calculations(
        self, modal_environment: str
    ) -> HouseholdCalculationService:
        """Return the household calculation service on this database.

        It finds household functions in the v2 version manifest of
        ``modal_environment`` and starts their calls there. Nothing connects
        until the first request.
        """
        return HouseholdCalculationService(
            store=HouseholdCalculationStore(self.engine),
            dispatch=RunDispatchService(
                transitions=RunTransitions(self.engine),
                dispatcher=ModalCallDispatcher(modal_environment),
            ),
            workers=ManifestHouseholdWorkers.from_modal_environment(modal_environment),
        )

    def dispose(self) -> None:
        """Close every pooled connection."""
        self.engine.dispose()
