"""Execution and persistence of one Stage 12 simulation."""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import partial
from hashlib import sha256
from typing import TYPE_CHECKING, Any

import pandas as pd
from policyengine.core import Simulation
from policyengine_simulation_contract.stage12_execution import (
    ArtifactMediaType,
    ArtifactReference,
    ComparisonRunLifecycleStatus,
    ComparisonSimulationRecord,
    PlannedSimulationExecutionInput,
    RowIdentity,
    SimulationArtifactDescriptor,
    SimulationExecutionInput,
    Stage12InvocationContext,
    stage12_output_plan_sha256,
)
from policyengine_simulation_observability.stages import (
    STAGE12_SIMULATION_STAGES,
    Stage,
    StagePlan,
)

from policyengine_simulation_executor.stage12_artifacts import (
    Stage12ArtifactStore,
    canonical_json_bytes,
)
from policyengine_simulation_executor.stage12_bundle import load_stage12_bundle
from policyengine_simulation_executor.uk_local_authority_metadata import (
    detect_uk_local_authority_metadata,
)

from .cached_simulation import Stage12CachedSimulation, qualifying_cache_identity
from .dependencies import ComparisonStore, artifact_store, runtime_store
from .failure_causes import (
    StageProgress,
    StageTracking,
    record_failure_cause,
    tracked_stage,
)
from .failure_recording import FailureSite, persist_failure_record, report_failure
from .failures import (
    Stage12CancellationError,
    Stage12ExecutionError,
    Stage12FailureDetail,
    validate_policy_periods,
)
from .output_planning import apply_output_plan, validate_output_frames
from .partition import should_segment_simulation

if TYPE_CHECKING:
    from contextlib import AbstractContextManager
    from types import ModuleType
    from uuid import UUID

    from policyengine.core import Dataset
    from policyengine_observability import ObservabilityRuntime
    from policyengine_simulation_contract.spm import SPMProvenance
    from policyengine_simulation_contract.stage12_bundle import CountryId
    from policyengine_simulation_contract.uk_geography import UKLocalAuthorityMetadata

    from policyengine_simulation_executor.simulation_runtime import (
        DatasetSelection,
        RegionResolution,
    )

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SimulationCalculation:
    """A calculator's entity output frames plus metadata stored alongside them."""

    frames: Mapping[str, pd.DataFrame]
    calculation_provenance: SPMProvenance | None = None
    uk_local_authority_metadata: UKLocalAuthorityMetadata | None = None
    cache_outcome: str | None = None


@dataclass(frozen=True)
class ResolvedSimulationInputs:
    """The country model, region, dataset selection and dataset a run uses."""

    country_module: ModuleType
    region: RegionResolution
    dataset_selection: DatasetSelection
    dataset: Dataset


def build_stage12_simulation(
    params: dict[str, Any],
    *,
    resolved: ResolvedSimulationInputs,
    policy: dict[str, Any] | None,
    execution: PlannedSimulationExecutionInput,
) -> Simulation:
    """Construct a Stage 12 simulation without selecting v1 cache code."""
    from policyengine_simulation_executor.spm import normalize_runtime_spm

    selection = normalize_runtime_spm(params)
    identity = qualifying_cache_identity(
        execution,
        resolved=load_stage12_bundle(),
        dataset_is_default=resolved.dataset_selection.is_default,
        scoping_strategy=resolved.region.scoping_strategy,
        spm=selection,
    )
    spm_kwargs: dict[str, Any] = {"spm": selection} if selection is not None else {}
    if identity is None:
        return Simulation(
            **spm_kwargs,
            dataset=resolved.dataset,
            tax_benefit_model_version=resolved.country_module.model,
            policy=policy,
            scoping_strategy=resolved.region.scoping_strategy,
        )
    model = Stage12CachedSimulation(
        **spm_kwargs,
        id=identity.simulation_id,
        dataset=resolved.dataset,
        tax_benefit_model_version=resolved.country_module.model,
        policy=policy,
        scoping_strategy=resolved.region.scoping_strategy,
    )
    model.configure_stage12_cache(execution.output_plan)
    return model


