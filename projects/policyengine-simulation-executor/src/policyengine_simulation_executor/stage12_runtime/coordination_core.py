"""What every report coordinator does, whatever records its runs.

A comparison-path report records its runs on the comparison path's
evaluation records, and a detailed-save report run on the Public API's run
tables; both plan their simulations' outputs, start their workers, wait for
them, and aggregate the two outputs in the same way, with the seams and
state ``CoordinationCore`` holds.
"""

from __future__ import annotations

import dataclasses
import time
from dataclasses import dataclass
from hashlib import sha256
from typing import TYPE_CHECKING, Any

from policyengine_simulation_observability.stages import Stage, StagePlan

from policyengine_simulation_executor.stage12_artifacts import (
    DecodedSimulationOutput,
    Stage12ArtifactStore,
    decode_simulation_output,
)
from policyengine_simulation_executor.stage12_timeouts import (
    SIMULATION_WAIT_TIMEOUT_SECONDS,
)

from .aggregation import (
    RetainedSimulationOutput,
    build_aggregate_report,
    validate_aligned_outputs,
    validate_uk_local_authority_metadata,
)
from .dependencies import ChildCall, ChildInvoker, ChildSpawnRequest, ComparisonStore
from .failure_causes import StageProgress, StageTracking, tracked_stage
from .output_planning import (
    plan_simulation_input,
    resolve_report_output_plan,
    validate_output_frames,
    with_zero_labor_supply_responses,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from contextlib import AbstractContextManager

    from policyengine_observability import ObservabilityRuntime
    from policyengine_simulation_contract.stage12_execution import (
        PlannedSimulationExecutionInput,
        ReportExecutionInput,
        SimulationArtifactDescriptor,
        SimulationExecutionInput,
        Stage12InvocationContext,
        Stage12OutputPlan,
    )
    from policyengine_simulation_contract.uk_geography import UKLocalAuthorityMetadata

    from .report_run_access import ReportRunWriter
    from .reused_outputs import OutputFileReader, ReusedSimulationOutput
    from .run_sweep import CallState


def require_artifact_digest(
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
    plan resolution, no telemetry runtime, the real clock and sleep, the
    standard child wait window, and the report run store, the Cloud Storage
    reader of reused output files, the stored output plan of each
    simulation (``resolve_simulation_output_plan``) and Modal's state of a
    call whose probe failed (``modal_call_state``), which only a call that
    names a report run uses.
    """

    store: ComparisonStore | None = None
    artifacts: Stage12ArtifactStore | None = None
    invoker: ChildInvoker | None = None
    aggregator: Callable[..., dict[str, Any]] = build_aggregate_report
    output_plan_resolver: Callable[[ReportExecutionInput], Stage12OutputPlan] | None = (
        None
    )
    runtime: ObservabilityRuntime | None = None
    sleep: Callable[[float], None] = time.sleep
    monotonic: Callable[[], float] = time.monotonic
    wait_timeout_seconds: float = SIMULATION_WAIT_TIMEOUT_SECONDS
    report_runs: ReportRunWriter | None = None
    output_files: OutputFileReader | None = None
    stored_plan_resolver: (
        Callable[[SimulationExecutionInput], Stage12OutputPlan] | None
    ) = None
    call_state: Callable[[str], CallState] | None = None


@dataclass(kw_only=True)
class CoordinationCore:
    """One report's inputs, resolved seams and children, shared by both coordinators."""

    report: ReportExecutionInput
    context: Stage12InvocationContext
    invocation: CoordinatorInvocation
    artifact_storage: Stage12ArtifactStore
    child_invoker: ChildInvoker
    dependencies: CoordinatorDependencies
    stage_plan: StagePlan
    # The last stage entered and whether it finished, for failure causes.
    progress: StageProgress
    # The shared child-wait window closes at this monotonic time.
    child_wait_deadline: float
    # Each child by role: its plan, its started call and its descriptor once
    # finished, so a failure at any point can find the unfinished children.
    simulations: dict[str, PlannedSimulationExecutionInput] = dataclasses.field(
        default_factory=dict
    )
    calls: dict[str, ChildCall] = dataclasses.field(default_factory=dict)
    descriptors: dict[str, SimulationArtifactDescriptor] = dataclasses.field(
        default_factory=dict
    )
    # Each reused simulation run's checked output by role, in place of a child.
    reused: dict[str, ReusedSimulationOutput] = dataclasses.field(default_factory=dict)

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

    def plan_simulations(
        self,
    ) -> tuple[Stage12OutputPlan, tuple[PlannedSimulationExecutionInput, ...]]:
        """Plan the report's outputs, then each simulation's child input.

        The child's plan is ``child_output_plan``'s: the report's own plan
        unless a coordinator plans each simulation by itself.
        """
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
                simulations.append(
                    plan_simulation_input(
                        simulation, self.child_output_plan(simulation, output_plan)
                    )
                )
        return output_plan, tuple(simulations)

    def child_output_plan(
        self,
        simulation: SimulationExecutionInput,  # noqa: ARG002  # a coordinator that plans each simulation by itself reads it
        output_plan: Stage12OutputPlan,
    ) -> Stage12OutputPlan:
        """Return the plan ``simulation``'s child calculates: the report's own."""
        return output_plan

    def spawn_child(
        self,
        simulation: PlannedSimulationExecutionInput,
        context: Mapping[str, Any],
    ) -> ChildCall:
        """Start the simulation's worker with ``context``; return its call."""
        with self.span(
            Stage.STAGE12_CHILD_DISPATCH,
            {"simulation_role": simulation.role.value},
        ):
            child_observability_context = (
                self.runtime.capture_context() if self.runtime is not None else None
            )
            return self.child_invoker.spawn(
                ChildSpawnRequest(
                    application_name=self.invocation.application_name,
                    function_name=self.context.simulation_callable,
                    environment=self.context.modal_environment,
                    simulation=simulation.model_dump(mode="json"),
                    context=dict(context),
                    observability_context=child_observability_context,
                )
            )

    def aggregate(
        self,
        output_plan: Stage12OutputPlan,
        descriptors: Mapping[str, SimulationArtifactDescriptor],
    ) -> tuple[dict[str, Any], RetainedSimulationOutput, RetainedSimulationOutput]:
        """Read and check both outputs, then aggregate them into the report.

        Returns the aggregate and the baseline and reform outputs it read.
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
        return aggregate, baseline, reform

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
        """Read, digest-check and validate the baseline and reform outputs.

        A worker's artifact is read and decoded here; a reused output was
        read, checked and decoded while the report was prepared.
        """
        validate_aligned_outputs(
            self.report,
            baseline,
            reform,
            baseline_plan=self.simulations["baseline"].output_plan,
            reform_plan=self.simulations["reform"].output_plan,
        )
        # A report run naming one simulation run on both sides has one file.
        one_file = baseline.artifact == reform.artifact
        with self.span(Stage.STAGE12_ARTIFACT_READ):
            payloads = {
                descriptor.role.value: self.artifact_storage.read(
                    descriptor.artifact.uri
                )
                for descriptor in ((baseline,) if one_file else (baseline, reform))
                if descriptor.role.value not in self.reused
            }
        baseline_output = self._decoded_output(baseline, payloads)
        reform_output = (
            baseline_output if one_file else self._decoded_output(reform, payloads)
        )
        uk_local_authority_metadata = validate_uk_local_authority_metadata(
            self.report.baseline.geography.country,
            baseline_output.uk_local_authority_metadata,
            reform_output.uk_local_authority_metadata,
            region_code=self.report.baseline.geography.region,
        )
        # A side whose own plan stores no labour-supply responses the report
        # reads is read with them as zeros.
        baseline_frames = with_zero_labor_supply_responses(
            baseline_output.frames,
            output_plan,
            self.simulations["baseline"].output_plan,
        )
        reform_frames = with_zero_labor_supply_responses(
            reform_output.frames, output_plan, self.simulations["reform"].output_plan
        )
        validate_output_frames(baseline_frames, output_plan)
        validate_output_frames(reform_frames, output_plan)
        return (
            RetainedSimulationOutput(baseline_frames, baseline),
            RetainedSimulationOutput(reform_frames, reform),
            uk_local_authority_metadata,
        )

    def _decoded_output(
        self,
        descriptor: SimulationArtifactDescriptor,
        payloads: Mapping[str, bytes],
    ) -> DecodedSimulationOutput:
        """Return a reused output as decoded, or digest-check and decode a worker's.

        Raises ValueError when the bytes a worker's artifact holds differ from
        its digest.
        """
        reused = self.reused.get(descriptor.role.value)
        if reused is not None:
            return reused.decoded
        payload = payloads[descriptor.role.value]
        require_artifact_digest(
            payload,
            descriptor.artifact.content_sha256,
            mismatch_message=f"{descriptor.role.value} artifact digest mismatch",
        )
        return decode_simulation_output(payload)
