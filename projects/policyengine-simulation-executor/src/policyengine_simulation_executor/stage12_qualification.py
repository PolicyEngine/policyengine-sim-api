"""Controlled parity qualification for the existing and Stage 12 runtimes."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from hashlib import sha256
from typing import Any, Literal

import pandas as pd
from policyengine_observability import (
    DeploymentIdentity,
    LoggingConfig,
    ObservabilityConfig,
    ObservabilityRuntime,
    OTelConfig,
    ServiceIdentity,
)
from policyengine_simulation_contract.stage12_execution import (
    ArtifactMediaType,
    ArtifactReference,
    BundleProvenance,
    PlannedSimulationExecutionInput,
    ReportExecutionInput,
    SimulationArtifactDescriptor,
    stage12_output_plan_sha256,
)
from pydantic import BaseModel, ConfigDict

from policyengine_simulation_executor.simulation_microdata import (
    rebuild_entity_frame,
)
from policyengine_simulation_executor.stage12_artifacts import (
    canonical_json_bytes,
    serialize_simulation_frames,
)
from policyengine_simulation_executor.stage12_parity import (
    NumericalTolerance,
    ParityMismatchError,
    compare_aggregate_reports,
    compare_simulation_frames,
)
from policyengine_simulation_executor.stage12_runtime import (
    RetainedSimulationOutput,
    SimulationCalculation,
    build_aggregate_report,
    calculate_simulation_frames,
    simulation_input_sha256,
)
from policyengine_simulation_executor.stage12_runtime.aggregation import (
    validate_uk_local_authority_metadata,
)
from policyengine_simulation_executor.stage12_runtime.output_planning import (
    plan_simulation_input,
    resolve_report_output_plan,
)


class Stage12ParityReceipt(BaseModel):
    """Value-free evidence for one completed parity qualification."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    contract_version: Literal[1] = 1
    evaluation_id: str
    bundle_manifest_sha256: str
    baseline_input_sha256: str
    reform_input_sha256: str
    existing_baseline_output_sha256: str
    v2_baseline_output_sha256: str
    existing_reform_output_sha256: str
    v2_reform_output_sha256: str
    existing_aggregate_output_sha256: str
    v2_aggregate_output_sha256: str
    simulation_tolerance_fields: tuple[str, ...]
    aggregate_tolerance_fields: tuple[str, ...]


ExistingRunner = Callable[[dict[str, Any]], Mapping[str, Any]]
SingleSimulationRunner = Callable[
    [PlannedSimulationExecutionInput],
    Mapping[str, pd.DataFrame] | SimulationCalculation,
]
AggregateBuilder = Callable[..., dict[str, Any]]


def _local_qualification_runtime() -> ObservabilityRuntime:
    """Create a local runtime that cannot send qualification data remotely."""
    return ObservabilityRuntime(
        ObservabilityConfig(
            service=ServiceIdentity(
                name="policyengine-stage12-parity",
                namespace="policyengine.simulation-v2",
                version="local",
                role="qualification",
            ),
            deployment=DeploymentIdentity(
                environment="local",
                platform="local",
            ),
            logging=LoggingConfig(destinations=()),
            otel=OTelConfig(enabled=False),
        )
    )


