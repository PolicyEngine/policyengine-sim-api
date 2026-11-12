"""One claimed comparison-path report's coordination on its evaluation records.

The comparison path's coordinator creates an evaluation child record for
each simulation, starts or reattaches its worker, then aggregates both
outputs, stores the aggregate and completes its evaluation record. A
detailed-save report run is coordinated on the run tables instead
(``report_run_coordination``).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from functools import partial
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from policyengine_simulation_contract.stage12_execution import (
    AggregateReportArtifactDescriptor,
    ComparisonReportRecord,
    ComparisonRunAggregationStatus,
    ComparisonRunLifecycleStatus,
    ComparisonSimulationRecord,
    PlannedSimulationExecutionInput,
    SimulationArtifactDescriptor,
    SimulationRole,
    Stage12OutputPlan,
)
from policyengine_simulation_observability.stages import Stage

from policyengine_simulation_executor.stage12_artifacts import (
    deserialize_calculation_provenance,
)

from .child_wait import wait_for_children
from .comparison import ComparisonServices, compare_completed_report
from .coordinated_simulations import (
    cancel_pending_simulations,
    cancellation,
    child_failure_detail,
    child_record,
    record_child_failure,
    simulation_failure_attributes,
)
from .coordination_core import (
    CoordinationCore,
    CoordinatorDependencies,
    CoordinatorInvocation,
    require_artifact_digest,
)
from .failure_causes import record_failure_cause
from .failure_recording import (
    FailureSite,
    persist_failure_record,
    report_failure,
)
from .failures import Stage12ExecutionError, Stage12FailureDetail
from .simulation import descriptor_from_record

if TYPE_CHECKING:
    from .dependencies import ChildCall, ComparisonStore

__all__ = [
    "CoordinatorDependencies",
    "CoordinatorInvocation",
    "ReportCoordination",
]


@dataclass(kw_only=True)
class ReportCoordination(CoordinationCore):
    """One claimed comparison-path report, run through each phase."""

    persistence: ComparisonStore

    def run(self, parent: ComparisonReportRecord) -> dict[str, Any]:
        """Plan, start and await both outputs, then aggregate them."""
        output_plan, planned_simulations = self._prepare(parent)
        self.simulations = {
            simulation.role.value: simulation for simulation in planned_simulations
        }
        children = self._create_children(planned_simulations)
        descriptors, calls = self._start_children(planned_simulations, children)
        # Every required call has been started before the coordinator observes
        # baseline and reform together through nonblocking result probes.
        wait_for_children(self, calls, descriptors)
        return self._aggregate_and_complete(parent, output_plan, descriptors)

    def child_descriptor(
        self, role: str, result: object
    ) -> SimulationArtifactDescriptor:
        """Return the descriptor a finished child's call returned."""
        del role
        return SimulationArtifactDescriptor.model_validate(result)

    def child_failure(self, role: str, error: Exception) -> Stage12FailureDetail:
        """Report a failed child call; return its failure, as its record has it."""
        return child_failure_detail(
            error=error,
            simulation=self.simulations[role],
            store=self.persistence,
            runtime=self.runtime,
            progress=self.progress,
        )

    def cancel_children(self, roles: set[str], detail: Stage12FailureDetail) -> None:
        """Record each started, unfinished child of ``roles`` failed, then stop it."""
        cancel_pending_simulations(
            calls=self.calls,
            simulations=self.simulations,
            roles=roles,
            store=self.persistence,
            detail=detail,
        )

    def _prepare(
        self, parent: ComparisonReportRecord
    ) -> tuple[Stage12OutputPlan, tuple[PlannedSimulationExecutionInput, ...]]:
        """Bind the run's artifact prefix, then plan the outputs and child inputs."""
        with self.stage_scope(Stage.STAGE12_COORDINATOR_PREPARATION):
            self.context = self.context.model_copy(
                update={
                    "artifact_prefix": (
                        f"stage-12-runs/{parent.environment}/"
                        f"{parent.created_at:%Y}/{parent.created_at:%m}/"
                        f"{parent.evaluation_id}"
                    ),
                    "created_at": parent.created_at,
                    "retention_expires_at": parent.retention_expires_at,
                }
            )
            return self.plan_simulations()

    def _create_children(
        self, planned_simulations: tuple[PlannedSimulationExecutionInput, ...]
    ) -> dict[SimulationRole, ComparisonSimulationRecord]:
        """Create each child's row, or resolve the row an earlier attempt made."""
        with self.span(Stage.STAGE12_CHILD_STATE_CREATE):
            return {
                simulation.role: self.persistence.create_or_resolve_simulation(
                    child_record(
                        simulation=simulation,
                        context=self.context,
                        function_name=self.context.simulation_callable,
                    )
                ).record
                for simulation in planned_simulations
            }

    def _start_children(
        self,
        planned_simulations: tuple[PlannedSimulationExecutionInput, ...],
        children: dict[SimulationRole, ComparisonSimulationRecord],
    ) -> tuple[dict[str, SimulationArtifactDescriptor], dict[str, ChildCall]]:
        """Reuse finished children, reattach running ones and dispatch the rest."""
        descriptors = self.descriptors
        calls = self.calls
        for simulation in planned_simulations:
            child = children[simulation.role]
            role = simulation.role.value
            if child.status is ComparisonRunLifecycleStatus.SUCCEEDED:
                descriptors[role] = self._retained_descriptor(child, simulation)
            elif (
                child.status is ComparisonRunLifecycleStatus.RUNNING
                and child.modal_invocation_id is not None
                and not child.modal_invocation_id.startswith("dispatch-pending-")
            ):
                calls[role] = self.child_invoker.restore(child.modal_invocation_id)
            else:
                calls[role] = self._dispatch_child(child, simulation)
        return descriptors, calls

    def _retained_descriptor(
        self,
        child: ComparisonSimulationRecord,
        simulation: PlannedSimulationExecutionInput,
    ) -> SimulationArtifactDescriptor:
        """Describe a succeeded child's digest-checked retained artifact."""
        descriptor = descriptor_from_record(child, simulation)
        retained = self.artifact_storage.read(descriptor.artifact.uri)
        require_artifact_digest(
            retained,
            descriptor.artifact.content_sha256,
            mismatch_message="retained child artifact digest mismatch",
        )
        return descriptor.model_copy(
            update={
                "artifact": descriptor.artifact.model_copy(
                    update={"size_bytes": len(retained)}
                ),
                "calculation_provenance": (
                    deserialize_calculation_provenance(retained)
                ),
            }
        )

    def _dispatch_child(
        self,
        child: ComparisonSimulationRecord,
        simulation: PlannedSimulationExecutionInput,
    ) -> ChildCall:
        """Mark the child dispatching, spawn it, and attach its Modal call ID.

        A spawn failure is reported and recorded on the child (unless it already
        finished), every started peer is stopped, and Stage12ExecutionError is
        raised with the failure.
        """
        dispatching_at = datetime.now(UTC)
        dispatch_placeholder = f"dispatch-pending-{uuid4()}"
        self.persistence.replace_simulation(
            child.model_copy(
                update={
                    "status": ComparisonRunLifecycleStatus.RUNNING,
                    "modal_invocation_id": dispatch_placeholder,
                    "started_at": child.started_at or dispatching_at,
                    "updated_at": dispatching_at,
                    "error_code": None,
                    "error_summary": None,
                    "completed_at": None,
                }
            )
        )
        try:
            call = self.spawn_child(
                simulation,
                # A comparison run's children receive the context fields they
                # received before detailed-save runs existed.
                self.context.model_dump(
                    mode="json", exclude={"report_run", "simulation_run_id"}
                ),
            )
        except Exception as error:  # noqa: BLE001  # any dispatch failure is recorded on the child and stops its started peers
            detail = report_failure(
                error,
                FailureSite(
                    scope="child_dispatch",
                    default_code="simulation_dispatch_failed",
                    attributes=simulation_failure_attributes(simulation),
                ),
                runtime=self.runtime,
                progress=self.progress,
            )
            record_child_failure(
                store=self.persistence,
                simulation=simulation,
                detail=detail,
            )
            cancel_pending_simulations(
                calls=self.calls,
                simulations=self.simulations,
                roles=set(self.calls),
                store=self.persistence,
                detail=cancellation(f"the {simulation.role.value} simulation failed"),
            )
            raise Stage12ExecutionError(detail) from None
        # Record the call before attaching its ID, so a failed attach still
        # stops it.
        self.calls[simulation.role.value] = call
        self.persistence.attach_simulation_invocation(
            simulation.simulation_execution_id,
            expected_placeholder=dispatch_placeholder,
            modal_invocation_id=call.object_id,
            updated_at=datetime.now(UTC),
        )
        return call

    def _aggregate_and_complete(
        self,
        parent: ComparisonReportRecord,
        output_plan: Stage12OutputPlan,
        descriptors: dict[str, SimulationArtifactDescriptor],
    ) -> dict[str, Any]:
        """Aggregate both outputs, store the aggregate and complete the parent.

        A shadow run's result is then compared with the production result.
        """
        aggregate, baseline, reform = self.aggregate(output_plan, descriptors)
        with self.span(Stage.STAGE12_AGGREGATE_ARTIFACT_WRITE):
            aggregate_artifact = self.artifact_storage.write_aggregate(
                prefix=self.context.artifact_prefix,
                payload=aggregate,
            )
        descriptor = AggregateReportArtifactDescriptor(
            evaluation_id=self.report.evaluation_id,
            artifact=aggregate_artifact,
            baseline_artifact_sha256=baseline.descriptor.artifact.content_sha256,
            reform_artifact_sha256=reform.descriptor.artifact.content_sha256,
            bundle=self.report.baseline.bundle,
        )
        completed = datetime.now(UTC)
        completed_parent = self.persistence.replace_report(
            parent.model_copy(
                update={
                    "status": ComparisonRunLifecycleStatus.SUCCEEDED,
                    "aggregation_status": ComparisonRunAggregationStatus.SUCCEEDED,
                    "aggregate_output_uri": aggregate_artifact.uri,
                    "aggregate_output_sha256": aggregate_artifact.content_sha256,
                    "aggregate_schema_version": descriptor.aggregate_schema_version,
                    "updated_at": completed,
                    "completed_at": completed,
                }
            )
        )
        compare_completed_report(
            parent=completed_parent,
            aggregate=aggregate,
            context=self.context,
            services=ComparisonServices(
                store=self.persistence,
                artifacts=self.artifact_storage,
                invoker=self.child_invoker,
                runtime=self.runtime,
                stage_plan=self.stage_plan,
            ),
        )
        return descriptor.model_dump(mode="json")

    def fail(self, parent: ComparisonReportRecord, error: Exception) -> None:
        """Record a failed run once, stop its unfinished children, fail the parent.

        Child, dispatch and wait-timeout failures raise Stage12ExecutionError
        only after reporting the original failure and stopping their unfinished
        peers. Any other failure is reported here, once, before any database
        write. If it occurred after a child started, such as a failed
        placeholder write or invocation attach, every started, unfinished child
        is then stopped so none keeps running for a failed report.
        """
        if isinstance(error, Stage12ExecutionError):
            detail = error.detail
        else:
            detail = report_failure(
                error,
                FailureSite(
                    scope="report_coordination",
                    default_code="report_coordination_failed",
                    attributes={"evaluation_id": str(self.report.evaluation_id)},
                ),
                runtime=self.runtime,
                progress=self.progress,
            )
            cancel_pending_simulations(
                calls=self.calls,
                simulations=self.simulations,
                roles=set(self.calls) - set(self.descriptors),
                store=self.persistence,
                detail=cancellation("the coordinator failed"),
            )
        record_failure_cause(self.runtime, error, detail.error_summary)
        failed_at = datetime.now(UTC)
        persist_failure_record(
            partial(
                self.persistence.replace_report,
                parent.model_copy(
                    update={
                        "status": ComparisonRunLifecycleStatus.FAILED,
                        "aggregation_status": ComparisonRunAggregationStatus.FAILED,
                        "error_code": detail.error_code,
                        "error_summary": detail.error_summary,
                        "updated_at": failed_at,
                        "completed_at": failed_at,
                    }
                ),
            ),
            runtime=self.runtime,
            failure_scope="report_coordination",
        )