def simulation_runtime_params(
    simulation: PlannedSimulationExecutionInput,
) -> dict[str, Any]:
    """Return the parameters a Stage 12 simulation is resolved and built from."""
    return {
        "country": simulation.geography.country,
        "scope": "macro",
        "time_period": str(simulation.year),
        "region": simulation.geography.region,
        **simulation.options,
    }


def simulation_input_sha256(simulation: SimulationExecutionInput) -> str:
    """Hash a simulation input ignoring its run ids, so equal inputs hash equally."""
    normalized = simulation.model_dump(
        mode="json",
        exclude={"evaluation_id", "simulation_execution_id"},
    )
    return sha256(canonical_json_bytes(normalized)).hexdigest()


def _require_context(
    simulation: PlannedSimulationExecutionInput,
    context: Stage12InvocationContext,
    *,
    required_country: CountryId,
) -> None:
    if simulation.geography.country != required_country:
        msg = "single-simulation input was sent to another country worker"
        raise ValueError(msg)
    if simulation.bundle.policyengine_version != context.worker_version:
        msg = "single-simulation worker version does not match its context"
        raise ValueError(msg)
    if simulation.bundle.bundle_manifest_sha256 != context.bundle_manifest_sha256:
        msg = "single-simulation bundle digest does not match its context"
        raise ValueError(msg)
    if str(simulation.evaluation_id) not in context.artifact_prefix:
        msg = "single-simulation artifact prefix names another comparison run"
        raise ValueError(msg)
    if simulation.output_plan.country != required_country:
        msg = "single-simulation output plan names another country"
        raise ValueError(msg)


def _require_installed_bundle(simulation: SimulationExecutionInput) -> None:
    resolved = load_stage12_bundle()
    if resolved.bundle_manifest_sha256 != simulation.bundle.bundle_manifest_sha256:
        msg = "installed bundle digest differs from the simulation input"
        raise RuntimeError(msg)
    if resolved.bundle.policyengine_version != simulation.bundle.policyengine_version:
        msg = "installed PolicyEngine.py version differs from the input"
        raise RuntimeError(msg)
    country = next(
        item
        for item in resolved.bundle.countries
        if item.country == simulation.geography.country
    )
    # A region loads the dataset the bundle names for its region type. Entry
    # services released before regional provenance name the default dataset
    # for every region, so the default stays accepted until they are updated.
    regional = country.dataset_for_region(simulation.geography.region)
    named = simulation.bundle.dataset.identity
    regional_entry = next(
        (item for item in country.datasets if item.identity == regional), None
    )
    if regional_entry is not None and named == regional_entry.identity:
        expected_dataset = {
            "dataset_identity": regional_entry.identity,
            "dataset_uri": regional_entry.uri,
            "dataset_revision": regional_entry.artifact_revision,
        }
    else:
        expected_dataset = {
            "dataset_identity": country.default_dataset,
            "dataset_uri": country.default_dataset_uri,
            "dataset_revision": country.data_artifact_revision,
        }
    expected = {
        "country_package_name": country.country_package_name,
        "country_package_version": country.country_package_version,
        **expected_dataset,
    }
    actual = {
        "country_package_name": simulation.bundle.country_package_name,
        "country_package_version": simulation.bundle.country_package_version,
        "dataset_identity": simulation.bundle.dataset.identity,
        "dataset_uri": simulation.bundle.dataset.uri,
        "dataset_revision": simulation.bundle.dataset.artifact_revision,
    }
    if actual != expected:
        msg = "simulation provenance differs from the installed bundle"
        raise RuntimeError(msg)


