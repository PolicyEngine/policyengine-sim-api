"""Calculate one v2 economy simulation run and store its output.

The call (``EconomySimulationCall``) names the run, its policy's parameter
values, its year and the region and dataset its catalog version has. The
worker plans the run's output columns for any report it may be compared in
(``resolve_simulation_output_plan``), and calculates it as a Stage 12 worker
calculates a simulation whose result returns to its caller: it reads and
writes no database rows, scopes the catalog's dataset with the catalog's
filter (``catalog_population``), calculates a US national run in segments,
and stores its input and output under keys naming their SHA-256, so a call
Modal restarts after pre-empting it can always store its own. A simulation
run has no report role; the Stage 12 input names it the baseline.

The artifacts go under the Stage 12 layout for the run
(``stage-12-runs/<environment>/<YYYY>/<MM>/<simulation_run_id>``), in the
bucket whose files are deleted after 30 days.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from hashlib import sha256
from typing import TYPE_CHECKING, Any

from policyengine_simulation_contract.economy_runs import (
    EconomySimulationCall,
    EconomySimulationOutput,
)
from policyengine_simulation_contract.stage12_execution import (
    MAX_COMPARISON_RUN_ARTIFACT_RETENTION,
    ArtifactMediaType,
    ArtifactReference,
    BundleProvenance,
    DatasetArtifactMediaType,
    DatasetArtifactReference,
    DatasetPopulationInput,
    DatasetProvenance,
    GeographySelection,
    PlannedSimulationExecutionInput,
    ReportResultDelivery,
    SimulationArtifactDescriptor,
    SimulationExecutionInput,
    SimulationRole,
    Stage12InvocationContext,
    Stage12OutputPlan,
)

from policyengine_simulation_executor.stage12_artifacts import (
    canonical_json_bytes,
    comparison_run_prefix,
    content_addressed_input_path,
)
from policyengine_simulation_executor.stage12_bundle import load_stage12_bundle
from policyengine_simulation_executor.stage12_runtime import (
    SimulationWorkerDependencies,
    run_single_simulation,
)
from policyengine_simulation_executor.stage12_runtime.catalog_population import (
    CATALOG_POPULATION_OPTION,
    catalog_dataset,
)
from policyengine_simulation_executor.stage12_runtime.dependencies import (
    artifact_store,
)
from policyengine_simulation_executor.stage12_runtime.output_planning import (
    resolve_simulation_output_plan,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from policyengine_simulation_contract.stage12_bundle import (
        CountryId,
        ResolvedStage12Bundle,
    )

    from policyengine_simulation_executor.stage12_artifacts import (
        Stage12ArtifactStore,
    )


@dataclass(frozen=True)
class EconomyRunInvocation:
    """The deployed Modal function running the call, as its context names it."""

    application_name: str
    modal_environment: str
    simulation_callable: str


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _bundle_provenance(
    call: EconomySimulationCall, resolved: ResolvedStage12Bundle
) -> tuple[BundleProvenance, DatasetPopulationInput]:
    """Return the provenance and population of the catalog's dataset.

    Raise ValueError when this application installs another policyengine.py
    version, or a release without the dataset the catalog names.
    """
    installed = resolved.bundle.policyengine_version
    if installed != call.policyengine_version:
        msg = (
            f"the call names PolicyEngine.py {call.policyengine_version}, but this "
            f"application installs PolicyEngine.py {installed}"
        )
        raise ValueError(msg)
    country = next(
        item for item in resolved.bundle.countries if item.country == call.country
    )
    dataset = catalog_dataset(country, call.population)
    provenance = BundleProvenance(
        policyengine_version=installed,
        country_package_name=country.country_package_name,
        country_package_version=country.country_package_version,
        dataset=DatasetProvenance(
            identity=dataset.identity,
            uri=dataset.uri,
            artifact_revision=dataset.artifact_revision,
            data_package_name=country.data_package_name,
            data_package_version=country.data_package_version,
        ),
        bundle_manifest_sha256=resolved.bundle_manifest_sha256,
    )
    population = DatasetPopulationInput(
        artifact=DatasetArtifactReference(
            uri=dataset.uri,
            media_type=DatasetArtifactMediaType.HDF5,
            content_sha256=dataset.sha256,
        )
    )
    return provenance, population


def _input_reference(
    storage: Stage12ArtifactStore,
    prefix: str,
    simulation: PlannedSimulationExecutionInput,
) -> ArtifactReference:
    """Name the input file the worker stored under its digest."""
    payload = canonical_json_bytes(simulation.model_dump(mode="json"))
    digest = sha256(payload).hexdigest()
    path = content_addressed_input_path(
        prefix=prefix, role=simulation.role.value, content_sha256=digest
    )
    return ArtifactReference(
        uri=f"gs://{storage.bucket_name}/{path}",
        media_type=ArtifactMediaType.JSON,
        content_sha256=digest,
        size_bytes=len(payload),
    )


@dataclass(frozen=True)
class EconomySimulationDependencies:
    """The call's seams; unset seams use the deployed defaults.

    ``worker`` holds the Stage 12 worker's seams, ``output_planner`` plans
    the run's output columns, and ``clock`` dates its artifact paths.
    """

    worker: SimulationWorkerDependencies = field(
        default_factory=SimulationWorkerDependencies
    )
    output_planner: Callable[..., Stage12OutputPlan] = resolve_simulation_output_plan
    clock: Callable[[], datetime] = _utc_now


def run_economy_simulation(
    payload: object,
    *,
    required_country: CountryId,
    invocation: EconomyRunInvocation,
    dependencies: EconomySimulationDependencies | None = None,
) -> dict[str, Any]:
    """Calculate one simulation run, store its input and output, and describe them.

    Return an ``EconomySimulationOutput`` in JSON mode. A call for another
    country, another policyengine.py version or a dataset the installed
    release lacks raises ValueError before anything is stored. A failed
    calculation raises ``ReportExecutionError`` with its redacted detail, as
    a returned Stage 12 simulation does.
    """
    dependencies = dependencies or EconomySimulationDependencies()
    call = EconomySimulationCall.model_validate(payload)
    if call.country != required_country:
        msg = "the call was sent to another country's simulation function"
        raise ValueError(msg)
    provenance, population = _bundle_provenance(call, load_stage12_bundle())
    simulation = SimulationExecutionInput(
        evaluation_id=call.simulation_run_id,
        simulation_execution_id=call.simulation_run_id,
        role=SimulationRole.BASELINE,
        policy=dict(call.policy),
        population=population,
        year=call.year,
        geography=GeographySelection(
            country=call.country, region=call.population.region_code
        ),
        options={CATALOG_POPULATION_OPTION: call.population.model_dump(mode="json")},
        bundle=provenance,
    )
    planned = PlannedSimulationExecutionInput(
        **simulation.model_dump(),
        output_plan=dependencies.output_planner(
            simulation, labor_supply_response=call.labor_supply_response
        ),
    )
    created_at = dependencies.clock()
    prefix = comparison_run_prefix(
        environment=call.artifact_environment,
        created_at=created_at,
        evaluation_id=call.simulation_run_id,
    )
    context = Stage12InvocationContext(
        request_id=str(call.simulation_run_id),
        environment=call.artifact_environment,
        modal_environment=invocation.modal_environment,
        worker_version=provenance.policyengine_version,
        modal_application=invocation.application_name,
        simulation_callable=invocation.simulation_callable,
        # The v2 economy path deploys no version manifest of its own; a
        # returned simulation reads neither digest.
        version_manifest_sha256=provenance.bundle_manifest_sha256,
        bundle_manifest_sha256=provenance.bundle_manifest_sha256,
        artifact_prefix=prefix,
        created_at=created_at,
        retention_expires_at=created_at + MAX_COMPARISON_RUN_ARTIFACT_RETENTION,
        result_delivery=ReportResultDelivery.RETURN_TO_CALLER,
    )
    worker = dependencies.worker
    storage = worker.artifacts or artifact_store()
    descriptor = SimulationArtifactDescriptor.model_validate(
        run_single_simulation(
            planned.model_dump(mode="json"),
            context.model_dump(mode="json"),
            required_country=required_country,
            dependencies=replace(worker, artifacts=storage),
        )
    )
    return EconomySimulationOutput(
        simulation_run_id=call.simulation_run_id,
        input=_input_reference(storage, prefix, planned),
        output=descriptor.artifact,
        output_plan_sha256=descriptor.output_plan_sha256,
        row_identity=descriptor.row_identity,
        bundle=descriptor.bundle,
    ).model_dump(mode="json")
