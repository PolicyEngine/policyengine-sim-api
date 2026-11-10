"""Waiting for a report's started child simulations to finish.

The coordinator probes every started child together, without blocking,
backing off between passes while none finishes, within the shared child-wait
window that opens when the coordinator starts.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from policyengine_simulation_contract.stage12_execution import (
    SimulationArtifactDescriptor,
)
from policyengine_simulation_observability.stages import Stage

from .coordinated_simulations import (
    cancel_pending_simulations,
    cancellation,
    child_failure_detail,
)
from .failure_recording import report_original_failure
from .failures import Stage12ExecutionError, Stage12FailureDetail

if TYPE_CHECKING:
    from .dependencies import ChildCall
    from .report_coordination import ReportCoordination

SIMULATION_POLL_INITIAL_SECONDS = 0.25
SIMULATION_POLL_MAX_SECONDS = 1.0
SIMULATION_POLL_BACKOFF_FACTOR = 2.0


def _next_poll_interval(current: float) -> float:
    return min(
        current * SIMULATION_POLL_BACKOFF_FACTOR,
        SIMULATION_POLL_MAX_SECONDS,
    )


def wait_for_children(
    coordination: ReportCoordination,
    calls: dict[str, ChildCall],
    descriptors: dict[str, SimulationArtifactDescriptor],
) -> None:
    """Observe every started child together until each has a descriptor.

    A child's failure, or the shared wait window closing, stops the
    unfinished children and raises Stage12ExecutionError.
    """
    with coordination.span(Stage.STAGE12_CHILD_WAIT):
        _observe_children(coordination, calls, descriptors)


def _observe_children(
    coordination: ReportCoordination,
    calls: dict[str, ChildCall],
    descriptors: dict[str, SimulationArtifactDescriptor],
) -> None:
    """Probe every unfinished child, without blocking, until each finishes.

    Between passes the coordinator sleeps, backing off while no child
    finishes, and never past the shared wait window.
    """
    wait_stage_name = coordination.stage_plan.name(Stage.STAGE12_CHILD_WAIT)
    timeout_seconds = coordination.dependencies.wait_timeout_seconds
    pending = set(calls)
    poll_interval = SIMULATION_POLL_INITIAL_SECONDS

    while pending:
        progress_made = False
        for role in sorted(pending):
            call = calls[role]
            try:
                # The probe records the child's role, so a failure names it.
                with coordination.progress.stage(wait_stage_name, role=role):
                    result = call.get(timeout=0)
                    descriptor = SimulationArtifactDescriptor.model_validate(result)
            except TimeoutError:
                continue
            except Exception as error:  # noqa: BLE001  # a child call's failure can be any exception type; it is recorded and stops the other child
                detail = child_failure_detail(
                    error=error,
                    simulation=coordination.simulations[role],
                    store=coordination.persistence,
                    runtime=coordination.runtime,
                    progress=coordination.progress,
                )
                cancel_pending_simulations(
                    calls=calls,
                    simulations=coordination.simulations,
                    roles=pending - {role},
                    store=coordination.persistence,
                    detail=cancellation(f"the {role} simulation failed"),
                )
                raise Stage12ExecutionError(detail) from None
            descriptors[role] = descriptor
            pending.remove(role)
            progress_made = True

        if not pending:
            return
        remaining_seconds = (
            coordination.child_wait_deadline - coordination.dependencies.monotonic()
        )
        if remaining_seconds <= 0:
            detail = Stage12FailureDetail(
                error_code="simulation_wait_timeout",
                error_summary=(
                    "Stage 12 simulations did not finish within "
                    f"{timeout_seconds:g} seconds of the report coordinator "
                    "starting."
                ),
            )
            wait_timeout = Stage12ExecutionError(detail)
            report_original_failure(
                wait_timeout,
                runtime=coordination.runtime,
                failure_scope="child_wait_timeout",
            )
            cancel_pending_simulations(
                calls=calls,
                simulations=coordination.simulations,
                roles=pending,
                store=coordination.persistence,
                detail=cancellation(
                    f"the {timeout_seconds:g}-second wait window closed"
                ),
            )
            raise wait_timeout
        # Never sleep past the deadline, so the window closes on time.
        coordination.dependencies.sleep(min(poll_interval, remaining_seconds))
        poll_interval = (
            SIMULATION_POLL_INITIAL_SECONDS
            if progress_made
            else _next_poll_interval(poll_interval)
        )