def _require_loaded_dataset_matches_provenance(
    simulation: SimulationExecutionInput, selection: DatasetSelection
) -> None:
    """Require a calculation to load the dataset its provenance names.

    The provenance is recorded with the comparison run, so it must name the
    dataset the region actually loads. The one accepted difference is a
    state or district run whose provenance names the default dataset while it
    loads a regional one: entry services released before regional provenance
    send that until they are updated.
    """
    from policyengine_simulation_executor.simulation_runtime import (  # noqa: PLC0415  # resolved per call so tests can patch simulation_runtime helpers
        _resolve_dataset_reference,
    )

    named = simulation.bundle.dataset
    if (selection.name, selection.uri) == (named.identity, named.uri):
        return
    if not selection.is_default and named.identity == _resolve_dataset_reference(
        simulation.geography.country
    ):
        return
    msg = (
        "simulation provenance names a dataset other than the dataset its region loads"
    )
    raise RuntimeError(msg)


def calculate_simulation_frames(
    simulation: PlannedSimulationExecutionInput,
    *,
    runtime: ObservabilityRuntime | None = None,
    stage_plan: StagePlan = STAGE12_SIMULATION_STAGES,
    progress: StageProgress | None = None,
) -> SimulationCalculation:
    """Run one policy and return the coordinator-planned entity output tables."""
    if progress is None:
        progress = StageProgress(role=simulation.role.value)
    stage_span = partial(
        tracked_stage,
        tracking=StageTracking(stage_plan, progress, runtime),
    )
    if simulation.population.kind != "dataset":
        msg = "Stage 12 society-wide worker requires a dataset population"
        raise ValueError(msg)
    if simulation.population.artifact.uri != simulation.bundle.dataset.uri:
        msg = "population artifact differs from bundle dataset provenance"
        raise ValueError(msg)
    _require_installed_bundle(simulation)
    country = simulation.geography.country
    params = simulation_runtime_params(simulation)
    from policyengine_simulation_executor.simulation_runtime import (  # noqa: PLC0415  # resolved per call so tests can patch simulation_runtime helpers
        _country_module,
        _load_dataset,
        _normalise_policy,
        _require_certified_uk_weight_matrix,
        _resolve_dataset_selection,
        _resolve_region,
        resolve_data_folder,
        setup_gcp_credentials,
    )
    from policyengine_simulation_executor.stage12_cache import (  # noqa: PLC0415  # resolved per call so tests can patch the baked-folder lookup
        state_datasets,
    )

    with (
        stage_span(Stage.CREDENTIAL_SETUP),
        setup_gcp_credentials(),
    ):
        with stage_span(Stage.COUNTRY_MODULE_LOAD):
            country_module = _country_module(country)
        with stage_span(Stage.REGION_RESOLUTION):
            region = _resolve_region(
                country_module=country_module,
                country=country,
                params=params,
            )
        with stage_span(Stage.DATASET_RESOLUTION):
            dataset_selection = _resolve_dataset_selection(
                params,
                region_resolution=region,
            )
        _require_loaded_dataset_matches_provenance(simulation, dataset_selection)
        with stage_span(Stage.DATASET_LOAD):
            dataset = _load_dataset(
                params,
                selection=dataset_selection,
                country_module=country_module,
                data_folder=state_datasets.baked_state_data_folder(
                    country=country,
                    selection=dataset_selection,
                    state_code=region.state_code,
                    year=simulation.year,
                    data_folder=resolve_data_folder(),
                ),
            )
        uk_local_authority_metadata = detect_uk_local_authority_metadata(
            country,
            dataset,
            region_code=region.code,
        )
        _require_certified_uk_weight_matrix(
            region.scoping_strategy, dataset, dataset_selection
        )
        with stage_span(Stage.POLICY_NORMALIZATION):
            policy = _normalise_policy(simulation.policy)
        with stage_span(Stage.SIMULATION_BUILD):
            model = build_stage12_simulation(
                params,
                resolved=ResolvedSimulationInputs(
                    country_module=country_module,
                    region=region,
                    dataset_selection=dataset_selection,
                    dataset=dataset,
                ),
                policy=policy,
                execution=simulation,
            )
        with stage_span(Stage.STAGE12_CALCULATION):
            apply_output_plan(model, simulation.output_plan)
            with (
                stage_span(Stage.STAGE12_CACHE_LOOKUP)
                if hasattr(model, "stage12_cache_outcome")
                else nullcontext()
            ):
                model.ensure()
        cache_outcome = getattr(model, "stage12_cache_outcome", None)
        if runtime is not None and cache_outcome is not None:
            runtime.set_context(stage12_cache_outcome=cache_outcome)
        output_data = getattr(getattr(model, "output_dataset", None), "data", None)
        entity_data = getattr(output_data, "entity_data", None)
        if not isinstance(entity_data, Mapping):
            msg = "simulation produced no entity output tables"
            raise TypeError(msg)
        frames = {entity: pd.DataFrame(frame) for entity, frame in entity_data.items()}
        validate_output_frames(frames, simulation.output_plan)
        selection = getattr(model, "spm_config", None)
        calculation_provenance = None
        if selection is not None:
            from policyengine_simulation_executor.spm import (
                simulation_spm_calculation_provenance,
            )

            calculation_provenance = simulation_spm_calculation_provenance(
                model,
                selection,
            )
        return SimulationCalculation(
            frames=frames,
            calculation_provenance=calculation_provenance,
            uk_local_authority_metadata=uk_local_authority_metadata,
            cache_outcome=cache_outcome,
        )


