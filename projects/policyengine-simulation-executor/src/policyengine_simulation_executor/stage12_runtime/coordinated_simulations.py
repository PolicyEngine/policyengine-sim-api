"""The coordinator's child simulations: their records, failures and cancellation."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from policyengine_simulation_contract.stage12_execution import (
    ComparisonRunLifecycleStatus,
    ComparisonSimulationRecord,
    PlannedSimulationExecutionInput,
    Stage12InvocationContext,
)

from .failure_recording import FailureSite, report_failure
from .failures import Stage12FailureDetail, failure_detail_from_record
from .partition import should_segment_simulation
from .simulation import simulation_input_sha256

if TYPE_CHECKING:
    from policyengine_observability import ObservabilityRuntime

    from .dependencies import ChildCall, ComparisonStore
    from .failure_causes import StageProgress

# Moved out of coordination.py; child failures still log under its name.
logger = logging.getLogger(
    "policyengine_simulation_executor.stage12_runtime.coordination"
)


def _log_child_operation_failure(
    level: int,
    message: str,
    *,
    simulation: PlannedSimulationExecutionInput,
    error: Exception,
) -> None:
    """Log a failed child state update or cancellation with its identifiers.

    ``message`` receives the simulation role, simulation execution ID,
    evaluation ID and error type, in that order. The error's own message is
    never logged.
    """
    error_type = type(error).__name__
    logger.log(
        level,
        message,
        simulation.role.value,
        simulation.simulation_execution_id,
        simulation.evaluation_id,
        error_type,
        extra={
            "evaluation_id": str(simulation.evaluation_id),
            "simulation_execution_id": str(simulation.simulation_execution_id),
            "simulation_role": simulation.role.value,
            "error_type": error_type,
        },
    )


def _replace_running_simulation(
    *,
    store: ComparisonStore,
    simulation: PlannedSimulationExecutionInput,
    status: ComparisonRunLifecycleStatus,
    detail: Stage12FailureDetail,
) -> ComparisonSimulationRecord:
    changed_at = datetime.now(UTC)
    current = store.get_simulation(simulation.simulation_execution_id)
    candidate = current.model_copy(
        update={
            "status": status,
            "error_code": detail.error_code,
            "error_summary": detail.error_summary,
            "updated_at": changed_at,
            "completed_at": changed_at,
        }
    )
    result, _ = store.replace_simulation_if_status(
        candidate,
        expected_status=ComparisonRunLifecycleStatus.RUNNING,
    )
    return result


SIMULATION_CANCELLED_ERROR_CODE = "simulation_cancelled"


def cancellation(reason: str) -> Stage12FailureDetail:
    """Describe a child the coordinator stopped; ``reason`` ends the summary."""
    return Stage12FailureDetail(
        error_code=SIMULATION_CANCELLED_ERROR_CODE,
        error_summary=f"Cancelled after {reason}.",
    )


def cancel_pending_simulations(
    *,
    calls: dict[str, ChildCall],
    simulations: dict[str, PlannedSimulationExecutionInput],
    roles: set[str],
    store: ComparisonStore,
    detail: Stage12FailureDetail,
) -> None:
    """Record each started, unfinished child as failed, then stop it.

    Only a running record changes, so a child that has already succeeded or
    failed keeps its own outcome. An unsegmented child is always cancelled
    through Modal, even if its record could not be written. State-update and
    cancellation errors are logged rather than raised, so they never replace
    the failure that caused the cancellation.
    """
    for role in sorted(roles):
        simulation = simulations[role]
        try:
            _replace_running_simulation(
                store=store,
                simulation=simulation,
                status=ComparisonRunLifecycleStatus.FAILED,
                detail=detail,
            )
        except Exception as error:  # noqa: BLE001  # a failed cancellation write is logged and must not replace the failure that caused it
            _log_child_operation_failure(
                logging.ERROR,
                "Stage 12 could not record cancellation of %s simulation %s "
                "in report %s (%s)",
                simulation=simulation,
                error=error,
            )
            cancellation_recorded = False
        else:
            cancellation_recorded = True
        if cancellation_recorded and should_segment_simulation(simulation):
            # A segmented worker reads its record, cancels the region-group
            # calls it started, and exits through its own failure handling.
            # Cancelling it through Modal instead raises Modal's
            # InputCancellation in it, which also makes it cancel those calls
            # (Stage12SegmentRunner.run), but records nothing: that is the
            # fallback only when the record could not be written and the
            # worker cannot learn of the cancellation.
            continue
        cancel_child_call(calls[role], simulation)


def cancel_child_call(
    call: ChildCall,
    simulation: PlannedSimulationExecutionInput,
) -> None:
    """Ask Modal to stop one started child; log a failure rather than raise it.

    A cancellation error must never replace the failure that caused the
    cancellation.
    """
    try:
        call.cancel()
    except Exception as error:  # noqa: BLE001  # a failed Modal cancellation is logged and must not replace the failure that caused it
        _log_child_operation_failure(
            logging.WARNING,
            "Stage 12 could not cancel %s simulation %s in report %s (%s)",
            simulation=simulation,
            error=error,
        )


def simulation_failure_attributes(
    simulation: PlannedSimulationExecutionInput,
) -> dict[str, object]:
    """Return the telemetry attributes that identify a failed simulation."""
    return {
        "simulation_role": simulation.role.value,
        "evaluation_id": str(simulation.evaluation_id),
        "simulation_execution_id": str(simulation.simulation_execution_id),
    }


def _log_child_state_failure(
    *,
    simulation: PlannedSimulationExecutionInput,
    error: Exception,
) -> None:
    _log_child_operation_failure(
        logging.ERROR,
        "Stage 12 could not record the failure of %s simulation %s in report %s (%s)",
        simulation=simulation,
        error=error,
    )


def record_child_failure(
    *,
    store: ComparisonStore,
    simulation: PlannedSimulationExecutionInput,
    detail: Stage12FailureDetail,
) -> None:
    """Record a running child as failed with its own cause.

    A child that has already finished keeps its own outcome. A state error is
    logged rather than raised, so it never replaces the child's failure.
    """
    try:
        _replace_running_simulation(
            store=store,
            simulation=simulation,
            status=ComparisonRunLifecycleStatus.FAILED,
            detail=detail,
        )
    except Exception as state_error:  # noqa: BLE001  # a failed state write is logged and must not replace the child's failure
        _log_child_state_failure(simulation=simulation, error=state_error)


def report_child_wait_failure(
    error: Exception,
    *,
    simulation: PlannedSimulationExecutionInput,
    runtime: ObservabilityRuntime | None,
    progress: StageProgress,
) -> Stage12FailureDetail:
    """Report a failed child call once and return its redacted detail."""
    return report_failure(
        error,
        FailureSite(
            scope="child_wait",
            default_code="simulation_invocation_failed",
            attributes=simulation_failure_attributes(simulation),
        ),
        runtime=runtime,
        progress=progress,
    )


def child_failure_detail(
    *,
    error: Exception,
    simulation: PlannedSimulationExecutionInput,
    store: ComparisonStore,
    runtime: ObservabilityRuntime | None,
    progress: StageProgress,
) -> Stage12FailureDetail:
    """Report a failed child call and return the failure for its report.

    A failure the child's worker recorded itself takes precedence over the
    error its call raised. Reading or writing the child's record can fail,
    for example when the database is unavailable. That error is logged, and
    the call's own failure is used and still recorded where possible, so a
    state error never replaces the child's failure.
    """
    detail = report_child_wait_failure(
        error,
        simulation=simulation,
        runtime=runtime,
        progress=progress,
    )
    try:
        child = store.get_simulation(simulation.simulation_execution_id)
    except Exception as state_error:  # noqa: BLE001  # if the child's record cannot be read, its call failure is reported instead
        _log_child_state_failure(simulation=simulation, error=state_error)
    else:
        if (
            child.status is ComparisonRunLifecycleStatus.FAILED
            and child.error_code is not None
        ):
            return failure_detail_from_record(
                error_code=child.error_code,
                error_summary=child.error_summary,
            )
    record_child_failure(store=store, simulation=simulation, detail=detail)
    return detail


def child_record(
    *,
    simulation: PlannedSimulationExecutionInput,
    context: Stage12InvocationContext,
    function_name: str,
) -> ComparisonSimulationRecord:
    """Return the pending child record for ``simulation`` in this invocation."""
    return ComparisonSimulationRecord(
        simulation_execution_id=simulation.simulation_execution_id,
        evaluation_id=simulation.evaluation_id,
        role=simulation.role,
        input_sha256=simulation_input_sha256(simulation),
        worker_version=context.worker_version,
        modal_application=context.modal_application,
        simulation_callable=function_name,
        version_manifest_sha256=context.version_manifest_sha256,
        status=ComparisonRunLifecycleStatus.PENDING,
        created_at=context.created_at,
        updated_at=context.created_at,
        retention_expires_at=context.retention_expires_at,
    )
