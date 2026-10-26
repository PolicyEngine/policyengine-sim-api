"""Coordination of a report whose result returns to its caller.

The coordinator plans, starts, awaits and checks the report's baseline and
reform simulations with the same steps as the Stage 12 coordinator
(``ReportSimulationCoordination``), but reads and writes no Stage 12 rows. It
returns a ``ReportExecutionResult`` with the aggregate inline, or raises
``ReportExecutionError``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, override

from policyengine_simulation_contract.macro_output import SingleYearMacroOutput
from policyengine_simulation_contract.report_execution import (
    ReportExecutionError,
    ReportExecutionProvenance,
    ReportExecutionResult,
    report_aggregate_sha256,
)
from policyengine_simulation_contract.stage12_execution import (
    AggregateReportArtifactPayload,
    ReportExecutionInput,
    ReportResultDelivery,
    Stage12InvocationContext,
)
from policyengine_simulation_observability.stages import (
    STAGE12_CANONICAL_REPORT_STAGES,
    Stage,
)

from policyengine_simulation_executor.stage12_artifacts import comparison_run_prefix

from .coordination import (
    CoordinatorDependencies,
    CoordinatorInvocation,
    ReportSimulationCoordination,
    validate_dispatch_context,
)
from .dependencies import ModalChildInvoker, artifact_store
from .failure_causes import StageProgress, record_failure_cause
from .failures import failure_detail_from_record, report_execution_error

if TYPE_CHECKING:
    from policyengine_simulation_contract.stage12_execution import (
        PlannedSimulationExecutionInput,
        Stage12OutputPlan,
    )

    from .failures import Stage12FailureDetail


@dataclass(kw_only=True)
class _ReturnedReportCoordination(ReportSimulationCoordination):
    """One report whose result returns to its caller, run through each phase.

    No Stage 12 row exists for the report or its children, so nothing records
    their state: a failed child's own ``ReportExecutionError`` carries its
    failure, and a child that must stop is cancelled through Modal.
    """

    def run(self) -> ReportExecutionResult:
        """Plan, start and await both children, then aggregate and check the result."""
        with self.stage_scope(Stage.STAGE12_COORDINATOR_PREPARATION):
            self.context = self.context.model_copy(
                update={
                    "artifact_prefix": comparison_run_prefix(
                        environment=self.context.environment,
                        created_at=self.context.created_at,
                        evaluation_id=self.report.evaluation_id,
                    )
                }
            )
            output_plan, planned_simulations = self._plan_report_simulations()
        self.simulations = {
            simulation.role.value: simulation for simulation in planned_simulations
        }
        for simulation in planned_simulations:
            self._spawn_child(simulation)
        # Both children have started before the coordinator observes them
        # together through nonblocking result probes.
        self._wait_for_children(self.calls, self.descriptors)
        baseline, reform, uk_local_authority_metadata = self._read_retained_outputs(
            output_plan, self.descriptors["baseline"], self.descriptors["reform"]
        )
        with self.span(Stage.STAGE12_AGGREGATION):
            aggregate = self.dependencies.aggregator(
                report=self.report,
                baseline=baseline,
                reform=reform,
                uk_local_authority_metadata=uk_local_authority_metadata,
            )
            return self._result(output_plan, aggregate)

    def _result(
        self,
        output_plan: Stage12OutputPlan,
        aggregate: dict[str, Any],
    ) -> ReportExecutionResult:
        """Check the aggregate and build the result this report delivers.

        The aggregate passes the checks a Stage 12 aggregate artifact write
        applies, must describe this report, and its result must be a
        ``SingleYearMacroOutput``.
        """
        payload = AggregateReportArtifactPayload.model_validate(aggregate)
        if (
            payload.evaluation_id != self.report.evaluation_id
            or payload.bundle != self.report.baseline.bundle
            or payload.requested_aggregates != self.report.requested_aggregates
        ):
            msg = "aggregate does not describe this report"
            raise ValueError(msg)
        totals = SingleYearMacroOutput.model_validate(payload.result)
        return ReportExecutionResult(
            evaluation_id=self.report.evaluation_id,
            requested_aggregates=self.report.requested_aggregates,
            aggregate=totals,
            aggregate_sha256=report_aggregate_sha256(totals),
            output_plan=output_plan,
            baseline=self.descriptors["baseline"],
            reform=self.descriptors["reform"],
            provenance=ReportExecutionProvenance(
                bundle=self.report.baseline.bundle,
                worker_version=self.context.worker_version,
                modal_environment=self.context.modal_environment,
                modal_application=self.context.modal_application,
                simulation_callable=self.context.simulation_callable,
                version_manifest_sha256=self.context.version_manifest_sha256,
                coordinator_invocation_id=self.invocation.invocation_id,
            ),
            completed_at=datetime.now(UTC),
        )

    def fail(self, error: Exception) -> ReportExecutionError:
        """Return the typed error for a failed run, reporting the failure once.

        See ``_failure_detail`` for how the failure is reported and which
        children are stopped. The redacted failure is also recorded as the
        operation's cause.
        """
        detail = self._failure_detail(error)
        record_failure_cause(self.runtime, error, detail.error_summary)
        return report_execution_error(self.report.evaluation_id, detail)

    def stop_unfinished_children(self) -> None:
        """Cancel every started, unfinished child through Modal.

        The coordinator does this when it stops without a failure to report,
        as when Modal cancels its own input: Modal does not cancel the calls
        that input spawned.
        """
        self._stop_children(
            set(self.calls) - set(self.descriptors),
            reason="the coordinator stopped",
        )

    @override
    def _record_dispatch_failure(
        self,
        simulation: PlannedSimulationExecutionInput,
        detail: Stage12FailureDetail,
    ) -> None:
        """Record nothing: a returned report's children have no record."""

    @override
    def _child_call_failed(self, role: str, error: Exception) -> Stage12FailureDetail:
        """Report the failed call; the child's own ``ReportExecutionError`` wins.

        That error carries the failure its worker reported, as a Stage 12
        worker's recorded failure does.
        """
        detail = self._report_failed_child_call(role, error)
        if isinstance(error, ReportExecutionError):
            return failure_detail_from_record(
                error_code=error.failure.error_code,
                error_summary=error.failure.error_summary,
            )
        return detail

    @override
    def _stop_children(self, roles: set[str], *, reason: str) -> None:
        """Cancel each child through Modal: no record can tell it to stop.

        Modal raises its ``InputCancellation`` in the child, and a segmented US
        child then cancels the region-group segment calls it started
        (``Stage12SegmentRunner.run``), since Modal does not cancel them.
        """
        self._cancel_through_modal(roles)