def _validate_matching_input(
    existing_request: Mapping[str, Any],
    report: ReportExecutionInput,
) -> None:
    """Reject a qualification that does not compare the same calculation."""
    baseline = report.baseline
    reform = report.reform
    bundle = baseline.bundle
    accepted_dataset_values = {
        None,
        "default",
        bundle.dataset.identity,
        bundle.dataset.uri,
        f"{bundle.dataset.identity}@{bundle.dataset.artifact_revision}",
    }
    expected = {
        "country": baseline.geography.country,
        "scope": "macro",
        "time_period": str(baseline.year),
        "region": baseline.geography.region,
        "baseline": baseline.policy,
        "reform": reform.policy,
        "policyengine_version": bundle.policyengine_version,
        "version": bundle.country_package_version,
        "spm": baseline.options.get("spm"),
    }
    for field, expected_value in expected.items():
        actual_value = existing_request.get(field)
        if field == "spm" and expected_value is None and actual_value is None:
            continue
        if actual_value != expected_value:
            error_code = "normalized_input_mismatch"
            raise ParityMismatchError(error_code, field)
    if existing_request.get("data") not in accepted_dataset_values:
        error_code = "dataset_provenance_mismatch"
        raise ParityMismatchError(error_code, "data")
    population = baseline.population
    if population.kind != "dataset":
        error_code = "population_kind_mismatch"
        raise ParityMismatchError(error_code, "population.kind")
    if population.artifact.uri != bundle.dataset.uri:
        error_code = "dataset_provenance_mismatch"
        raise ParityMismatchError(
            error_code,
            "population.artifact.uri",
        )


def _existing_frames(
    microdata: object,
    *,
    side: Literal["baseline", "reform"],
) -> dict[str, pd.DataFrame]:
    if not isinstance(microdata, Mapping):
        error_code = "missing_existing_microdata"
        raise ParityMismatchError(error_code, "report._microdata")
    raw_side = microdata.get(side)
    raw_dtypes = microdata.get("dtypes")
    side_dtypes = raw_dtypes.get(side) if isinstance(raw_dtypes, Mapping) else None
    if not isinstance(raw_side, Mapping) or not isinstance(side_dtypes, Mapping):
        error_code = "invalid_existing_microdata"
        raise ParityMismatchError(error_code, f"report._microdata.{side}")
    frames: dict[str, pd.DataFrame] = {}
    for entity, columns in raw_side.items():
        entity_dtypes = side_dtypes.get(entity)
        if not isinstance(entity, str) or not isinstance(columns, dict):
            error_code = "invalid_existing_microdata"
            raise ParityMismatchError(
                error_code,
                f"report._microdata.{side}",
            )
        if not isinstance(entity_dtypes, dict):
            error_code = "invalid_existing_dtypes"
            raise ParityMismatchError(
                error_code,
                f"report._microdata.dtypes.{side}.{entity}",
            )
        frames[entity] = rebuild_entity_frame(columns, entity_dtypes)
    return frames


def _calculation(
    result: Mapping[str, pd.DataFrame] | SimulationCalculation,
) -> SimulationCalculation:
    if isinstance(result, SimulationCalculation):
        return result
    return SimulationCalculation(frames=result)


def _descriptor(
    simulation: PlannedSimulationExecutionInput,
    calculation: SimulationCalculation,
) -> SimulationArtifactDescriptor:
    payload, row_identity = serialize_simulation_frames(
        calculation.frames,
        calculation_provenance=calculation.calculation_provenance,
        uk_local_authority_metadata=calculation.uk_local_authority_metadata,
    )
    return SimulationArtifactDescriptor(
        evaluation_id=simulation.evaluation_id,
        simulation_execution_id=simulation.simulation_execution_id,
        role=simulation.role,
        artifact=ArtifactReference(
            uri=(
                "gs://stage12-parity/"
                f"{simulation.evaluation_id}/{simulation.role.value}.parquet"
            ),
            media_type=ArtifactMediaType.PARQUET,
            content_sha256=sha256(payload).hexdigest(),
            size_bytes=len(payload),
        ),
        output_plan_sha256=stage12_output_plan_sha256(simulation.output_plan),
        row_identity=row_identity,
        bundle=simulation.bundle,
        calculation_provenance=calculation.calculation_provenance,
    )


@dataclass(frozen=True)
class QualificationRunners:
    """Seams for the calculations a qualification runs; None selects the default.

    The defaults are the existing combined executor (on a local telemetry
    runtime that sends nothing remotely), the Stage 12 single-simulation
    calculation and the Stage 12 aggregate builder.
    """

    existing: ExistingRunner | None = None
    single_simulation: SingleSimulationRunner | None = None
    aggregate: AggregateBuilder | None = None


