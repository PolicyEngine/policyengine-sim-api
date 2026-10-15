"""Coordination of a Stage 12 report's baseline and reform simulations."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from functools import partial
from hashlib import sha256
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from policyengine_simulation_contract.stage12_execution import (
    AggregateReportArtifactDescriptor,
    ComparisonReportRecord,
    ComparisonRunAggregationStatus,
    ComparisonRunLifecycleStatus,
    ComparisonSimulationRecord,
    PlannedSimulationExecutionInput,
    ReportExecutionInput,
    ResultComparisonStatus,
    SimulationArtifactDescriptor,
    SimulationRole,
    Stage12InvocationContext,
    Stage12OutputPlan,
)
from policyengine_simulation_observability.stages import (
    STAGE12_CANONICAL_REPORT_STAGES,
    STAGE12_SHADOW_REPORT_STAGES,
    Stage,
    StagePlan,
)

from policyengine_simulation_executor.stage12_artifacts import (
    Stage12ArtifactStore,
    deserialize_calculation_provenance,
    deserialize_simulation_frames,
    deserialize_uk_local_authority_metadata,
)

from .aggregation import (
    RetainedSimulationOutput,
    build_aggregate_report,
    validate_aligned_outputs,
    validate_uk_local_authority_metadata,
)
from .comparison import ComparisonServices, compare_completed_report
from .dependencies import (
    ChildCall,
    ChildInvoker,
    ChildSpawnRequest,
    ComparisonStore,
    ModalChildInvoker,
    artifact_store,
    runtime_store,
)
from .failure_causes import (
    StageProgress,
    StageTracking,
    failure_summary,
    record_failure_cause,
    tracked_stage,
)
from .failure_recording import persist_failure_record, report_original_failure
from .output_planning import (
    plan_simulation_input,
    resolve_report_output_plan,
    validate_output_frames,
)
from .simulation import descriptor_from_record, simulation_input_sha256

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from contextlib import AbstractContextManager

    from policyengine_observability import ObservabilityRuntime
    from policyengine_simulation_contract.uk_geography import UKLocalAuthorityMetadata

SIMULATION_WAIT_TIMEOUT_SECONDS = 3_000


def _child_record(
    *,
    simulation: PlannedSimulationExecutionInput,
    context: Stage12InvocationContext,
    function_name: str,
) -> ComparisonSimulationRecord:
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


def _validate_dispatch_context(
    report: ReportExecutionInput,
    context: Stage12InvocationContext,
    application_name: str,
) -> None:
    """Reject a dispatch context naming another application, worker or bundle."""
    if application_name != context.modal_application:
        msg = "report coordinator application differs from dispatch context"
        raise ValueError(msg)
    if report.baseline.bundle.policyengine_version != context.worker_version:
        msg = "report worker version differs from dispatch context"
        raise ValueError(msg)
    if report.baseline.bundle.bundle_manifest_sha256 != context.bundle_manifest_sha256:
        msg = "report bundle digest differs from dispatch context"
        raise ValueError(msg)


def _validate_submitted_parent(
    *,
    report: ReportExecutionInput,
    context: Stage12InvocationContext,
    parent: ComparisonReportRecord,
) -> None:
    """Reject dispatch metadata that does not describe this exact report."""
    _validate_parent_matches_invocation(report=report, context=context, parent=parent)
    _validate_parent_is_unclaimed(parent)
    _validate_parent_production_link(report=report, context=context, parent=parent)


def _validate_parent_matches_invocation(
    *,
    report: ReportExecutionInput,
    context: Stage12InvocationContext,
    parent: ComparisonReportRecord,
) -> None:
    """Require every parent field the dispatch fixes to match the report and context."""
    bundle = report.baseline.bundle
    expected = {
        "evaluation_id": report.evaluation_id,
        "environment": context.environment,
        "originating_request_id": context.request_id,
        "worker_version": context.worker_version,
        "modal_application": context.modal_application,
        "version_manifest_sha256": context.version_manifest_sha256,
        "policyengine_version": bundle.policyengine_version,
        "country_package_name": bundle.country_package_name,
        "country_package_version": bundle.country_package_version,
        "country": report.baseline.geography.country,
        "dataset_identity": bundle.dataset.identity,
        "dataset_uri": bundle.dataset.uri,
        "data_package_name": bundle.dataset.data_package_name,
        "data_package_version": bundle.dataset.data_package_version,
        "data_artifact_revision": bundle.dataset.artifact_revision,
        "created_at": context.created_at,
        "retention_expires_at": context.retention_expires_at,
    }
    for field, value in expected.items():
        if getattr(parent, field) != value:
            msg = f"submitted parent {field} does not match invocation"
            raise ValueError(msg)


def _validate_parent_is_unclaimed(parent: ComparisonReportRecord) -> None:
    """Require a pending parent that no coordinator has started."""
    if parent.status is not ComparisonRunLifecycleStatus.PENDING:
        msg = "submitted parent must be pending"
        raise ValueError(msg)
    if parent.aggregation_status is not ComparisonRunAggregationStatus.NOT_STARTED:
        msg = "submitted parent aggregation must not be started"
        raise ValueError(msg)
    if parent.coordinator_invocation_id is not None:
        msg = "submitted parent must not contain an invocation identifier"
        raise ValueError(msg)


def _validate_parent_production_link(
    *,
    report: ReportExecutionInput,
    context: Stage12InvocationContext,
    parent: ComparisonReportRecord,
) -> None:
    """Require a direct parent to stand alone and a shadow parent to name its job."""
    production_job_id = context.production_function_call_id
    if production_job_id is None:
        if parent.production_identity != f"direct:{report.evaluation_id}":
            msg = "direct parent identity does not match its report"
            raise ValueError(msg)
        if parent.incumbent_execution_id is not None:
            msg = "direct parent must not name a production execution"
            raise ValueError(msg)
        if parent.comparison_status is not ResultComparisonStatus.NOT_REQUESTED:
            msg = "direct parent must not request result comparison"
            raise ValueError(msg)
    elif (
        parent.production_identity != production_job_id
        or parent.incumbent_execution_id != production_job_id
    ):
        msg = "automatic parent does not match the production execution"
        raise ValueError(msg)


def _claim_parent(
    *,
    submitted: ComparisonReportRecord,
    coordinator_invocation_id: str,
    store: ComparisonStore,
) -> tuple[ComparisonReportRecord, bool]:
    """Create the parent in Modal or claim a retry; reject duplicate execution."""
    coordinating_at = datetime.now(UTC)
    candidate = submitted.model_copy(
        update={
            "status": ComparisonRunLifecycleStatus.RUNNING,
            "aggregation_status": ComparisonRunAggregationStatus.RUNNING,
            "coordinator_invocation_id": coordinator_invocation_id,
            "error_code": None,
            "error_summary": None,
            "started_at": coordinating_at,
            "updated_at": coordinating_at,
            "completed_at": None,
        }
    )
    persistence = store.create_or_resolve_report(candidate)
    parent = persistence.record
    if persistence.created:
        return parent, True
    if parent.evaluation_id != submitted.evaluation_id:
        msg = "existing comparison identity has a different report identifier"
        raise ValueError(msg)
    if parent.status in {
        ComparisonRunLifecycleStatus.RUNNING,
        ComparisonRunLifecycleStatus.SUCCEEDED,
        ComparisonRunLifecycleStatus.SKIPPED,
    }:
        return parent, False
    claimed = store.replace_report(
        parent.model_copy(
            update={
                "status": ComparisonRunLifecycleStatus.RUNNING,
                "aggregation_status": ComparisonRunAggregationStatus.RUNNING,
                "coordinator_invocation_id": coordinator_invocation_id,
                "error_code": None,
                "error_summary": None,
                "started_at": parent.started_at or coordinating_at,
                "updated_at": coordinating_at,
                "completed_at": None,
            }
        )
    )
    return claimed, True


def _mark_child_failed(
    persistence: ComparisonStore,
    simulation_execution_id: UUID,
    *,
    error_code: str,
    error_summary: str,
) -> str | None:
    """Mark an unfinished child failed and return the cause its record keeps.

    A worker that failed has already persisted its own redacted cause before
    Modal returned the worker's stable exception, so that cause is kept rather
    than overwritten. A succeeded child is left unchanged and has no cause.
    """
    latest = persistence.get_simulation(simulation_execution_id)
    if latest.status is ComparisonRunLifecycleStatus.SUCCEEDED:
        return None
    if (
        latest.status is ComparisonRunLifecycleStatus.FAILED
        and latest.error_summary is not None
    ):
        return latest.error_summary
    failed_at = datetime.now(UTC)
    persistence.replace_simulation(
        latest.model_copy(
            update={
                "status": ComparisonRunLifecycleStatus.FAILED,
                "error_code": error_code,
                "error_summary": error_summary,
                "updated_at": failed_at,
                "completed_at": failed_at,
            }
        )
    )
    return error_summary


def _require_artifact_digest(
    payload: bytes,
    expected_sha256: str,
    *,
    mismatch_message: str,
) -> None:
    """Raise ``ValueError(mismatch_message)`` unless ``payload`` matches its digest."""
    if sha256(payload).hexdigest() != expected_sha256:
        raise ValueError(mismatch_message)


@dataclass(frozen=True)
class CoordinatorInvocation:
    """The deployed coordinator call running a report: its Modal app and call ID."""

    application_name: str
    invocation_id: str


@dataclass(frozen=True)
class CoordinatorDependencies:
    """Seams the coordinator uses; an unset seam selects the deployed default.

    The defaults are the Stage 12 database store, the private artifact
    bucket, Modal child invocation, the standard aggregate builder and output
    plan resolution, and no telemetry runtime.
    """

    store: ComparisonStore | None = None
    artifacts: Stage12ArtifactStore | None = None
    invoker: ChildInvoker | None = None
    aggregator: Callable[..., dict[str, Any]] = build_aggregate_report
    output_plan_resolver: Callable[[ReportExecutionInput], Stage12OutputPlan] | None = (
        None
    )
    runtime: ObservabilityRuntime | None = None


@dataclass
class _ReportCoordination:
    """One claimed report's inputs and resolved seams, run through each phase."""

    report: ReportExecutionInput
    context: Stage12InvocationContext
    invocation: CoordinatorInvocation
    persistence: ComparisonStore
    artifact_storage: Stage12ArtifactStore
    child_invoker: ChildInvoker
    dependencies: CoordinatorDependencies
    stage_plan: StagePlan
    # The last stage entered and whether it finished, for failure causes.
    progress: StageProgress
    # A child failure is reported where it occurs and then propagates to the
    # report handler, which must not report the same error twice.
    reported_child_failure: Exception | None = None
    # A failed child records its own cause; the report keeps that cause
    # rather than the generic exception Modal returns to the coordinator.
    child_failure_summary: str | None = None

    @property
    def runtime(self) -> ObservabilityRuntime | None:
        """The telemetry runtime, or None when the coordinator records nothing."""
        return self.dependencies.runtime

    @property
    def stage_tracking(self) -> StageTracking:
        """How this report's stages are named, recorded and timed."""
        return StageTracking(self.stage_plan, self.progress, self.runtime)

    def span(
        self,
        stage: Stage,
        attributes: Mapping[str, object] | None = None,
    ) -> AbstractContextManager[object]:
        """Track ``stage`` for failure causes and time it as a span."""
        return tracked_stage(stage, attributes, tracking=self.stage_tracking)

    def stage_scope(
        self,
        stage: Stage,
        attributes: Mapping[str, object] | None = None,
    ) -> AbstractContextManager[object]:
        """Track ``stage`` for failure causes and record it as an operation."""
        return tracked_stage(
            stage, attributes, tracking=self.stage_tracking, operation=True
        )

    def run(self, parent: ComparisonReportRecord) -> dict[str, Any]:
        """Plan, start and await both children, then aggregate and complete."""
        output_plan, planned_simulations = self._prepare(parent)
        children = self._create_children(planned_simulations)
        descriptors, calls = self._start_children(planned_simulations, children)
        # Every required call has been started before the coordinator waits.
        self._wait_for_children(calls, descriptors, planned_simulations)
        return self._aggregate_and_complete(parent, output_plan, descriptors)

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
            with self.stage_scope(
                Stage.STAGE12_OUTPUT_PLANNING,
                {"country": self.report.baseline.geography.country},
            ):
                resolver = self.dependencies.output_plan_resolver
                if resolver is None:
                    output_plan = resolve_report_output_plan(
                        self.report,
                        stage_scope=self.stage_scope,
                    )
                else:
                    output_plan = resolver(self.report)
            simulations = []
            for simulation in (self.report.baseline, self.report.reform):
                with self.stage_scope(
                    Stage.STAGE12_CHILD_INPUT_PLANNING,
                    {"simulation_role": simulation.role.value},
                ):
                    simulations.append(plan_simulation_input(simulation, output_plan))
        return output_plan, tuple(simulations)

    def _create_children(
        self, planned_simulations: tuple[PlannedSimulationExecutionInput, ...]
    ) -> dict[SimulationRole, ComparisonSimulationRecord]:
        """Create each child's row, or resolve the row an earlier attempt made."""
        with self.span(Stage.STAGE12_CHILD_STATE_CREATE):
            return {
                simulation.role: self.persistence.create_or_resolve_simulation(
                    _child_record(
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
        descriptors: dict[str, SimulationArtifactDescriptor] = {}
        calls: dict[str, ChildCall] = {}
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
        _require_artifact_digest(
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

        A spawn failure is reported, marks the child failed (unless it already
        succeeded) and re-raises.
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
            with self.span(
                Stage.STAGE12_CHILD_DISPATCH,
                {"simulation_role": simulation.role.value},
            ):
                child_observability_context = (
                    self.runtime.capture_context() if self.runtime is not None else None
                )
                call = self.child_invoker.spawn(
                    ChildSpawnRequest(
                        application_name=self.invocation.application_name,
                        function_name=self.context.simulation_callable,
                        environment=self.context.modal_environment,
                        simulation=simulation.model_dump(mode="json"),
                        context=self.context.model_dump(mode="json"),
                        observability_context=child_observability_context,
                    )
                )
        except Exception as error:
            self._record_child_failure(
                simulation,
                error,
                failure_scope="child_dispatch",
                error_code="simulation_dispatch_failed",
            )
            raise
        self.persistence.attach_simulation_invocation(
            simulation.simulation_execution_id,
            expected_placeholder=dispatch_placeholder,
            modal_invocation_id=call.object_id,
            updated_at=datetime.now(UTC),
        )
        return call

    def _wait_for_children(
        self,
        calls: dict[str, ChildCall],
        descriptors: dict[str, SimulationArtifactDescriptor],
        planned_simulations: tuple[PlannedSimulationExecutionInput, ...],
    ) -> None:
        """Await each started child's descriptor.

        A failure is reported and marks that child failed before it is re-raised.
        """
        for role, call in calls.items():
            try:
                with self.span(Stage.STAGE12_CHILD_WAIT, {"simulation_role": role}):
                    descriptors[role] = SimulationArtifactDescriptor.model_validate(
                        call.get(timeout=SIMULATION_WAIT_TIMEOUT_SECONDS)
                    )
            except Exception as error:  # noqa: PERF203  # per child: the failing child is marked failed before the error is re-raised
                simulation = next(
                    item for item in planned_simulations if item.role.value == role
                )
                self.child_failure_summary = self._record_child_failure(
                    simulation,
                    error,
                    failure_scope="child_wait",
                    error_code="simulation_invocation_failed",
                )
                raise

    def _record_child_failure(
        self,
        simulation: PlannedSimulationExecutionInput,
        error: Exception,
        *,
        failure_scope: str,
        error_code: str,
    ) -> str | None:
        """Report a child failure, then try to mark the child failed with its cause.

        The original failure reaches telemetry before the database write, which
        may itself fail without replacing it; a succeeded child is left as is.
        Returns the redacted cause the child's record keeps, or ``None``.
        """
        report_original_failure(
            error,
            runtime=self.runtime,
            failure_scope=failure_scope,
            attributes={"simulation_role": simulation.role.value},
        )
        self.reported_child_failure = error
        return persist_failure_record(
            partial(
                _mark_child_failed,
                self.persistence,
                simulation.simulation_execution_id,
                error_code=error_code,
                error_summary=failure_summary(error, progress=self.progress),
            ),
            runtime=self.runtime,
            failure_scope=failure_scope,
        )

    def _read_retained_outputs(
        self,
        output_plan: Stage12OutputPlan,
        baseline: SimulationArtifactDescriptor,
        reform: SimulationArtifactDescriptor,
    ) -> tuple[
        RetainedSimulationOutput,
        RetainedSimulationOutput,
        UKLocalAuthorityMetadata | None,
    ]:
        """Read, digest-check and validate both children's retained artifacts."""
        validate_aligned_outputs(self.report, output_plan, baseline, reform)
        with self.span(Stage.STAGE12_ARTIFACT_READ):
            baseline_payload = self.artifact_storage.read(baseline.artifact.uri)
            reform_payload = self.artifact_storage.read(reform.artifact.uri)
        _require_artifact_digest(
            baseline_payload,
            baseline.artifact.content_sha256,
            mismatch_message="baseline artifact digest mismatch",
        )
        _require_artifact_digest(
            reform_payload,
            reform.artifact.content_sha256,
            mismatch_message="reform artifact digest mismatch",
        )
        baseline_frames = deserialize_simulation_frames(baseline_payload)
        reform_frames = deserialize_simulation_frames(reform_payload)
        uk_local_authority_metadata = validate_uk_local_authority_metadata(
            self.report.baseline.geography.country,
            deserialize_uk_local_authority_metadata(baseline_payload),
            deserialize_uk_local_authority_metadata(reform_payload),
            region_code=self.report.baseline.geography.region,
        )
        validate_output_frames(baseline_frames, output_plan)
        validate_output_frames(reform_frames, output_plan)
        return (
            RetainedSimulationOutput(baseline_frames, baseline),
            RetainedSimulationOutput(reform_frames, reform),
            uk_local_authority_metadata,
        )

    def _aggregate_and_complete(
        self,
        parent: ComparisonReportRecord,
        output_plan: Stage12OutputPlan,
        descriptors: dict[str, SimulationArtifactDescriptor],
    ) -> dict[str, Any]:
        """Aggregate both outputs, store the aggregate and complete the parent.

        A shadow run's result is then compared with the production result.
        """
        baseline, reform, uk_local_authority_metadata = self._read_retained_outputs(
            output_plan, descriptors["baseline"], descriptors["reform"]
        )
        with self.span(Stage.STAGE12_AGGREGATION):
            aggregate = self.dependencies.aggregator(
                report=self.report,
                baseline=baseline,
                reform=reform,
                uk_local_authority_metadata=uk_local_authority_metadata,
            )
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
    reform child simulations, and aggregates their digest-checked artifacts;
    shadow runs are then compared with the production result. A dispatch
    context that disagrees with the report raises ``ValueError`` before the
    claim; any later failure marks the report FAILED and raises a
    ``RuntimeError`` that carries no calculation detail.
    """
    dependencies = dependencies or CoordinatorDependencies()
    report = ReportExecutionInput.model_validate(payload)
    context = Stage12InvocationContext.model_validate(context_payload)
    submitted_parent = ComparisonReportRecord.model_validate(parent_payload)
    _validate_dispatch_context(report, context, invocation.application_name)
    _validate_submitted_parent(
        report=report,
        context=context,
        parent=submitted_parent,
    )
    coordination = _ReportCoordination(
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
    )
    with coordination.span(Stage.STAGE12_COORDINATOR_CLAIM):
        parent, should_execute = _claim_parent(
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
        if error is not coordination.reported_child_failure:
            report_original_failure(
                error,
                runtime=dependencies.runtime,
                failure_scope="report_coordination",
            )
        report_failure_summary = coordination.child_failure_summary or (
            failure_summary(error, progress=coordination.progress)
        )
        record_failure_cause(dependencies.runtime, error, report_failure_summary)
        failed_at = datetime.now(UTC)
        persist_failure_record(
            partial(
                coordination.persistence.replace_report,
                parent.model_copy(
                    update={
                        "status": ComparisonRunLifecycleStatus.FAILED,
                        "aggregation_status": ComparisonRunAggregationStatus.FAILED,
                        "error_code": "report_coordination_failed",
                        "error_summary": report_failure_summary,
                        "updated_at": failed_at,
                        "completed_at": failed_at,
                    }
                ),
            ),
            runtime=dependencies.runtime,
            failure_scope="report_coordination",
        )
        msg = "Stage 12 report coordination failed"
        raise RuntimeError(msg) from None