def coordinate_report_for_caller(
    payload: object,
    context_payload: object,
    *,
    invocation: CoordinatorInvocation,
    dependencies: CoordinatorDependencies | None = None,
) -> dict[str, Any]:
    """Run one report whose result returns to its caller; return the result payload.

    The payload is a ``ReportExecutionResult`` in JSON mode. No Stage 12 row is
    read or written and ``dependencies.store`` is never used. Nothing claims
    the report, so a repeated dispatch runs it again, and the caller
    deduplicates. Artifacts go under the Stage 12 layout for the context's
    environment, creation time and report, whatever prefix the caller sent.
    Both children start with the same delivery, so they write no rows either,
    and each names its input and output by their digests. A repeated dispatch,
    or a call Modal restarts after pre-empting it, therefore stores its
    artifacts beside any an earlier attempt stored, even where it plans or
    calculates differently. No aggregate artifact is written: the aggregate is
    returned inline.

    A context that does not ask for the report to return to its caller, or
    that disagrees with the report, raises ``ValueError`` before any child
    starts. Any later failure stops the unfinished children through Modal, is
    reported once, and is raised as ``ReportExecutionError`` with the redacted
    code and summary a Stage 12 report row would record. When the coordinator
    stops on a ``BaseException`` that is not an ``Exception``, such as the
    ``InputCancellation`` Modal raises in a cancelled input, it cancels the
    unfinished children through Modal and re-raises it unreported.
    """
    dependencies = dependencies or CoordinatorDependencies()
    # Open the shared child-wait window before any setup work.
    child_wait_deadline = dependencies.monotonic() + dependencies.wait_timeout_seconds
    report = ReportExecutionInput.model_validate(payload)
    context = Stage12InvocationContext.model_validate(context_payload)
    if context.result_delivery is not ReportResultDelivery.RETURN_TO_CALLER:
        msg = "coordinate_report_for_caller requires result_delivery return_to_caller"
        raise ValueError(msg)
    validate_dispatch_context(report, context, invocation.application_name)
    coordination = _ReturnedReportCoordination(
        report=report,
        context=context,
        invocation=invocation,
        artifact_storage=dependencies.artifacts or artifact_store(),
        child_invoker=dependencies.invoker or ModalChildInvoker(),
        dependencies=dependencies,
        # A returned report cannot be compared with a production job.
        stage_plan=STAGE12_CANONICAL_REPORT_STAGES,
        progress=StageProgress(),
        child_wait_deadline=child_wait_deadline,
    )
    try:
        result = coordination.run()
    # Report a redacted cause for any coordinator, child-call, aggregation or
    # artifact failure, and return it to the caller as a typed error.
    except Exception as error:  # noqa: BLE001  # every failure reaches the caller as a typed error with its redacted detail
        raise coordination.fail(error) from None
    # Modal stops a cancelled coordinator by raising InputCancellation, a
    # BaseException, in it.
    except BaseException:
        coordination.stop_unfinished_children()
        raise
    return result.model_dump(mode="json")