def descriptor_from_record(
    child: ComparisonSimulationRecord,
    simulation: PlannedSimulationExecutionInput,
) -> SimulationArtifactDescriptor:
    """Rebuild the artifact descriptor of a child that already succeeded.

    ``size_bytes`` is 0 because the record does not store it; raises
    ``ValueError`` if the record lacks any output metadata.
    """
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
        msg = "successful child record has incomplete output metadata"
        raise ValueError(msg)

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
        output_plan_sha256=stage12_output_plan_sha256(simulation.output_plan),
        row_identity=RowIdentity(
            identifier_columns=row_identity_columns,
            row_count=row_count,
            identity_sha256=row_identity_sha256,
        ),
        bundle=simulation.bundle,
    )


def _require_completion_recorded(*, replaced: bool) -> None:
    """Raise ``Stage12CancellationError`` when the completion write replaced nothing.

    The coordinator records a cancelled simulation as failed while it is still
    running, so a write that finds the record no longer running means this
    simulation was cancelled.
    """
    if not replaced:
        msg = "Stage 12 simulation was cancelled"
        raise Stage12CancellationError(msg)


def _mark_running_simulation_failed(
    persistence: ComparisonStore,
    simulation_execution_id: UUID,
    *,
    detail: Stage12FailureDetail,
    failed_at: datetime,
) -> None:
    """Record a failure only while the simulation is still running."""
    latest = persistence.get_simulation(simulation_execution_id)
    if latest.status is not ComparisonRunLifecycleStatus.RUNNING:
        return
    persistence.replace_simulation_if_status(
        latest.model_copy(
            update={
                "status": ComparisonRunLifecycleStatus.FAILED,
                "error_code": detail.error_code,
                "error_summary": detail.error_summary,
                "updated_at": failed_at,
                "completed_at": failed_at,
            }
        ),
        expected_status=ComparisonRunLifecycleStatus.RUNNING,
    )


@dataclass(frozen=True)
class SimulationWorkerDependencies:
    """Seams the single-simulation worker uses; unset seams use the defaults.

    The defaults are the Stage 12 database store, the private artifact bucket,
    the standard country-package calculation, the deployed region-group
    segment runner and no telemetry runtime.
    """

    store: ComparisonStore | None = None
    artifacts: Stage12ArtifactStore | None = None
    calculator: Callable[
        [PlannedSimulationExecutionInput],
        Mapping[str, pd.DataFrame] | SimulationCalculation,
    ] = calculate_simulation_frames
    segmented_calculator: (
        Callable[
            [PlannedSimulationExecutionInput, Callable[[], bool]],
            SimulationCalculation,
        ]
        | None
    ) = None
    runtime: ObservabilityRuntime | None = None


