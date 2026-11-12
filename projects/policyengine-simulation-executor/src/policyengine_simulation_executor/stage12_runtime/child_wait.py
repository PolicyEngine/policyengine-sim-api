"""Waiting for a report's started child simulations to finish.

The coordinator probes every started child together, without blocking,
backing off between passes while none finishes, within the shared child-wait
window that opens when the coordinator starts. A failed child, or the window
closing, is recorded through the coordinator's own ``child_failure`` and
``cancel_children``, on whichever records it keeps.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from policyengine_simulation_contract.stage12_execution import (
    SimulationArtifactDescriptor,
)
from policyengine_simulation_observability.stages import Stage

from .coordinated_simulations import cancellation
from .failure_recording import report_original_failure
from .failures import Stage12ExecutionError, Stage12FailureDetail

if TYPE_CHECKING:
    from collections.abc import Mapping
    from contextlib import AbstractContextManager

    from policyengine_observability import ObservabilityRuntime
    from policyengine_simulation_observability.stages import StagePlan

    from .coordination_core import CoordinatorDependencies
    from .dependencies import ChildCall
    from .failure_causes import StageProgress


class WaitingCoordination(Protocol):
    """What waiting for children needs of a coordinator."""

    @property
    def dependencies(self) -> CoordinatorDependencies:
        """The coordinator's seams: its clock, sleep and wait window."""
        ...

    @property
    def stage_plan(self) -> StagePlan:
        """How the report's stages are named."""
        ...

    @property
    def progress(self) -> StageProgress:
        """The last stage entered, for failure causes."""
        ...

    @property
    def child_wait_deadline(self) -> float:
        """The monotonic time the shared wait window closes."""
        ...

    @property
    def runtime(self) -> ObservabilityRuntime | None:
        """The telemetry runtime, or None."""
        ...

    def span(
        self, stage: Stage, attributes: Mapping[str, object] | None = None
    ) -> AbstractContextManager[object]:
        """Track ``stage`` and time it as a span."""
        ...

    def child_failure(self, role: str, error: Exception) -> Stage12FailureDetail:
        """Report a failed child call; return the failure its report records."""
        ...

    def cancel_children(self, roles: set[str], detail: Stage12FailureDetail) -> None:
        """Record each started, unfinished child of ``roles`` failed, then stop it."""
        ...


SIMULATION_POLL_INITIAL_SECONDS = 0.25
SIMULATION_POLL_MAX_SECONDS = 1.0
SIMULATION_POLL_BACKOFF_FACTOR = 2.0


def _next_poll_interval(current: float) -> float:
    return min(
        current * SIMULATION_POLL_BACKOFF_FACTOR,
        SIMULATION_POLL_MAX_SECONDS,
    )


def wait_for_children(
    coordination: WaitingCoordination,
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
    coordination: WaitingCoordination,
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
                detail = coordination.child_failure(role, error)
                coordination.cancel_children(
                    pending - {role}, cancellation(f"the {role} simulation failed")
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
            coordination.cancel_children(
                set(pending),
                cancellation(f"the {timeout_seconds:g}-second wait window closed"),
            )
            raise wait_timeout
        # Never sleep past the deadline, so the window closes on time.
        coordination.dependencies.sleep(min(poll_interval, remaining_seconds))
        poll_interval = (
            SIMULATION_POLL_INITIAL_SECONDS
            if progress_made
            else _next_poll_interval(poll_interval)
        )
