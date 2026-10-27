"""Execution and persistence of one Stage 12 simulation."""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from contextlib import nullcontext
from dataclasses import dataclass
from functools import partial
from hashlib import sha256
from typing import TYPE_CHECKING, Any

import pandas as pd
from policyengine.core import Simulation
from policyengine_simulation_contract.stage12_execution import (
    ComparisonRunLifecycleStatus,
    PlannedSimulationExecutionInput,
    ReportResultDelivery,
    SimulationArtifactDescriptor,
    SimulationExecutionInput,
    Stage12InvocationContext,
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
    detect_uk_run_dataset_metadata,
)

from .cached_simulation import Stage12CachedSimulation, qualifying_cache_identity
from .dependencies import ComparisonStore, artifact_store, runtime_store
from .failure_causes import (
    StageProgress,
    StageTracking,
    record_failure_cause,
    tracked_stage,
)
from .failures import (
    Stage12CancellationError,
    report_execution_error,
    validate_policy_periods,
)
from .output_planning import apply_output_plan, validate_output_frames
from .partition import should_segment_simulation
from .simulation_records import (
    cancellation_check,
    claim_simulation,
    descriptor_from_record,
    record_failure,
    record_success,
    simulation_failure_detail,
)

if TYPE_CHECKING:
    from contextlib import AbstractContextManager
    from types import ModuleType

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

