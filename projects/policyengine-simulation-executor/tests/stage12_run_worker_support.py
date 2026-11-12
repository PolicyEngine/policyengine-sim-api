"""Real simulation workers for coordinator tests, on in-memory run tables.

Each child the coordinator starts runs ``run_single_simulation`` in a thread,
under its own Modal call ID, so a test sees each worker claim and record its
own simulation run.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Event

from policyengine_simulation_contract.stage12_execution import (
    Stage12InvocationContext,
)
from policyengine_simulation_executor.stage12_runtime import (
    SimulationCalculation,
    SimulationWorkerDependencies,
    run_single_simulation,
)
from stage12_child_call_support import FutureCall
from stage12_fake_report_runs import ComparisonStoreNotUsed
from stage12_fake_stores import simulation_frames

# A worker for the role this names raises from its calculation.
CALCULATION_FAILURE = "the reform calculation failed"


class EarlierCall:
    """An earlier attempt's child, which a restarted coordinator only cancels.

    Each cancellation is recorded in ``events``, beside the run tables' writes.
    """

    def __init__(self, object_id: str, events: list[str]) -> None:
        self.object_id = object_id
        self.events = events
        self.cancelled = 0

    def get(self, *, timeout=None):
        msg = "a restarted coordinator never waits for an earlier attempt's child"
        raise AssertionError(msg)

    def cancel(self) -> None:
        self.cancelled += 1
        self.events.append(f"cancel:{self.object_id}")


class RunWorkerInvoker:
    """Start each child as the real worker, on the coordinator's run tables.

    ``failing_role`` names a role whose calculation raises. ``held_roles``
    names roles whose workers wait, after claiming their run, until
    ``release_held`` is set, so a test can act while they run. Each
    calculation carries ``calculation_provenance``, the SPM receipt a real
    worker would write, when one is given.
    """

    def __init__(
        self,
        report_runs,
        artifacts,
        *,
        failing_role: str | None = None,
        held_roles: tuple[str, ...] = (),
        calculation_provenance=None,
    ) -> None:
        self.report_runs = report_runs
        self.artifacts = artifacts
        self.failing_role = failing_role
        self.held_roles = held_roles
        self.calculation_provenance = calculation_provenance
        # "spawn:<role>" for each child started, in order.
        self.events: list[str] = []
        self.release_held = Event()
        self.executor = ThreadPoolExecutor(max_workers=2)
        self.contexts: list[Stage12InvocationContext] = []
        self.simulations: list[dict] = []
        self.call_ids: list[str] = []
        self.restored: dict[str, EarlierCall] = {}

    def spawn(self, request):
        role = request.simulation["role"]
        call_id = f"fc-{role}-worker-{len(self.call_ids) + 1}"
        self.call_ids.append(call_id)
        self.events.append(f"spawn:{role}")
        self.contexts.append(Stage12InvocationContext.model_validate(request.context))
        self.simulations.append(request.simulation)
        return self.worker_call(request, call_id)

    def worker_call(self, request, call_id: str) -> FutureCall:
        """Run the real worker for ``request`` under ``call_id``, in a thread."""
        role = request.simulation["role"]

        def calculate(_simulation):
            if role in self.held_roles:
                self.release_held.wait(timeout=10)
            if role == self.failing_role:
                raise RuntimeError(CALCULATION_FAILURE)
            return SimulationCalculation(
                frames=simulation_frames(100.0 if role == "baseline" else 120.0),
                calculation_provenance=self.calculation_provenance,
            )

        future = self.executor.submit(
            run_single_simulation,
            request.simulation,
            request.context,
            required_country="us",
            modal_call_id=call_id,
            dependencies=SimulationWorkerDependencies(
                store=ComparisonStoreNotUsed(),
                artifacts=self.artifacts,
                calculator=calculate,
                report_runs=self.report_runs,
            ),
        )
        return FutureCall(call_id, future)

    def restore(self, invocation_id: str) -> EarlierCall:
        call = EarlierCall(invocation_id, self.report_runs.events)
        self.restored[invocation_id] = call
        return call