@dataclass(frozen=True)
class ParityTolerances:
    """Reviewed numeric slack for the simulation and aggregate comparisons.

    Each mapping is keyed by field path; an absent path compares exactly.
    """

    simulation: Mapping[str, NumericalTolerance] | None = None
    aggregate: Mapping[str, NumericalTolerance] | None = None


@dataclass(frozen=True)
class _QualifiedSide:
    """One role's planned input, existing-executor frames and Stage 12 result."""

    planned_input: PlannedSimulationExecutionInput
    existing_frames: dict[str, pd.DataFrame]
    calculation: SimulationCalculation

    def output_digests(self) -> tuple[str, str]:
        """Return the SHA-256 of the existing and the Stage 12 output artifacts."""
        existing_payload, _ = serialize_simulation_frames(self.existing_frames)
        v2_payload, _ = serialize_simulation_frames(
            self.calculation.frames,
            calculation_provenance=self.calculation.calculation_provenance,
            uk_local_authority_metadata=self.calculation.uk_local_authority_metadata,
        )
        return sha256(existing_payload).hexdigest(), sha256(v2_payload).hexdigest()


def _run_existing_executor(
    existing_request: Mapping[str, Any],
    existing_runner: ExistingRunner | None,
) -> dict[str, Any]:
    """Run the existing executor with microdata emission on, and copy its result.

    Without an injected runner this runs the real executor on a local runtime,
    which is shut down even when the run fails.
    """
    local_runtime: ObservabilityRuntime | None = None
    if existing_runner is None:
        from policyengine_simulation_executor.simulation_runtime import (  # noqa: PLC0415  # default runner, bound only when none is injected
            run_simulation_impl,
        )

        qualification_runtime = _local_qualification_runtime()
        local_runtime = qualification_runtime

        def run_existing(request: dict[str, Any]) -> Mapping[str, Any]:
            return run_simulation_impl(request, runtime=qualification_runtime)

        existing_runner = run_existing

    request_with_microdata = dict(existing_request)
    request_with_microdata["_emit_microdata"] = True
    try:
        return dict(existing_runner(request_with_microdata))
    finally:
        if local_runtime is not None:
            local_runtime.shutdown()


def _validate_existing_provenance(
    existing_result: Mapping[str, Any],
    bundle: BundleProvenance,
) -> None:
    """Require the existing result's model and data versions to be the bundle's."""
    if existing_result.get("model_version") != bundle.country_package_version:
        error_code = "country_package_provenance_mismatch"
        raise ParityMismatchError(
            error_code,
            "report.model_version",
        )
    if existing_result.get("data_version") != bundle.dataset.artifact_revision:
        error_code = "dataset_provenance_mismatch"
        raise ParityMismatchError(
            error_code,
            "report.data_version",
        )


def _run_v2_simulations(
    single_simulation_runner: SingleSimulationRunner,
    baseline_input: PlannedSimulationExecutionInput,
    reform_input: PlannedSimulationExecutionInput,
) -> tuple[SimulationCalculation, SimulationCalculation]:
    """Run the Stage 12 baseline and reform calculations concurrently."""
    with ThreadPoolExecutor(max_workers=2) as executor:
        baseline_future = executor.submit(single_simulation_runner, baseline_input)
        reform_future = executor.submit(single_simulation_runner, reform_input)
        baseline_calculation = _calculation(baseline_future.result())
        reform_calculation = _calculation(reform_future.result())
    return baseline_calculation, reform_calculation