__all__ = [
    "SimulationCalculation",
    "SimulationWorkerDependencies",
    "calculate_simulation_frames",
    "descriptor_from_record",
    "run_single_simulation",
    "simulation_input_sha256",
]


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
        resolve_dataset_reference,
    )

    named = simulation.bundle.dataset
    if (selection.name, selection.uri) == (named.identity, named.uri):
        return
    if not selection.is_default and named.identity == resolve_dataset_reference(
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
        _resolve_dataset_selection,
        normalise_policy,
        resolve_data_folder,
        resolve_region,
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
            region = resolve_region(
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
        uk_local_authority_metadata = detect_uk_run_dataset_metadata(
            country,
            dataset,
            region_code=region.code,
            dataset_name=dataset_selection.name,
        )
        with stage_span(Stage.POLICY_NORMALIZATION):
            policy = normalise_policy(simulation.policy)
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


@dataclass(frozen=True)
class _SimulationRun:
    """One planned simulation with its context, artifact store, seams and progress.

    ``progress`` records the stage the run is in, so a failure names it.
    """

    simulation: PlannedSimulationExecutionInput
    context: Stage12InvocationContext
    artifact_storage: Stage12ArtifactStore
    dependencies: SimulationWorkerDependencies
    progress: StageProgress

    def calculate_and_store(
        self,
        cancellation_requested: Callable[[], bool],
    ) -> SimulationArtifactDescriptor:
        """Check the policy, store the input, calculate, and store the output artifact.

        An eligible US simulation goes to the segmented calculator, which
        checks ``cancellation_requested`` while it waits for its segments.
        """
        simulation = self.simulation
        stage_span = partial(
            tracked_stage,
            tracking=StageTracking(
                STAGE12_SIMULATION_STAGES, self.progress, self.dependencies.runtime
            ),
        )
        # Modal restarts a pre-empted call, possibly on another machine whose
        # calculation differs in its last bits, or on a redeployed application
        # that plans the simulation differently. A returned simulation names
        # its input and output by their digests, so it never needs a key that
        # already holds other data.
        returned = self.context.result_delivery is ReportResultDelivery.RETURN_TO_CALLER
        write_input = (
            self.artifact_storage.write_input_named_by_digest
            if returned
            else self.artifact_storage.write_input
        )
        write_simulation = (
            self.artifact_storage.write_simulation_named_by_digest
            if returned
            else self.artifact_storage.write_simulation
        )
        validate_policy_periods(simulation.policy)
        with stage_span(Stage.STAGE12_INPUT_WRITE):
            write_input(prefix=self.context.artifact_prefix, simulation=simulation)
        calculation = _calculate(
            simulation,
            self.dependencies,
            progress=self.progress,
            stage_span=stage_span,
            cancellation_requested=cancellation_requested,
        )
        validate_output_frames(calculation.frames, simulation.output_plan)
        with stage_span(Stage.STAGE12_SIMULATION_ARTIFACT_WRITE):
            return write_simulation(
                prefix=self.context.artifact_prefix,
                simulation=simulation,
                frames=calculation.frames,
                calculation_provenance=calculation.calculation_provenance,
                uk_local_authority_metadata=calculation.uk_local_authority_metadata,
            )


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

    When the context's ``result_delivery`` returns the report to its caller,
    no Stage 12 row is read or written and ``store`` is not used; a failure
    raises ``ReportExecutionError`` with its redacted detail instead (see
    ``_run_simulation_for_caller``).
    """
    dependencies = dependencies or SimulationWorkerDependencies()
    runtime = dependencies.runtime
    simulation = PlannedSimulationExecutionInput.model_validate(payload)
    context = Stage12InvocationContext.model_validate(context_payload)
    _require_context(simulation, context, required_country=required_country)
    if context.result_delivery is ReportResultDelivery.RETURN_TO_CALLER:
        return _run_simulation_for_caller(
            _SimulationRun(
                simulation=simulation,
                context=context,
                artifact_storage=dependencies.artifacts or artifact_store(),
                dependencies=dependencies,
                progress=StageProgress(role=simulation.role.value),
            )
        )
    persistence = dependencies.store or runtime_store(runtime)
    artifact_storage = dependencies.artifacts or artifact_store()
    child = persistence.get_simulation(simulation.simulation_execution_id)
    if child.status is ComparisonRunLifecycleStatus.SUCCEEDED:
        return descriptor_from_record(child, simulation).model_dump(mode="json")
    running = claim_simulation(persistence, child)
    cancellation_requested = cancellation_check(persistence, simulation)
    run = _SimulationRun(
        simulation=simulation,
        context=context,
        artifact_storage=artifact_storage,
        dependencies=dependencies,
        progress=StageProgress(role=simulation.role.value),
    )
    try:
        descriptor = run.calculate_and_store(cancellation_requested)
        record_success(persistence, running, descriptor)
        return descriptor.model_dump(mode="json")
    except Stage12CancellationError:
        msg = "Stage 12 simulation was cancelled"
        raise RuntimeError(msg) from None
    # Persist a redacted cause for any country-package calculation failure
    # before returning a stable exception to Modal.
    except Exception as error:  # noqa: BLE001  # every calculation failure is persisted before a stable exception returns to Modal
        record_failure(
            error,
            simulation,
            persistence=persistence,
            runtime=runtime,
            progress=run.progress,
        )
        msg = "Stage 12 simulation execution failed"
        raise RuntimeError(msg) from None


def _never_cancelled() -> bool:
    """Report no cancellation: nothing records one for a returned simulation."""
    return False


def _run_simulation_for_caller(run: _SimulationRun) -> dict[str, Any]:
    """Calculate one simulation of a report returned to its caller.

    No Stage 12 row is read or written. The simulation is always calculated:
    no record can carry a cancellation, so the coordinator stops this call
    through Modal instead. Its input and output are stored under keys naming
    their digests, so a call Modal restarts after pre-empting it can always
    store its own.
    A failure is reported once, recorded as the operation's cause, and raised
    as ``ReportExecutionError`` with the redacted detail a Stage 12 row would
    record.
    """
    runtime = run.dependencies.runtime
    try:
        descriptor = run.calculate_and_store(_never_cancelled)
    except Exception as error:  # noqa: BLE001  # every calculation failure reaches the caller as a typed error with its redacted detail
        detail = simulation_failure_detail(
            error,
            run.simulation,
            runtime=runtime,
            progress=run.progress,
        )
        record_failure_cause(runtime, error, detail.error_summary)
        raise report_execution_error(run.simulation.evaluation_id, detail) from None
    return descriptor.model_dump(mode="json")


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
