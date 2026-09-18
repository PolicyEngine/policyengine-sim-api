"""Independent Stage 12 simulation execution and report coordination."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import os
from typing import Any, Protocol
from uuid import UUID, uuid4

import pandas as pd

from policyengine_simulation_contract.stage12_bundle import CountryId
from policyengine_simulation_contract.stage12_execution import (
    AggregateReportArtifactDescriptor,
    EvaluationAggregationStatus,
    EvaluationLifecycleStatus,
    EvaluationReportRecord,
    EvaluationSimulationPersistenceResult,
    EvaluationSimulationRecord,
    ReportExecutionInput,
    SimulationArtifactDescriptor,
    SimulationExecutionInput,
    Stage12InvocationContext,
)
from policyengine_simulation_contract.stage12_persistence import (
    PostgresEvaluationStore,
)
from policyengine_simulation_executor.stage12_artifacts import (
    Stage12ArtifactStore,
    canonical_json_bytes,
    deserialize_calculation_provenance,
    deserialize_simulation_frames,
)
from policyengine_simulation_executor.stage12_bundle import load_stage12_bundle

SIMULATION_WAIT_TIMEOUT_SECONDS = 3_000


class EvaluationStore(Protocol):
    def get_report(self, evaluation_id: UUID) -> EvaluationReportRecord: ...

    def replace_report(
        self, record: EvaluationReportRecord
    ) -> EvaluationReportRecord: ...

    def create_or_resolve_simulation(
        self,
        record: EvaluationSimulationRecord,
    ) -> EvaluationSimulationPersistenceResult: ...

    def get_simulation(
        self,
        simulation_execution_id: UUID,
    ) -> EvaluationSimulationRecord: ...

    def replace_simulation(
        self,
        record: EvaluationSimulationRecord,
    ) -> EvaluationSimulationRecord: ...

    def attach_simulation_invocation(
        self,
        simulation_execution_id: UUID,
        *,
        expected_placeholder: str,
        modal_invocation_id: str,
        updated_at: datetime,
    ) -> EvaluationSimulationRecord: ...

class ChildCall(Protocol):
    object_id: str

    def get(self, *, timeout: float | None = None) -> object: ...


class ChildInvoker(Protocol):
    def spawn(
        self,
        *,
        application_name: str,
        function_name: str,
        environment: str,
        simulation: dict[str, Any],
        context: dict[str, Any],
    ) -> ChildCall: ...

    def restore(self, invocation_id: str) -> ChildCall: ...


class ModalChildInvoker:
    def spawn(
        self,
        *,
        application_name: str,
        function_name: str,
        environment: str,
        simulation: dict[str, Any],
        context: dict[str, Any],
    ) -> ChildCall:
        from importlib import import_module

        modal: Any = import_module("modal")

        function = modal.Function.from_name(
            application_name,
            function_name,
            environment_name=environment,
        )
        return function.spawn(simulation, context)

    def restore(self, invocation_id: str) -> ChildCall:
        from importlib import import_module

        modal: Any = import_module("modal")

        return modal.FunctionCall.from_id(invocation_id)


@dataclass(frozen=True)
class SimulationCalculation:
    frames: Mapping[str, pd.DataFrame]
    calculation_provenance: dict[str, Any] | None = None


def _runtime_store() -> PostgresEvaluationStore:
    return PostgresEvaluationStore(os.environ.get("STAGE12_DATABASE_URL", ""))


def _artifact_store() -> Stage12ArtifactStore:
    return Stage12ArtifactStore(os.environ.get("STAGE12_ARTIFACT_BUCKET", ""))


def simulation_input_sha256(simulation: SimulationExecutionInput) -> str:
    normalized = simulation.model_dump(
        mode="json",
        exclude={"evaluation_id", "simulation_execution_id"},
    )
    return sha256(canonical_json_bytes(normalized)).hexdigest()


def _require_context(
    simulation: SimulationExecutionInput,
    context: Stage12InvocationContext,
    *,
    required_country: CountryId,
) -> None:
    if simulation.geography.country != required_country:
        raise ValueError("single-simulation input was sent to another country worker")
    if simulation.bundle.policyengine_version != context.worker_version:
        raise ValueError("single-simulation worker version does not match its context")
    if simulation.bundle.bundle_manifest_sha256 != context.bundle_manifest_sha256:
        raise ValueError("single-simulation bundle digest does not match its context")
    if str(simulation.evaluation_id) not in context.artifact_prefix:
        raise ValueError("single-simulation artifact prefix names another evaluation")
    if simulation.requested_output.variables != ("*",):
        raise ValueError(
            "Stage 12 report simulations require the complete output table"
        )


def _require_installed_bundle(simulation: SimulationExecutionInput) -> None:
    resolved = load_stage12_bundle()
    if resolved.bundle_manifest_sha256 != simulation.bundle.bundle_manifest_sha256:
        raise RuntimeError("installed bundle digest differs from the simulation input")
    if resolved.bundle.policyengine_version != simulation.bundle.policyengine_version:
        raise RuntimeError("installed PolicyEngine.py version differs from the input")
    country = next(
        item
        for item in resolved.bundle.countries
        if item.country == simulation.geography.country
    )
    expected = {
        "country_package_name": country.country_package_name,
        "country_package_version": country.country_package_version,
        "dataset_identity": country.default_dataset,
        "dataset_uri": country.default_dataset_uri,
        "dataset_revision": country.data_artifact_revision,
    }
    actual = {
        "country_package_name": simulation.bundle.country_package_name,
        "country_package_version": simulation.bundle.country_package_version,
        "dataset_identity": simulation.bundle.dataset.identity,
        "dataset_uri": simulation.bundle.dataset.uri,
        "dataset_revision": simulation.bundle.dataset.artifact_revision,
    }
    if actual != expected:
        raise RuntimeError("simulation provenance differs from the installed bundle")


def calculate_simulation_frames(
    simulation: SimulationExecutionInput,
) -> SimulationCalculation:
    """Run exactly one policy and return its complete entity output tables."""

    if simulation.population.kind != "dataset":
        raise ValueError("Stage 12 society-wide worker requires a dataset population")
    if simulation.population.artifact.uri != simulation.bundle.dataset.uri:
        raise ValueError("population artifact differs from bundle dataset provenance")
    _require_installed_bundle(simulation)
    country = simulation.geography.country
    params: dict[str, Any] = {
        "country": country,
        "scope": "macro",
        "data": simulation.bundle.dataset.uri,
        "data_version": simulation.bundle.dataset.artifact_revision,
        "time_period": str(simulation.year),
        "region": simulation.geography.region,
        **simulation.options,
    }
    from policyengine_simulation_executor.simulation_runtime import (
        _build_simulation,
        _country_module,
        _load_dataset,
        _normalise_policy,
        _resolve_region,
        setup_gcp_credentials,
    )

    with setup_gcp_credentials():
        country_module = _country_module(country)
        region = _resolve_region(
            country_module=country_module,
            country=country,
            params=params,
        )
        dataset = _load_dataset(
            params,
            country_module=country_module,
            region_resolution=region,
        )
        model = _build_simulation(
            params,
            dataset=dataset,
            policy=_normalise_policy(simulation.policy),
            scoping_strategy=region.scoping_strategy,
            region_code=region.code,
        )
        model.ensure()
        output_data = getattr(getattr(model, "output_dataset", None), "data", None)
        entity_data = getattr(output_data, "entity_data", None)
        if not isinstance(entity_data, Mapping):
            raise RuntimeError("simulation produced no entity output tables")
        frames = {entity: pd.DataFrame(frame) for entity, frame in entity_data.items()}
        selection = getattr(model, "spm_config", None)
        calculation_provenance = None
        if selection is not None:
            from policyengine_simulation_contract.spm import (
                SPMProvenance,
                SPMSelection,
            )

            receipt = getattr(model, "spm_provenance", None)
            if not callable(receipt):
                raise RuntimeError("simulation produced no SPM receipt")
            calculation_provenance = {
                "spm_config": SPMSelection.model_validate(selection).model_dump(
                    mode="json"
                ),
                "spm_provenance": SPMProvenance.model_validate(receipt()).model_dump(
                    mode="json"
                ),
            }
        return SimulationCalculation(
            frames=frames,
            calculation_provenance=calculation_provenance,
        )


def run_single_simulation(
    payload: object,
    context_payload: object,
    *,
    required_country: CountryId,
    store: EvaluationStore | None = None,
    artifacts: Stage12ArtifactStore | None = None,
    calculator: Callable[
        [SimulationExecutionInput],
        Mapping[str, pd.DataFrame] | SimulationCalculation,
    ] = calculate_simulation_frames,
) -> dict[str, Any]:
    simulation = SimulationExecutionInput.model_validate(payload)
    context = Stage12InvocationContext.model_validate(context_payload)
    _require_context(simulation, context, required_country=required_country)
    runtime_store = store or _runtime_store()
    artifact_store = artifacts or _artifact_store()
    child = runtime_store.get_simulation(simulation.simulation_execution_id)
    if child.status is EvaluationLifecycleStatus.SUCCEEDED:
        return _descriptor_from_child(child, simulation).model_dump(mode="json")
    started = datetime.now(timezone.utc)
    running = child.model_copy(
        update={
            "status": EvaluationLifecycleStatus.RUNNING,
            "started_at": child.started_at or started,
            "updated_at": started,
            "error_code": None,
            "error_summary": None,
        }
    )
    runtime_store.replace_simulation(running)
    try:
        artifact_store.write_input(
            prefix=context.artifact_prefix,
            simulation=simulation,
        )
        calculated = calculator(simulation)
        if isinstance(calculated, SimulationCalculation):
            frames = calculated.frames
            calculation_provenance = calculated.calculation_provenance
        else:
            frames = calculated
            calculation_provenance = None
        descriptor = artifact_store.write_simulation(
            prefix=context.artifact_prefix,
            simulation=simulation,
            frames=frames,
            calculation_provenance=calculation_provenance,
        )
        completed = datetime.now(timezone.utc)
        runtime_store.replace_simulation(
            running.model_copy(
                update={
                    "status": EvaluationLifecycleStatus.SUCCEEDED,
                    "output_uri": descriptor.artifact.uri,
                    "output_sha256": descriptor.artifact.content_sha256,
                    "output_schema_version": descriptor.output_schema_version,
                    "row_identity_columns": descriptor.row_identity.identifier_columns,
                    "row_count": descriptor.row_identity.row_count,
                    "row_identity_sha256": descriptor.row_identity.identity_sha256,
                    "updated_at": completed,
                    "completed_at": completed,
                }
            )
        )
        return descriptor.model_dump(mode="json")
    except Exception as error:
        failed_at = datetime.now(timezone.utc)
        runtime_store.replace_simulation(
            running.model_copy(
                update={
                    "status": EvaluationLifecycleStatus.FAILED,
                    "error_code": "simulation_execution_failed",
                    "error_summary": type(error).__name__,
                    "updated_at": failed_at,
                    "completed_at": failed_at,
                }
            )
        )
        raise RuntimeError("Stage 12 simulation execution failed") from None


def _descriptor_from_child(
    child: EvaluationSimulationRecord,
    simulation: SimulationExecutionInput,
) -> SimulationArtifactDescriptor:
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
        raise ValueError("successful child record has incomplete output metadata")
    from policyengine_simulation_contract.stage12_execution import (
        ArtifactMediaType,
        ArtifactReference,
        RowIdentity,
    )

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
        row_identity=RowIdentity(
            identifier_columns=row_identity_columns,
            row_count=row_count,
            identity_sha256=row_identity_sha256,
        ),
        bundle=simulation.bundle,
    )


def _child_record(
    *,
    simulation: SimulationExecutionInput,
    context: Stage12InvocationContext,
    function_name: str,
) -> EvaluationSimulationRecord:
    return EvaluationSimulationRecord(
        simulation_execution_id=simulation.simulation_execution_id,
        evaluation_id=simulation.evaluation_id,
        role=simulation.role,
        input_sha256=simulation_input_sha256(simulation),
        worker_version=context.worker_version,
        modal_application=context.modal_application,
        simulation_callable=function_name,
        version_manifest_sha256=context.version_manifest_sha256,
        status=EvaluationLifecycleStatus.PENDING,
        created_at=context.created_at,
        updated_at=context.created_at,
        retention_expires_at=context.retention_expires_at,
    )


def _validate_aligned_outputs(
    report: ReportExecutionInput,
    baseline: SimulationArtifactDescriptor,
    reform: SimulationArtifactDescriptor,
) -> None:
    if baseline.role.value != "baseline" or reform.role.value != "reform":
        raise ValueError("simulation artifacts have incorrect report roles")
    if (
        baseline.evaluation_id != report.evaluation_id
        or reform.evaluation_id != report.evaluation_id
    ):
        raise ValueError("simulation artifacts name another evaluation")
    if baseline.bundle != reform.bundle or baseline.bundle != report.baseline.bundle:
        raise ValueError("simulation artifacts have incompatible bundle provenance")
    if baseline.output_schema_version != reform.output_schema_version:
        raise ValueError("simulation artifacts use incompatible output schemas")
    if baseline.row_identity != reform.row_identity:
        raise ValueError("simulation artifacts have incompatible stable row identities")


def _dataset_from_frames(
    frames: Mapping[str, pd.DataFrame],
    *,
    country: CountryId,
    year: int,
):
    from microdf import MicroDataFrame

    fields = {}
    for entity, frame in frames.items():
        weight_column = f"{entity}_weight"
        fields[entity] = (
            MicroDataFrame(frame, weights=weight_column)
            if weight_column in frame.columns
            else MicroDataFrame(frame)
        )
    if country == "us":
        from policyengine.tax_benefit_models.us.datasets import (
            PolicyEngineUSDataset,
            USYearData,
        )

        return PolicyEngineUSDataset(
            data=USYearData(**fields),
            year=year,
            filepath=None,
            name="stage12-evaluation",
            description="Retained Stage 12 output",
        )
    from policyengine.tax_benefit_models.uk.datasets import (
        PolicyEngineUKDataset,
        UKYearData,
    )

    return PolicyEngineUKDataset(
        data=UKYearData(**fields),
        year=year,
        filepath=None,
        name="stage12-evaluation",
        description="Retained Stage 12 output",
    )


def build_aggregate_report(
    *,
    report: ReportExecutionInput,
    baseline_frames: Mapping[str, pd.DataFrame],
    reform_frames: Mapping[str, pd.DataFrame],
    baseline_descriptor: SimulationArtifactDescriptor,
    reform_descriptor: SimulationArtifactDescriptor,
) -> dict[str, Any]:
    from policyengine_simulation_executor.segmented_national_reduce import (
        PrecomputedSimulation,
    )
    from policyengine_simulation_executor.simulation_output_builder import (
        SimulationOutputBuilder,
    )
    from policyengine_simulation_executor.simulation_runtime import _country_module

    country = report.baseline.geography.country
    country_module = _country_module(country)
    datasets = {
        "baseline": _dataset_from_frames(
            baseline_frames,
            country=country,
            year=report.baseline.year,
        ),
        "reform": _dataset_from_frames(
            reform_frames,
            country=country,
            year=report.reform.year,
        ),
    }

    def stand_in(dataset):
        simulation = PrecomputedSimulation(
            dataset=dataset,
            tax_benefit_model_version=country_module.model,
            policy=None,
        )
        simulation.output_dataset = dataset
        return simulation

    params = {
        "country": country,
        "scope": "macro",
        "data": report.baseline.bundle.dataset.uri,
        "data_version": report.baseline.bundle.dataset.artifact_revision,
        "time_period": str(report.baseline.year),
        "region": report.baseline.geography.region,
        **report.baseline.options,
    }
    output = SimulationOutputBuilder(
        country=country,
        simulation_params=params,
        country_module=country_module,
        dataset=datasets["baseline"],
        baseline=stand_in(datasets["baseline"]),
        reform=stand_in(datasets["reform"]),
        resolved_data_version=report.baseline.bundle.dataset.artifact_revision,
        resolved_region_code=report.baseline.geography.region,
    ).serialize()
    output.update(
        _build_spm_result(
            report=report,
            baseline_descriptor=baseline_descriptor,
            reform_descriptor=reform_descriptor,
        )
    )
    return {
        "contract_version": 1,
        "aggregate_schema_version": 1,
        "evaluation_id": str(report.evaluation_id),
        "requested_aggregates": [item.value for item in report.requested_aggregates],
        "bundle": report.baseline.bundle.model_dump(mode="json"),
        "result": output,
    }


def _build_spm_result(
    *,
    report: ReportExecutionInput,
    baseline_descriptor: SimulationArtifactDescriptor,
    reform_descriptor: SimulationArtifactDescriptor,
) -> dict[str, Any]:
    requested_spm = report.baseline.options.get("spm")
    if requested_spm is None:
        return {}
    from policyengine_simulation_contract.spm import combine_spm_results

    baseline_provenance = baseline_descriptor.calculation_provenance
    reform_provenance = reform_descriptor.calculation_provenance
    if baseline_provenance is None or reform_provenance is None:
        raise ValueError("SPM calculation provenance is missing")
    selection = baseline_provenance.get("spm_config")
    if not isinstance(selection, dict):
        raise ValueError("SPM calculation selection is invalid")
    if selection != reform_provenance.get("spm_config"):
        raise ValueError("SPM calculation selections do not match")
    return combine_spm_results(
        [
            {
                "spm_config": selection,
                "spm_provenance": {
                    "baseline": [baseline_provenance.get("spm_provenance")],
                    "reform": [reform_provenance.get("spm_provenance")],
                },
            }
        ],
        selection,
        expected_year=report.baseline.year,
    )


def coordinate_report(
    payload: object,
    context_payload: object,
    *,
    application_name: str,
    store: EvaluationStore | None = None,
    artifacts: Stage12ArtifactStore | None = None,
    invoker: ChildInvoker | None = None,
    aggregator: Callable[..., dict[str, Any]] = build_aggregate_report,
) -> dict[str, Any]:
    report = ReportExecutionInput.model_validate(payload)
    context = Stage12InvocationContext.model_validate(context_payload)
    if application_name != context.modal_application:
        raise ValueError("report coordinator application differs from dispatch context")
    if report.baseline.bundle.policyengine_version != context.worker_version:
        raise ValueError("report worker version differs from dispatch context")
    if report.baseline.bundle.bundle_manifest_sha256 != context.bundle_manifest_sha256:
        raise ValueError("report bundle digest differs from dispatch context")
    runtime_store = store or _runtime_store()
    artifact_store = artifacts or _artifact_store()
    child_invoker = invoker or ModalChildInvoker()
    parent = runtime_store.get_report(report.evaluation_id)
    coordinating_at = datetime.now(timezone.utc)
    parent = runtime_store.replace_report(
        parent.model_copy(
            update={
                "status": EvaluationLifecycleStatus.RUNNING,
                "aggregation_status": EvaluationAggregationStatus.RUNNING,
                "error_code": None,
                "error_summary": None,
                "started_at": parent.started_at or coordinating_at,
                "updated_at": coordinating_at,
                "completed_at": None,
            }
        )
    )
    function_name = context.simulation_callable
    simulations = (report.baseline, report.reform)
    children = {
        simulation.role: runtime_store.create_or_resolve_simulation(
            _child_record(
                simulation=simulation,
                context=context,
                function_name=function_name,
            )
        ).record
        for simulation in simulations
    }
    descriptors: dict[str, SimulationArtifactDescriptor] = {}
    calls: dict[str, ChildCall] = {}
    try:
        for simulation in simulations:
            child = children[simulation.role]
            if child.status is EvaluationLifecycleStatus.SUCCEEDED:
                descriptor = _descriptor_from_child(child, simulation)
                retained = artifact_store.read(descriptor.artifact.uri)
                if sha256(retained).hexdigest() != descriptor.artifact.content_sha256:
                    raise ValueError("retained child artifact digest mismatch")
                descriptor = descriptor.model_copy(
                    update={
                        "artifact": descriptor.artifact.model_copy(
                            update={"size_bytes": len(retained)}
                        ),
                        "calculation_provenance": (
                            deserialize_calculation_provenance(retained)
                        ),
                    }
                )
                descriptors[simulation.role.value] = descriptor
                continue
            if (
                child.status is EvaluationLifecycleStatus.RUNNING
                and child.modal_invocation_id is not None
                and not child.modal_invocation_id.startswith("dispatch-pending-")
            ):
                calls[simulation.role.value] = child_invoker.restore(
                    child.modal_invocation_id
                )
                continue
            dispatching_at = datetime.now(timezone.utc)
            dispatch_placeholder = f"dispatch-pending-{uuid4()}"
            runtime_store.replace_simulation(
                child.model_copy(
                    update={
                        "status": EvaluationLifecycleStatus.RUNNING,
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
                call = child_invoker.spawn(
                    application_name=application_name,
                    function_name=function_name,
                    environment=context.modal_environment,
                    simulation=simulation.model_dump(mode="json"),
                    context=context.model_dump(mode="json"),
                )
            except Exception as error:
                failed_at = datetime.now(timezone.utc)
                latest = runtime_store.get_simulation(
                    simulation.simulation_execution_id
                )
                if latest.status is not EvaluationLifecycleStatus.SUCCEEDED:
                    runtime_store.replace_simulation(
                        latest.model_copy(
                            update={
                                "status": EvaluationLifecycleStatus.FAILED,
                                "error_code": "simulation_dispatch_failed",
                                "error_summary": type(error).__name__,
                                "updated_at": failed_at,
                                "completed_at": failed_at,
                            }
                        )
                    )
                raise
            calls[simulation.role.value] = call
            runtime_store.attach_simulation_invocation(
                simulation.simulation_execution_id,
                expected_placeholder=dispatch_placeholder,
                modal_invocation_id=call.object_id,
                updated_at=datetime.now(timezone.utc),
            )
        # Every required call has been started before the coordinator waits.
        for role, call in calls.items():
            try:
                descriptors[role] = SimulationArtifactDescriptor.model_validate(
                    call.get(timeout=SIMULATION_WAIT_TIMEOUT_SECONDS)
                )
            except Exception as error:
                simulation = next(
                    item for item in simulations if item.role.value == role
                )
                child = runtime_store.get_simulation(simulation.simulation_execution_id)
                if child.status is not EvaluationLifecycleStatus.SUCCEEDED:
                    failed_at = datetime.now(timezone.utc)
                    runtime_store.replace_simulation(
                        child.model_copy(
                            update={
                                "status": EvaluationLifecycleStatus.FAILED,
                                "error_code": "simulation_invocation_failed",
                                "error_summary": type(error).__name__,
                                "updated_at": failed_at,
                                "completed_at": failed_at,
                            }
                        )
                    )
                raise
        baseline = descriptors["baseline"]
        reform = descriptors["reform"]
        _validate_aligned_outputs(report, baseline, reform)
        baseline_payload = artifact_store.read(baseline.artifact.uri)
        reform_payload = artifact_store.read(reform.artifact.uri)
        if sha256(baseline_payload).hexdigest() != baseline.artifact.content_sha256:
            raise ValueError("baseline artifact digest mismatch")
        if sha256(reform_payload).hexdigest() != reform.artifact.content_sha256:
            raise ValueError("reform artifact digest mismatch")
        aggregate = aggregator(
            report=report,
            baseline_frames=deserialize_simulation_frames(baseline_payload),
            reform_frames=deserialize_simulation_frames(reform_payload),
            baseline_descriptor=baseline,
            reform_descriptor=reform,
        )
        aggregate_artifact = artifact_store.write_aggregate(
            prefix=context.artifact_prefix,
            payload=aggregate,
        )
        descriptor = AggregateReportArtifactDescriptor(
            evaluation_id=report.evaluation_id,
            artifact=aggregate_artifact,
            baseline_artifact_sha256=baseline.artifact.content_sha256,
            reform_artifact_sha256=reform.artifact.content_sha256,
            bundle=report.baseline.bundle,
        )
        completed = datetime.now(timezone.utc)
        runtime_store.replace_report(
            parent.model_copy(
                update={
                    "status": EvaluationLifecycleStatus.SUCCEEDED,
                    "aggregation_status": EvaluationAggregationStatus.SUCCEEDED,
                    "aggregate_output_uri": aggregate_artifact.uri,
                    "aggregate_output_sha256": aggregate_artifact.content_sha256,
                    "aggregate_schema_version": descriptor.aggregate_schema_version,
                    "updated_at": completed,
                    "completed_at": completed,
                }
            )
        )
        return descriptor.model_dump(mode="json")
    except Exception as error:
        failed_at = datetime.now(timezone.utc)
        runtime_store.replace_report(
            parent.model_copy(
                update={
                    "status": EvaluationLifecycleStatus.FAILED,
                    "aggregation_status": EvaluationAggregationStatus.FAILED,
                    "error_code": "report_coordination_failed",
                    "error_summary": type(error).__name__,
                    "updated_at": failed_at,
                    "completed_at": failed_at,
                }
            )
        )
        raise RuntimeError("Stage 12 report coordination failed") from None
