"""Coordination of a Stage 12 report's baseline and reform simulations.

coordinate_report runs one report. A detailed-save call names its report
run and passes no evaluation record; report_run_coordination runs it on the
run tables. A comparison-path call passes its evaluation record;
report_coordination, coordinated_simulations and parent_claim hold its
steps. This module re-exports their public names.
"""

from __future__ import annotations

from typing import Any

from policyengine_simulation_contract.stage12_execution import (
    ComparisonReportRecord,
    ReportExecutionInput,
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

from .child_wait import (
    SIMULATION_POLL_BACKOFF_FACTOR,
    SIMULATION_POLL_INITIAL_SECONDS,
    SIMULATION_POLL_MAX_SECONDS,
)
from .coordinated_simulations import (
    SIMULATION_CANCELLED_ERROR_CODE,
    logger,
)
from .dependencies import ModalChildInvoker, artifact_store, runtime_store
from .failure_causes import StageProgress
from .parent_claim import (
    validate_dispatch_context,
    validate_submitted_parent,
)
from .report_claims import claim_report
from .report_coordination import (
    CoordinatorDependencies,
    CoordinatorInvocation,
    ReportCoordination,
)
from .report_run_coordination import coordinate_report_run

__all__ = [
    "SIMULATION_CANCELLED_ERROR_CODE",
    "SIMULATION_POLL_BACKOFF_FACTOR",
    "SIMULATION_POLL_INITIAL_SECONDS",
    "SIMULATION_POLL_MAX_SECONDS",
    "SIMULATION_WAIT_TIMEOUT_SECONDS",
    "CoordinatorDependencies",
    "CoordinatorInvocation",
    "coordinate_report",
    "logger",
]


def coordinate_report(
    payload: object,
    context_payload: object,
    parent_payload: object | None = None,
    *,
    invocation: CoordinatorInvocation,
    dependencies: CoordinatorDependencies | None = None,
) -> dict[str, Any]:
    """Run one Stage 12 report end to end; return its outcome.

    A detailed-save call's context names its report run, and it passes no
    evaluation record: ``coordinate_report_run`` claims and records that
    run on the run tables alone. A comparison-path call passes its
    evaluation record, and this claims it (one already running or finished
    returns a ``deduplicated`` summary instead), starts or reattaches each
    child within one shared wait window, aggregates the digest-checked
    outputs, compares a shadow run with the production result, and returns
    the aggregate's descriptor. A comparison dispatch context that disagrees
    with the report raises ``ValueError`` before the claim; any later
    failure stops the unfinished children, marks the report FAILED and
    raises a ``RuntimeError`` that carries no calculation detail.
    """
    dependencies = dependencies or CoordinatorDependencies()
    context = Stage12InvocationContext.model_validate(context_payload)
    if context.simulation_run_id is not None:
        msg = "a coordinator's call names a report run, not a simulation run"
        raise ValueError(msg)
    if context.report_run is not None:
        if parent_payload is not None:
            msg = "a report run's call passes no evaluation record"
            raise ValueError(msg)
        return coordinate_report_run(
            payload, context, invocation=invocation, dependencies=dependencies
        )
    if parent_payload is None:
        msg = "a comparison call needs its evaluation record"
        raise ValueError(msg)
    return _coordinate_comparison(
        payload,
        context,
        parent_payload,
        invocation=invocation,
        dependencies=dependencies,
    )


def _coordinate_comparison(
    payload: object,
    context: Stage12InvocationContext,
    parent_payload: object,
    *,
    invocation: CoordinatorInvocation,
    dependencies: CoordinatorDependencies,
) -> dict[str, Any]:
    """Run one comparison-path report on its evaluation records."""
    # Open the shared child-wait window before any setup work.
    child_wait_deadline = dependencies.monotonic() + dependencies.wait_timeout_seconds
    report = ReportExecutionInput.model_validate(payload)
    submitted_parent = ComparisonReportRecord.model_validate(parent_payload)
    validate_dispatch_context(report, context, invocation.application_name)
    validate_submitted_parent(
        report=report,
        context=context,
        parent=submitted_parent,
    )
    persistence = dependencies.store or runtime_store(dependencies.runtime)
    coordination = ReportCoordination(
        report=report,
        context=context,
        invocation=invocation,
        persistence=persistence,
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
        parent, summary = claim_report(coordination, submitted=submitted_parent)
    if parent is None:
        return summary or {}
    try:
        return coordination.run(parent)
    # Persist a redacted cause for any coordinator, child-call, aggregation, or
    # artifact failure before returning a stable exception to Modal.
    except Exception as error:  # noqa: BLE001  # every failure is persisted before a stable exception returns to Modal
        coordination.fail(parent, error)
        msg = "Stage 12 report coordination failed"
        raise RuntimeError(msg) from None
    # A pre-emption or cancellation records nothing: a comparison run's
    # restarted call reattaches to its running children.