def _v2_aggregate_result(
    report: ReportExecutionInput,
    aggregate_builder: AggregateBuilder,
    baseline: _QualifiedSide,
    reform: _QualifiedSide,
) -> Mapping[str, Any]:
    """Build the Stage 12 aggregate report and return its ``result`` object."""
    baseline_descriptor = _descriptor(baseline.planned_input, baseline.calculation)
    reform_descriptor = _descriptor(reform.planned_input, reform.calculation)
    v2_report = aggregate_builder(
        report=report,
        baseline=RetainedSimulationOutput(
            baseline.calculation.frames, baseline_descriptor
        ),
        reform=RetainedSimulationOutput(reform.calculation.frames, reform_descriptor),
        uk_local_authority_metadata=validate_uk_local_authority_metadata(
            report.baseline.geography.country,
            baseline.calculation.uk_local_authority_metadata,
            reform.calculation.uk_local_authority_metadata,
            region_code=report.baseline.geography.region,
        ),
    )
    v2_result = v2_report.get("result")
    if not isinstance(v2_result, Mapping):
        error_code = "invalid_v2_aggregate"
        raise ParityMismatchError(error_code, "report.result")
    return v2_result


def qualify_report_parity(
    existing_request: Mapping[str, Any],
    report_payload: object,
    *,
    runners: QualificationRunners | None = None,
    tolerances: ParityTolerances | None = None,
) -> Stage12ParityReceipt:
    """Run both implementations and fail on every unexplained difference."""
    runners = runners or QualificationRunners()
    tolerances = tolerances or ParityTolerances()
    report = ReportExecutionInput.model_validate(report_payload)
    _validate_matching_input(existing_request, report)
    output_plan = resolve_report_output_plan(report)
    baseline_input = plan_simulation_input(report.baseline, output_plan)
    reform_input = plan_simulation_input(report.reform, output_plan)

    existing_result = _run_existing_executor(existing_request, runners.existing)
    microdata = existing_result.pop("_microdata", None)
    existing_baseline = _existing_frames(microdata, side="baseline")
    existing_reform = _existing_frames(microdata, side="reform")
    bundle = report.baseline.bundle
    _validate_existing_provenance(existing_result, bundle)

    baseline_calculation, reform_calculation = _run_v2_simulations(
        runners.single_simulation or calculate_simulation_frames,
        baseline_input,
        reform_input,
    )
    baseline = _QualifiedSide(baseline_input, existing_baseline, baseline_calculation)
    reform = _QualifiedSide(reform_input, existing_reform, reform_calculation)
    for side in (baseline, reform):
        compare_simulation_frames(
            side.existing_frames,
            side.calculation.frames,
            tolerances=tolerances.simulation,
        )

    v2_result = _v2_aggregate_result(
        report, runners.aggregate or build_aggregate_report, baseline, reform
    )
    compare_aggregate_reports(
        existing_result,
        v2_result,
        tolerances=tolerances.aggregate,
    )

    existing_baseline_sha256, v2_baseline_sha256 = baseline.output_digests()
    existing_reform_sha256, v2_reform_sha256 = reform.output_digests()
    return Stage12ParityReceipt(
        evaluation_id=str(report.evaluation_id),
        bundle_manifest_sha256=bundle.bundle_manifest_sha256,
        baseline_input_sha256=simulation_input_sha256(baseline_input),
        reform_input_sha256=simulation_input_sha256(reform_input),
        existing_baseline_output_sha256=existing_baseline_sha256,
        v2_baseline_output_sha256=v2_baseline_sha256,
        existing_reform_output_sha256=existing_reform_sha256,
        v2_reform_output_sha256=v2_reform_sha256,
        existing_aggregate_output_sha256=sha256(
            canonical_json_bytes(existing_result)
        ).hexdigest(),
        v2_aggregate_output_sha256=sha256(canonical_json_bytes(v2_result)).hexdigest(),
        simulation_tolerance_fields=tuple(sorted(tolerances.simulation or {})),
        aggregate_tolerance_fields=tuple(sorted(tolerances.aggregate or {})),
    )
