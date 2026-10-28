"""A Stage 12 simulation's record: its claim, cancellation, success and failure.

The single-simulation worker (``simulation``) claims a planned simulation's
row before calculating it, checks the row for a cancellation while it runs,
and records the stored artifact or the redacted failure on it.
``descriptor_from_record`` rebuilds the artifact descriptor of a simulation
that already succeeded.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from functools import partial
from typing import TYPE_CHECKING

from policyengine_simulation_contract.stage12_execution import (
    ArtifactMediaType,
    ArtifactReference,
    ComparisonRunLifecycleStatus,
    ComparisonSimulationRecord,
    PlannedSimulationExecutionInput,
    RowIdentity,
    SimulationArtifactDescriptor,
    stage12_output_plan_sha256,
)

from .failure_causes import StageProgress, record_failure_cause
from .failure_recording import FailureSite, persist_failure_record, report_failure
from .failures import (
    Stage12CancellationError,
    Stage12ExecutionError,
    Stage12FailureDetail,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from uuid import UUID

    from policyengine_observability import ObservabilityRuntime

    from .dependencies import ComparisonStore

# Moved out of simulation.py; a failed cancellation check still logs under
# its name.
logger = logging.getLogger(
    "policyengine_simulation_executor.stage12_runtime.simulation"
)


def descriptor_from_record(
    child: ComparisonSimulationRecord,
    simulation: PlannedSimulationExecutionInput,
) -> SimulationArtifactDescriptor:
    """Rebuild the artifact descriptor of a child that already succeeded.

    ``size_bytes`` is 0 because the record does not store it; raises
    ``ValueError`` if the record lacks any output metadata.
    """
    output_uri = child.output_uri
    output_sha256 = child.output_sha256
    output_schema_version = child.output_schema_version
    row_identity_columns = child.row_identity_columns
    row_count = child.row_count
    row_identity_sha256 = child.row_identity_sha256
    if (
        output_uri is None
        or output_sha256 is None
        or output_schema_version != 1
        or row_identity_columns is None
        or row_count is None
        or row_identity_sha256 is None
    ):
        msg = "successful child record has incomplete output metadata"
        raise ValueError(msg)

    return SimulationArtifactDescriptor(
        evaluation_id=simulation.evaluation_id,
        simulation_execution_id=simulation.simulation_execution_id,
        role=simulation.role,
        artifact=ArtifactReference(
            uri=output_uri,
            media_type=ArtifactMediaType.PARQUET,
            content_sha256=output_sha256,
            size_bytes=0,
        ),
        output_schema_version=1,
        output_plan_sha256=stage12_output_plan_sha256(simulation.output_plan),
        row_identity=RowIdentity(
            identifier_columns=row_identity_columns,
            row_count=row_count,
            identity_sha256=row_identity_sha256,
        ),
        bundle=simulation.bundle,
    )


def _require_completion_recorded(*, replaced: bool) -> None:
    """Raise ``Stage12CancellationError`` when the completion write replaced nothing.

    The coordinator records a cancelled simulation as failed while it is still
    running, so a write that finds the record no longer running means this
    simulation was cancelled.
    """
    if not replaced:
        msg = "Stage 12 simulation was cancelled"
        raise Stage12CancellationError(msg)


def _mark_running_simulation_failed(
    persistence: ComparisonStore,
    simulation_execution_id: UUID,
    *,
    detail: Stage12FailureDetail,
    failed_at: datetime,
) -> None:
    """Record a failure only while the simulation is still running."""
    latest = persistence.get_simulation(simulation_execution_id)
    if latest.status is not ComparisonRunLifecycleStatus.RUNNING:
        return
    persistence.replace_simulation_if_status(
        latest.model_copy(
            update={
                "status": ComparisonRunLifecycleStatus.FAILED,
                "error_code": detail.error_code,
                "error_summary": detail.error_summary,
                "updated_at": failed_at,
                "completed_at": failed_at,
            }
        ),
        expected_status=ComparisonRunLifecycleStatus.RUNNING,
    )


def claim_simulation(
    persistence: ComparisonStore,
    child: ComparisonSimulationRecord,
) -> ComparisonSimulationRecord:
    """Mark an active simulation running and return its record, or refuse it.

    The claim is a conditional write, so a simulation whose record changed
    meanwhile, for example because the coordinator cancelled it, is not run.
    """
    if child.status not in {
        ComparisonRunLifecycleStatus.PENDING,
        ComparisonRunLifecycleStatus.RUNNING,
    }:
        msg = "Stage 12 simulation is no longer active"
        raise RuntimeError(msg)
    started = datetime.now(UTC)
    running = child.model_copy(
        update={
            "status": ComparisonRunLifecycleStatus.RUNNING,
            "started_at": child.started_at or started,
            "updated_at": started,
            "error_code": None,
            "error_summary": None,
        }
    )
    running, claimed = persistence.replace_simulation_if_status(
        running,
        expected_status=child.status,
    )
    if not claimed:
        msg = "Stage 12 simulation was cancelled"
        raise RuntimeError(msg)
    return running


def cancellation_check(
    persistence: ComparisonStore,
    simulation: PlannedSimulationExecutionInput,
) -> Callable[[], bool]:
    """Return a check of whether the coordinator has stopped this simulation."""

    def cancellation_requested() -> bool:
        try:
            latest = persistence.get_simulation(simulation.simulation_execution_id)
        except Exception as error:  # noqa: BLE001  # a failed status read is logged and treated as not cancelled, so the calculation continues
            logger.warning(
                "Stage 12 cancellation check failed for %s (%s)",
                simulation.simulation_execution_id,
                type(error).__name__,
            )
            return False
        return latest.status is not ComparisonRunLifecycleStatus.RUNNING

    return cancellation_requested


def record_success(
    persistence: ComparisonStore,
    running: ComparisonSimulationRecord,
    descriptor: SimulationArtifactDescriptor,
) -> None:
    """Record the stored artifact on the running simulation.

    Raises ``Stage12CancellationError`` when the coordinator stopped the
    simulation first.
    """
    completed = datetime.now(UTC)
    _, replaced = persistence.replace_simulation_if_status(
        running.model_copy(
            update={
                "status": ComparisonRunLifecycleStatus.SUCCEEDED,
                "output_uri": descriptor.artifact.uri,
                "output_sha256": descriptor.artifact.content_sha256,
                "output_schema_version": descriptor.output_schema_version,
                "row_identity_columns": descriptor.row_identity.identifier_columns,
                "row_count": descriptor.row_identity.row_count,
                "row_identity_sha256": descriptor.row_identity.identity_sha256,
                "updated_at": completed,
                "completed_at": completed,
            }
        ),
        expected_status=ComparisonRunLifecycleStatus.RUNNING,
    )
    _require_completion_recorded(replaced=replaced)


def simulation_failure_detail(
    error: Exception,
    simulation: PlannedSimulationExecutionInput,
    *,
    runtime: ObservabilityRuntime | None,
    progress: StageProgress,
) -> Stage12FailureDetail:
    """Report a calculation failure once and return its redacted detail."""
    if isinstance(error, Stage12ExecutionError):
        # A segmented calculation reports its segment failure where it
        # occurs, before raising this error.
        return error.detail
    return report_failure(
        error,
        FailureSite(
            scope="simulation_execution",
            default_code="simulation_execution_failed",
            attributes={
                "simulation_role": simulation.role.value,
                "evaluation_id": str(simulation.evaluation_id),
                "simulation_execution_id": str(simulation.simulation_execution_id),
            },
        ),
        runtime=runtime,
        progress=progress,
    )


def record_failure(
    error: Exception,
    simulation: PlannedSimulationExecutionInput,
    *,
    persistence: ComparisonStore,
    runtime: ObservabilityRuntime | None,
    progress: StageProgress,
) -> None:
    """Report a calculation failure once, then record it if still running."""
    detail = simulation_failure_detail(
        error,
        simulation,
        runtime=runtime,
        progress=progress,
    )
    record_failure_cause(runtime, error, detail.error_summary)
    failed_at = datetime.now(UTC)
    persist_failure_record(
        partial(
            _mark_running_simulation_failed,
            persistence,
            simulation.simulation_execution_id,
            detail=detail,
            failed_at=failed_at,
        ),
        runtime=runtime,
        failure_scope="simulation_execution",
    )
