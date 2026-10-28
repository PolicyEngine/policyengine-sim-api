"""Coordination of a report's baseline and reform simulations.

``ReportSimulationCoordination`` holds the steps every report coordinator
runs. ``coordinate_report`` runs a Stage 12 report and records each step in
the Stage 12 tables; ``returned_coordination`` runs the same steps for a
report whose result returns to its caller and records nothing.
report_coordination, coordinated_simulations and parent_claim hold the
steps, and this module re-exports their public names.
"""

from __future__ import annotations

from typing import Any

from policyengine_simulation_contract.stage12_execution import (
    ComparisonReportRecord,
    ReportExecutionInput,
    ReportResultDelivery,
    Stage12InvocationContext,
)
from policyengine_simulation_observability.stages import (
    STAGE12_CANONICAL_REPORT_STAGES,
    STAGE12_SHADOW_REPORT_STAGES,
    Stage,
)

from policyengine_simulation_executor.stage12_timeouts import (
    SIMULATION_WAIT_TIMEOUT_SECONDS,
)

from .coordinated_simulations import SIMULATION_CANCELLED_ERROR_CODE, logger
from .dependencies import ModalChildInvoker, artifact_store, runtime_store
from .failure_causes import StageProgress
from .parent_claim import (
    claim_parent,
    validate_dispatch_context,
    validate_submitted_parent,
)
from .report_coordination import (
    SIMULATION_POLL_BACKOFF_FACTOR,
    SIMULATION_POLL_INITIAL_SECONDS,
    SIMULATION_POLL_MAX_SECONDS,
    CoordinatorDependencies,
    CoordinatorInvocation,
    ReportCoordination,
    ReportSimulationCoordination,
)

__all__ = [
    "SIMULATION_CANCELLED_ERROR_CODE",
    "SIMULATION_POLL_BACKOFF_FACTOR",
    "SIMULATION_POLL_INITIAL_SECONDS",
    "SIMULATION_POLL_MAX_SECONDS",
    "SIMULATION_WAIT_TIMEOUT_SECONDS",
    "CoordinatorDependencies",
    "CoordinatorInvocation",
    "ReportSimulationCoordination",
    "coordinate_report",
    "logger",
    "validate_dispatch_context",
]


def coordinate_report(
    payload: object,
    context_payload: object,
    parent_payload: object,
    *,
    invocation: CoordinatorInvocation,
    dependencies: CoordinatorDependencies | None = None,
) -> dict[str, Any]:
    """Run one Stage 12 comparison report end to end; return its aggregate descriptor.

    Claims the parent report (one already running or finished returns a
    ``deduplicated`` summary instead), starts or reattaches the baseline and
    reform child simulations within one shared wait window, and aggregates
    their digest-checked artifacts; shadow runs are then compared with the
    production result. A dispatch context that disagrees with the report, or
    asks for the report to return to its caller, raises ``ValueError`` before
    the claim; any later failure stops the unfinished children, marks the
    report FAILED and raises a ``RuntimeError`` that carries no calculation
    detail.
    """
    dependencies = dependencies or CoordinatorDependencies()
    # Open the shared child-wait window before any setup work.
    child_wait_deadline = dependencies.monotonic() + dependencies.wait_timeout_seconds
    report = ReportExecutionInput.model_validate(payload)
    context = Stage12InvocationContext.model_validate(context_payload)
    if context.result_delivery is not ReportResultDelivery.STAGE12_DATABASE:
        msg = (
            "a report returned to its caller runs through coordinate_report_for_caller"
        )
        raise ValueError(msg)
    submitted_parent = ComparisonReportRecord.model_validate(parent_payload)
    validate_dispatch_context(report, context, invocation.application_name)
    validate_submitted_parent(
        report=report,
        context=context,
        parent=submitted_parent,
    )
    coordination = ReportCoordination(
        report=report,
        context=context,
        invocation=invocation,
        persistence=dependencies.store or runtime_store(dependencies.runtime),
        artifact_storage=dependencies.artifacts or artifact_store(),
        child_invoker=dependencies.invoker or ModalChildInvoker(),
        dependencies=dependencies,
        stage_plan=(
            STAGE12_SHADOW_REPORT_STAGES
            if context.production_function_call_id is not None
            else STAGE12_CANONICAL_REPORT_STAGES
        ),
        progress=StageProgress(),
        child_wait_deadline=child_wait_deadline,
    )
    with coordination.span(Stage.STAGE12_COORDINATOR_CLAIM):
        parent, should_execute = claim_parent(
            submitted=submitted_parent,
            coordinator_invocation_id=invocation.invocation_id,
            store=coordination.persistence,
        )
    if not should_execute:
        return {
            "deduplicated": True,
            "evaluation_id": str(parent.evaluation_id),
            "status": parent.status.value,
        }
    try:
        return coordination.run(parent)
    # Persist a redacted cause for any coordinator, child-call, aggregation, or
    # artifact failure before returning a stable exception to Modal.
    except Exception as error:  # noqa: BLE001  # every failure is persisted before a stable exception returns to Modal
        coordination.fail(parent, error)
        msg = "Stage 12 report coordination failed"
        raise RuntimeError(msg) from None