def run_single_simulation(
    payload: object,
    context_payload: object,
    *,
    required_country: CountryId,
    dependencies: SimulationWorkerDependencies | None = None,
) -> dict[str, Any]:
    """Calculate one planned simulation, store its artifact, and return its descriptor.

    A child already marked succeeded returns its stored descriptor without
    recalculating. A failure after the run is marked running marks it FAILED
    and raises a ``RuntimeError`` that carries no calculation detail.
    """
    dependencies = dependencies or SimulationWorkerDependencies()
    runtime = dependencies.runtime
    simulation = PlannedSimulationExecutionInput.model_validate(payload)
    context = Stage12InvocationContext.model_validate(context_payload)
    _require_context(simulation, context, required_country=required_country)
    persistence = dependencies.store or runtime_store(runtime)
    artifact_storage = dependencies.artifacts or artifact_store()
    child = persistence.get_simulation(simulation.simulation_execution_id)
    if child.status is ComparisonRunLifecycleStatus.SUCCEEDED:
        return descriptor_from_record(child, simulation).model_dump(mode="json")
    running = _claim_simulation(persistence, child)
    cancellation_requested = _cancellation_check(persistence, simulation)
    progress = StageProgress(role=simulation.role.value)
    stage_span = partial(
        tracked_stage,
        tracking=StageTracking(STAGE12_SIMULATION_STAGES, progress, runtime),
    )
    try:
        validate_policy_periods(simulation.policy)
        with stage_span(Stage.STAGE12_INPUT_WRITE):
            artifact_storage.write_input(
                prefix=context.artifact_prefix,
                simulation=simulation,
            )
        calculation = _calculate(
            simulation,
            dependencies,
            progress=progress,
            stage_span=stage_span,
            cancellation_requested=cancellation_requested,
        )
        validate_output_frames(calculation.frames, simulation.output_plan)
        with stage_span(Stage.STAGE12_SIMULATION_ARTIFACT_WRITE):
            descriptor = artifact_storage.write_simulation(
                prefix=context.artifact_prefix,
                simulation=simulation,
                frames=calculation.frames,
                calculation_provenance=calculation.calculation_provenance,
                uk_local_authority_metadata=calculation.uk_local_authority_metadata,
            )
        _record_success(persistence, running, descriptor)
        return descriptor.model_dump(mode="json")
    except Stage12CancellationError:
        msg = "Stage 12 simulation was cancelled"
        raise RuntimeError(msg) from None
    # Persist a redacted cause for any country-package calculation failure
    # before returning a stable exception to Modal.
    except Exception as error:  # noqa: BLE001  # every calculation failure is persisted before a stable exception returns to Modal
        _record_failure(
            error,
            simulation,
            persistence=persistence,
            runtime=runtime,
            progress=progress,
        )
        msg = "Stage 12 simulation execution failed"
        raise RuntimeError(msg) from None


def _claim_simulation(
    persistence: ComparisonStore,
    child: ComparisonSimulationRecord,
) -> ComparisonSimulationRecord:
    """Mark an active simulation running and return its record, or refuse it.

    The claim is a conditional write, so a simulation whose record changed
    meanwhile, for example because the coordinator cancelled it, is not run.
    """
    if child.status not in {
        ComparisonRunLifecycleStatus.PENDING,
        ComparisonRunLifecycleStatus.RUNNING,
    }:
        msg = "Stage 12 simulation is no longer active"
        raise RuntimeError(msg)
    started = datetime.now(UTC)
    running = child.model_copy(
        update={
            "status": ComparisonRunLifecycleStatus.RUNNING,
            "started_at": child.started_at or started,
            "updated_at": started,
            "error_code": None,
            "error_summary": None,
        }
    )
    running, claimed = persistence.replace_simulation_if_status(
        running,
        expected_status=child.status,
    )
    if not claimed:
        msg = "Stage 12 simulation was cancelled"
        raise RuntimeError(msg)
    return running


