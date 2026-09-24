"""Controlled parity qualification for the existing and Stage 12 runtimes."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
from typing import Any, Literal, cast

import pandas as pd
from pydantic import BaseModel, ConfigDict

from policyengine_simulation_contract.stage12_execution import (
    ArtifactMediaType,
    ArtifactReference,
    PlannedSimulationExecutionInput,
    ReportExecutionInput,
    SimulationArtifactDescriptor,
    stage12_output_plan_sha256,
)
from policyengine_simulation_executor.simulation_microdata import (
    rebuild_entity_frame,
)
from policyengine_simulation_executor.stage12_artifacts import (
    canonical_json_bytes,
    serialize_simulation_frames,
)
from policyengine_simulation_executor.stage12_parity import (
    NumericalTolerance,
    ParityMismatch,
    compare_aggregate_reports,
    compare_simulation_frames,
)
from policyengine_simulation_executor.stage12_runtime import (
    SimulationCalculation,
    simulation_input_sha256,
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
            raise ParityMismatch("normalized_input_mismatch", field)
    if existing_request.get("data") not in accepted_dataset_values:
        raise ParityMismatch("dataset_provenance_mismatch", "data")
    population = baseline.population
    if population.kind != "dataset":
        raise ParityMismatch("population_kind_mismatch", "population.kind")
    if population.artifact.uri != bundle.dataset.uri:
        raise ParityMismatch(
            "dataset_provenance_mismatch",
            "population.artifact.uri",
        )


def _existing_frames(
    microdata: object,
    *,
    side: Literal["baseline", "reform"],
) -> dict[str, pd.DataFrame]:
    if not isinstance(microdata, Mapping):
        raise ParityMismatch("missing_existing_microdata", "report._microdata")
    raw_side = microdata.get(side)
    raw_dtypes = microdata.get("dtypes")
    side_dtypes = raw_dtypes.get(side) if isinstance(raw_dtypes, Mapping) else None
    if not isinstance(raw_side, Mapping) or not isinstance(side_dtypes, Mapping):
        raise ParityMismatch("invalid_existing_microdata", f"report._microdata.{side}")
    frames: dict[str, pd.DataFrame] = {}
    for entity, columns in raw_side.items():
        entity_dtypes = side_dtypes.get(entity)
        if not isinstance(entity, str) or not isinstance(columns, dict):
            raise ParityMismatch(
                "invalid_existing_microdata",
                f"report._microdata.{side}",
            )
        if not isinstance(entity_dtypes, dict):
            raise ParityMismatch(
                "invalid_existing_dtypes",
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
        calculation_provenance=cast(
            dict[str, Any] | None,
            calculation.calculation_provenance,
        ),
    )


def qualify_report_parity(
    existing_request: Mapping[str, Any],
    report_payload: object,
    *,
    existing_runner: ExistingRunner | None = None,
    single_simulation_runner: SingleSimulationRunner | None = None,
    aggregate_builder: AggregateBuilder | None = None,
    simulation_tolerances: Mapping[str, NumericalTolerance] | None = None,
    aggregate_tolerances: Mapping[str, NumericalTolerance] | None = None,
) -> Stage12ParityReceipt:
    """Run both implementations and fail on every unexplained difference."""

    report = ReportExecutionInput.model_validate(report_payload)
    _validate_matching_input(existing_request, report)
    output_plan = resolve_report_output_plan(report)
    baseline_input = plan_simulation_input(report.baseline, output_plan)
    reform_input = plan_simulation_input(report.reform, output_plan)

    if existing_runner is None:
        from policyengine_simulation_executor.simulation_runtime import (
            run_simulation_impl,
        )

        existing_runner = run_simulation_impl
    if single_simulation_runner is None:
        from policyengine_simulation_executor.stage12_runtime import (
            calculate_simulation_frames,
        )

        single_simulation_runner = calculate_simulation_frames
    if aggregate_builder is None:
        from policyengine_simulation_executor.stage12_runtime import (
            build_aggregate_report,
        )

        aggregate_builder = build_aggregate_report

    request_with_microdata = dict(existing_request)
    request_with_microdata["_emit_microdata"] = True
    existing_result = dict(existing_runner(request_with_microdata))
    microdata = existing_result.pop("_microdata", None)
    existing_baseline = _existing_frames(microdata, side="baseline")
    existing_reform = _existing_frames(microdata, side="reform")

    bundle = report.baseline.bundle
    if existing_result.get("model_version") != bundle.country_package_version:
        raise ParityMismatch(
            "country_package_provenance_mismatch",
            "report.model_version",
        )
    if existing_result.get("data_version") != bundle.dataset.artifact_revision:
        raise ParityMismatch(
            "dataset_provenance_mismatch",
            "report.data_version",
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        baseline_future = executor.submit(single_simulation_runner, baseline_input)
        reform_future = executor.submit(single_simulation_runner, reform_input)
        baseline_calculation = _calculation(baseline_future.result())
        reform_calculation = _calculation(reform_future.result())

    compare_simulation_frames(
        existing_baseline,
        baseline_calculation.frames,
        tolerances=simulation_tolerances,
    )
    compare_simulation_frames(
        existing_reform,
        reform_calculation.frames,
        tolerances=simulation_tolerances,
    )

    baseline_descriptor = _descriptor(baseline_input, baseline_calculation)
    reform_descriptor = _descriptor(reform_input, reform_calculation)
    v2_report = aggregate_builder(
        report=report,
        baseline_frames=baseline_calculation.frames,
        reform_frames=reform_calculation.frames,
        baseline_descriptor=baseline_descriptor,
        reform_descriptor=reform_descriptor,
    )
    v2_result = v2_report.get("result")
    if not isinstance(v2_result, Mapping):
        raise ParityMismatch("invalid_v2_aggregate", "report.result")
    compare_aggregate_reports(
        existing_result,
        v2_result,
        tolerances=aggregate_tolerances,
    )

    existing_baseline_payload, _ = serialize_simulation_frames(existing_baseline)
    existing_reform_payload, _ = serialize_simulation_frames(existing_reform)
    v2_baseline_payload, _ = serialize_simulation_frames(
        baseline_calculation.frames,
        calculation_provenance=baseline_calculation.calculation_provenance,
    )
    v2_reform_payload, _ = serialize_simulation_frames(
        reform_calculation.frames,
        calculation_provenance=reform_calculation.calculation_provenance,
    )
    return Stage12ParityReceipt(
        evaluation_id=str(report.evaluation_id),
        bundle_manifest_sha256=bundle.bundle_manifest_sha256,
        baseline_input_sha256=simulation_input_sha256(baseline_input),
        reform_input_sha256=simulation_input_sha256(reform_input),
        existing_baseline_output_sha256=sha256(existing_baseline_payload).hexdigest(),
        v2_baseline_output_sha256=sha256(v2_baseline_payload).hexdigest(),
        existing_reform_output_sha256=sha256(existing_reform_payload).hexdigest(),
        v2_reform_output_sha256=sha256(v2_reform_payload).hexdigest(),
        existing_aggregate_output_sha256=sha256(
            canonical_json_bytes(existing_result)
        ).hexdigest(),
        v2_aggregate_output_sha256=sha256(canonical_json_bytes(v2_result)).hexdigest(),
        simulation_tolerance_fields=tuple(sorted(simulation_tolerances or {})),
        aggregate_tolerance_fields=tuple(sorted(aggregate_tolerances or {})),
    )
