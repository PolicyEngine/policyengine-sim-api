"""Execution and persistence of one Stage 12 simulation."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from typing import Any

import pandas as pd
from policyengine_simulation_contract.stage12_bundle import CountryId
from policyengine_simulation_contract.stage12_execution import (
    ComparisonRunLifecycleStatus,
    ComparisonSimulationRecord,
    PlannedSimulationExecutionInput,
    SimulationArtifactDescriptor,
    SimulationExecutionInput,
    Stage12InvocationContext,
    stage12_output_plan_sha256,
)

from policyengine_simulation_executor.stage12_artifacts import (
    Stage12ArtifactStore,
    canonical_json_bytes,
)
from policyengine_simulation_executor.stage12_bundle import load_stage12_bundle

from .dependencies import ComparisonStore, artifact_store, runtime_store
from .output_planning import apply_output_plan, validate_output_frames


@dataclass(frozen=True)
class SimulationCalculation:
    frames: Mapping[str, pd.DataFrame]
    calculation_provenance: dict[str, Any] | None = None


def simulation_input_sha256(simulation: SimulationExecutionInput) -> str:
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
        raise ValueError("single-simulation input was sent to another country worker")
    if simulation.bundle.policyengine_version != context.worker_version:
        raise ValueError("single-simulation worker version does not match its context")
    if simulation.bundle.bundle_manifest_sha256 != context.bundle_manifest_sha256:
        raise ValueError("single-simulation bundle digest does not match its context")
    if str(simulation.evaluation_id) not in context.artifact_prefix:
        raise ValueError(
            "single-simulation artifact prefix names another comparison run"
        )
    if simulation.output_plan.country != required_country:
        raise ValueError("single-simulation output plan names another country")


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
    simulation: PlannedSimulationExecutionInput,
) -> SimulationCalculation:
    """Run one policy and return the coordinator-planned entity output tables."""

    if simulation.population.kind != "dataset":
        raise ValueError("Stage 12 society-wide worker requires a dataset population")
    if simulation.population.artifact.uri != simulation.bundle.dataset.uri:
        raise ValueError("population artifact differs from bundle dataset provenance")
    _require_installed_bundle(simulation)
    country = simulation.geography.country
    params: dict[str, Any] = {
        "country": country,
        "scope": "macro",
        "time_period": str(simulation.year),
        "region": simulation.geography.region,
        **simulation.options,
    }
    from policyengine_simulation_executor.simulation_runtime import (
        _build_simulation,
        _country_module,
        _load_dataset,
        _normalise_policy,
        _resolve_dataset_selection,
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
        dataset_selection = _resolve_dataset_selection(
            params,
            region_resolution=region,
        )
        dataset = _load_dataset(
            params,
            selection=dataset_selection,
            country_module=country_module,
        )
        model = _build_simulation(
            params,
            dataset=dataset,
            dataset_selection=dataset_selection,
            policy=_normalise_policy(simulation.policy),
            scoping_strategy=region.scoping_strategy,
            region_code=region.code,
        )
        apply_output_plan(model, simulation.output_plan)
        model.ensure()
        output_data = getattr(getattr(model, "output_dataset", None), "data", None)
        entity_data = getattr(output_data, "entity_data", None)
        if not isinstance(entity_data, Mapping):
            raise TypeError("simulation produced no entity output tables")
        frames = {entity: pd.DataFrame(frame) for entity, frame in entity_data.items()}
        validate_output_frames(frames, simulation.output_plan)
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


def descriptor_from_record(
    child: ComparisonSimulationRecord,
    simulation: PlannedSimulationExecutionInput,
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
        output_plan_sha256=stage12_output_plan_sha256(simulation.output_plan),
        row_identity=RowIdentity(
            identifier_columns=row_identity_columns,
            row_count=row_count,
            identity_sha256=row_identity_sha256,
        ),
        bundle=simulation.bundle,
    )


def run_single_simulation(
    payload: object,
    context_payload: object,
    *,
    required_country: CountryId,
    store: ComparisonStore | None = None,
    artifacts: Stage12ArtifactStore | None = None,
    calculator: Callable[
        [PlannedSimulationExecutionInput],
        Mapping[str, pd.DataFrame] | SimulationCalculation,
    ] = calculate_simulation_frames,
) -> dict[str, Any]:
    simulation = PlannedSimulationExecutionInput.model_validate(payload)
    context = Stage12InvocationContext.model_validate(context_payload)
    _require_context(simulation, context, required_country=required_country)
    persistence = store or runtime_store()
    artifact_storage = artifacts or artifact_store()
    child = persistence.get_simulation(simulation.simulation_execution_id)
    if child.status is ComparisonRunLifecycleStatus.SUCCEEDED:
        return descriptor_from_record(child, simulation).model_dump(mode="json")
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
    persistence.replace_simulation(running)
    try:
        artifact_storage.write_input(
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
        validate_output_frames(frames, simulation.output_plan)
        descriptor = artifact_storage.write_simulation(
            prefix=context.artifact_prefix,
            simulation=simulation,
            frames=frames,
            calculation_provenance=calculation_provenance,
        )
        completed = datetime.now(UTC)
        persistence.replace_simulation(
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
            )
        )
        return descriptor.model_dump(mode="json")
    # Persist any country-package calculation failure before returning a
    # stable exception to Modal.
    except Exception as error:  # noqa: BLE001
        failed_at = datetime.now(UTC)
        persistence.replace_simulation(
            running.model_copy(
                update={
                    "status": ComparisonRunLifecycleStatus.FAILED,
                    "error_code": "simulation_execution_failed",
                    "error_summary": type(error).__name__,
                    "updated_at": failed_at,
                    "completed_at": failed_at,
                }
            )
        )
        raise RuntimeError("Stage 12 simulation execution failed") from None
