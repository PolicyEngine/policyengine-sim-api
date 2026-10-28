"""A household calculation service over fake runs, Modal, manifest and Public API rows.

``Harness`` wires the real ``HouseholdCalculationService`` and
``RunRecorder`` to ``FakeHouseholdStore``, ``FakeRunTransitions``, a fake
dispatcher and inspector, the fixture manifest and ``FakeHouseholdReads``,
all on one clock.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from household_fakes import (
    HOUSEHOLD_ID,
    POLICYENGINE_VERSION,
    FakeHouseholdReads,
    FakeHouseholdStore,
    manifest_workers,
)
from policyengine_simulation_contract.call_reports import CallEndedReport
from policyengine_simulation_contract.household_execution import (
    HouseholdExecutionRequest,
)
from policyengine_simulation_entry.household_api_models import (
    HouseholdCalculationSubmission,
)
from policyengine_simulation_entry.household_calculations import (
    HouseholdCalculationService,
)
from policyengine_simulation_entry.household_recording import (
    HouseholdResultRecorder,
)
from policyengine_simulation_entry.run_dispatch import RunDispatchService
from policyengine_simulation_entry.run_recording import ResultRecorders, RunRecorder
from policyengine_simulation_persistence import SimulationKind
from run_fakes import FakeDispatcher, FakeInspector, FakeRunTransitions, ManualClock

if TYPE_CHECKING:
    from uuid import UUID

EARNINGS_AXES = [
    [{"name": "employment_income", "count": 5, "min": 0, "max": 80_000}],
]


def submission(**overrides: object) -> HouseholdCalculationSubmission:
    """Return a current-law calculation of the stored one-person US household."""
    values: dict[str, object] = {
        "country": "us",
        "policyengine_version": POLICYENGINE_VERSION,
        "household_id": HOUSEHOLD_ID,
    }
    values.update(overrides)
    return HouseholdCalculationSubmission.model_validate(values)


class Harness:
    """A service over fake runs, Modal and manifest, with a recorder for reports."""

    def __init__(self, dispatcher: FakeDispatcher | None = None) -> None:
        """Build every part on one clock."""
        self.clock = ManualClock()
        self.transitions = FakeRunTransitions(self.clock)
        self.store = FakeHouseholdStore(self.transitions, self.clock)
        self.dispatcher = dispatcher or FakeDispatcher()
        self.inspector = FakeInspector()
        self.reads = FakeHouseholdReads()
        self.service = HouseholdCalculationService(
            store=self.store,
            reads=self.reads,
            dispatch=RunDispatchService(
                transitions=self.transitions,
                dispatcher=self.dispatcher,
                clock=self.clock,
            ),
            workers=manifest_workers(),
        )
        self.recorder = RunRecorder(
            transitions=self.transitions,
            runs=self.transitions,
            inspector=self.inspector,
            recorders=ResultRecorders(
                simulations={
                    SimulationKind.HOUSEHOLD: HouseholdResultRecorder(
                        write_output=self.store.write_output
                    )
                }
            ),
            clock=self.clock,
        )

    def submit(self, **overrides: object):
        """Submit a calculation and return it as it reads afterwards."""
        return asyncio.run(self.service.submit(submission(**overrides)))

    def read(self, run_id: UUID):
        """Read a calculation."""
        return asyncio.run(self.service.read(run_id))

    def report_call_ended(self, *run_ids: UUID) -> None:
        """Deliver the call's report that it ended, as report_call_ended does."""
        call_id = self.transitions.runs[run_ids[0]].modal_call_id
        assert call_id is not None
        asyncio.run(
            self.recorder.record_reported_call(
                CallEndedReport(modal_call_id=call_id, simulation_run_ids=run_ids)
            )
        )

    def sent_request(self, position: int = -1) -> HouseholdExecutionRequest:
        """Return a spawned call's household request."""
        return HouseholdExecutionRequest.model_validate(
            self.dispatcher.requests[position].arguments[0]
        )
