"""Coordination of a Stage 12 report's baseline and reform simulations.

coordinate_report runs one report; report_coordination,
coordinated_simulations and parent_claim hold its steps, and this module
re-exports their public names.
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
    cancel_pending_simulations,
    cancellation,
    logger,
)
from .dependencies import ModalChildInvoker, artifact_store, runtime_store
from .failure_causes import StageProgress
from .failures import Stage12ExecutionError, Stage12FailureDetail
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
from .report_run_access import report_run_store
from .report_run_recording import ReportRunRecorder, SimulationRunRecordingStore

CANCELLED_DETAIL = Stage12FailureDetail(
    error_code="report_cancelled",
    error_summary="Modal cancelled the report's coordinator call.",
)

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
    parent_payload: object,
    *,
    invocation: CoordinatorInvocation,
    dependencies: CoordinatorDependencies | None = None,
) -> dict[str, Any]:
    """Run one Stage 12 comparison report end to end; return its aggregate descriptor.

    Claims the parent report (one already running or finished returns a
    ``deduplicated`` summary instead). A call that names a report run uses
    the stored output of each of its simulation runs that already succeeded;
    each other simulation's child is started or reattached within one shared
    wait window. The digest-checked baseline and reform outputs are then
    aggregated; shadow runs are compared with the production result. A
    dispatch context that disagrees with the report raises ``ValueError``
    before the claim; any later failure stops the unfinished children, marks
    the report FAILED and raises a ``RuntimeError`` that carries no
    calculation detail.
    """
    dependencies = dependencies or CoordinatorDependencies()
    # Open the shared child-wait window before any setup work.
    child_wait_deadline = dependencies.monotonic() + dependencies.wait_timeout_seconds
    report = ReportExecutionInput.model_validate(payload)
    context = Stage12InvocationContext.model_validate(context_payload)
    submitted_parent = ComparisonReportRecord.model_validate(parent_payload)
    validate_dispatch_context(report, context, invocation.application_name)
    validate_submitted_parent(
        report=report,
        context=context,
        parent=submitted_parent,
    )
    persistence = dependencies.store or runtime_store(dependencies.runtime)
    report_run = None
    if context.report_run is not None:
        report_run = ReportRunRecorder(
            writer=dependencies.report_runs or report_run_store(dependencies.runtime),
            run_ids=context.report_run,
            modal_call_id=invocation.invocation_id,
        )
        persistence = SimulationRunRecordingStore(
            persistence, report_run.writer, context.report_run
        )
    coordination = ReportCoordination(
        report=report,
        context=context,
        invocation=invocation,
        persistence=persistence,
        artifact_storage=dependencies.artifacts
        or artifact_store(content_addressed=report_run is not None),
        child_invoker=dependencies.invoker or ModalChildInvoker(),
        dependencies=dependencies,
        stage_plan=(
            STAGE12_SHADOW_REPORT_STAGES
            if context.production_function_call_id is not None
            else STAGE12_CANONICAL_REPORT_STAGES
        ),
        progress=StageProgress(),
        child_wait_deadline=child_wait_deadline,
        report_run=report_run,
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
        _fail_unless_cancelled(coordination, parent, error)
        msg = "Stage 12 report coordination failed"
        raise RuntimeError(msg) from None
    except KeyboardInterrupt:
        # Modal pre-empts a call with SIGINT and runs it again under the same
        # call ID. A report run's coordinator stops its unfinished children
        # and records nothing, so the restarted call starts them again.
        if report_run is not None:
            coordination.stop_children()
        raise
    except BaseException:
        # Modal cancels a call with InputCancellation, which is not an
        # Exception, and never runs it again: a report run's coordinator
        # records its report, report run and unfinished children as failed.
        if report_run is not None:
            _record_cancellation(coordination, parent)
        raise


def _fail_unless_cancelled(
    coordination: ReportCoordination,
    parent: ComparisonReportRecord,
    error: Exception,
) -> None:
    """Record ``error``; if Modal cancels the call meanwhile, still fail the run."""
    try:
        coordination.fail(parent, error)
    except KeyboardInterrupt:
        raise
    except BaseException:
        if coordination.report_run is not None:
            coordination.report_run.fail_quietly(
                CANCELLED_DETAIL, runtime=coordination.runtime
            )
        raise


def _record_cancellation(
    coordination: ReportCoordination, parent: ComparisonReportRecord
) -> None:
    """Stop and fail the unfinished children, then fail the report and its run."""
    cancel_pending_simulations(
        calls=coordination.calls,
        simulations=coordination.simulations,
        roles=set(coordination.calls) - set(coordination.descriptors),
        store=coordination.persistence,
        detail=cancellation("the coordinator's call was cancelled"),
    )
    coordination.fail(parent, Stage12ExecutionError(CANCELLED_DETAIL))