def _cancellation_check(
    persistence: ComparisonStore,
    simulation: PlannedSimulationExecutionInput,
) -> Callable[[], bool]:
    """Return a check of whether the coordinator has stopped this simulation."""

    def cancellation_requested() -> bool:
        try:
            latest = persistence.get_simulation(simulation.simulation_execution_id)
        except Exception as error:  # noqa: BLE001  # a failed status read is logged and treated as not cancelled, so the calculation continues
            logger.warning(
                "Stage 12 cancellation check failed for %s (%s)",
                simulation.simulation_execution_id,
                type(error).__name__,
            )
            return False
        return latest.status is not ComparisonRunLifecycleStatus.RUNNING

    return cancellation_requested


def _calculate(
    simulation: PlannedSimulationExecutionInput,
    dependencies: SimulationWorkerDependencies,
    *,
    progress: StageProgress,
    stage_span: Callable[[Stage], AbstractContextManager[object]],
    cancellation_requested: Callable[[], bool],
) -> SimulationCalculation:
    """Run the segmented, standard or injected calculation this simulation uses."""
    segmented_calculator = dependencies.segmented_calculator
    if segmented_calculator is not None and should_segment_simulation(simulation):
        calculated = segmented_calculator(simulation, cancellation_requested)
    elif dependencies.calculator is calculate_simulation_frames:
        calculated = calculate_simulation_frames(
            simulation,
            runtime=dependencies.runtime,
            progress=progress,
        )
    else:
        with stage_span(Stage.STAGE12_CALCULATION):
            calculated = dependencies.calculator(simulation)
    if isinstance(calculated, SimulationCalculation):
        return calculated
    return SimulationCalculation(frames=calculated)


def _record_success(
    persistence: ComparisonStore,
    running: ComparisonSimulationRecord,
    descriptor: SimulationArtifactDescriptor,
) -> None:
    """Record the stored artifact on the running simulation.

    Raises ``Stage12CancellationError`` when the coordinator stopped the
    simulation first.
    """
    completed = datetime.now(UTC)
    _, replaced = persistence.replace_simulation_if_status(
        running.model_copy(
            update={
                "status": ComparisonRunLifecycleStatus.SUCCEEDED,
                "output_uri": descriptor.artifact.uri,
                "output_sha256": descriptor.artifact.content_sha256,
                "output_schema_version": descriptor.output_schema_version,
                "row_identity_columns": descriptor.row_identity.identifier_columns,
                "row_count": descriptor.row_identity.row_count,
                "row_identity_sha256": descriptor.row_identity.identity_sha256,
                "updated_at": completed,
                "completed_at": completed,
            }
        ),
        expected_status=ComparisonRunLifecycleStatus.RUNNING,
    )
    _require_completion_recorded(replaced=replaced)


def _record_failure(
    error: Exception,
    simulation: PlannedSimulationExecutionInput,
    *,
    persistence: ComparisonStore,
    runtime: ObservabilityRuntime | None,
    progress: StageProgress,
) -> None:
    """Report a calculation failure once, then record it if still running."""
    if isinstance(error, Stage12ExecutionError):
        # A segmented calculation reports its segment failure where it
        # occurs, before raising this error.
        detail = error.detail
    else:
        detail = report_failure(
            error,
            FailureSite(
                scope="simulation_execution",
                default_code="simulation_execution_failed",
                attributes={
                    "simulation_role": simulation.role.value,
                    "evaluation_id": str(simulation.evaluation_id),
                    "simulation_execution_id": str(simulation.simulation_execution_id),
                },
            ),
            runtime=runtime,
            progress=progress,
        )
    record_failure_cause(runtime, error, detail.error_summary)
    failed_at = datetime.now(UTC)
    persist_failure_record(
        partial(
            _mark_running_simulation_failed,
            persistence,
            simulation.simulation_execution_id,
            detail=detail,
            failed_at=failed_at,
        ),
        runtime=runtime,
        failure_scope="simulation_execution",
    )
