"""What the coordinator or a simulation function is given to calculate a run.

A report run's coordinator takes a ``ReportExecutionInput`` naming the
report run as its ``evaluation_id`` and each side's simulation run as that
side's ``simulation_execution_id``, and a ``Stage12InvocationContext`` whose
``report_run`` names the three runs. It claims the report run and gives
each simulation function the run it claims; no evaluation record is passed.
A standalone simulation run's function takes that run's
``SimulationExecutionInput`` and a context naming the run
(``simulation_run_id``).

Both sides of a report calculate on the same dataset, region, year,
options and bundle; only their policies differ. The dataset is the bundle's
dataset whose identity is the catalog dataset's name, the region is the
catalog region's code, which .py's region registry reads, and the policies
are the stored policy and dynamic values by parameter name and period.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING, Any, Final

from policyengine_simulation_contract.stage12_execution import (
    BundleProvenance,
    DatasetArtifactMediaType,
    DatasetArtifactReference,
    DatasetPopulationInput,
    DatasetProvenance,
    GeographySelection,
    ReportAggregate,
    ReportExecutionInput,
    ReportRunIds,
    SimulationExecutionInput,
    SimulationRole,
    Stage12InvocationContext,
)

if TYPE_CHECKING:
    from datetime import datetime
    from uuid import UUID

    from policyengine_simulation_contract.stage12_bundle import Stage12Dataset
    from policyengine_simulation_contract.stage12_execution import CountryId

    from policyengine_simulation_entry.economy_workers import EconomyWorker

# Every aggregate a report computes, as the coordinator requires.
REPORT_AGGREGATES: Final = tuple(ReportAggregate)
# How long the coordinator keeps a call's Stage 12 artifacts; the longest the
# contract allows. Simulation output files go to the output bucket instead.
ARTIFACT_RETENTION: Final = timedelta(days=30)
# The context field naming a standalone simulation run's own run.
SIMULATION_RUN_ID_FIELD: Final = "simulation_run_id"


class DatasetNotInBundleError(LookupError):
    """The default worker's bundle has no dataset with the catalog dataset's name."""


@dataclass(frozen=True)
class CalculationScope:
    """What every simulation of a request calculates on, as the worker reads it.

    ``options`` are the simulation options both sides share: ``include_cliffs``
    when true, and the SPM selection when one was given.
    """

    country: CountryId
    region_code: str
    dataset: Stage12Dataset
    year: int
    options: dict[str, Any]


@dataclass(frozen=True)
class CallOrigin:
    """Where a call comes from: the deployment, the request and when it was made."""

    environment: str
    modal_environment: str
    request_id: str
    created_at: datetime


@dataclass(frozen=True)
class RunCalculation:
    """The two arguments the run's Modal function is called with."""

    payload: dict[str, Any]
    context: dict[str, Any]


def bundle_dataset(worker: EconomyWorker, dataset_name: str) -> Stage12Dataset:
    """Return the worker's bundle dataset whose identity is ``dataset_name``.

    Raise DatasetNotInBundleError when it has none.
    """
    dataset = next(
        (item for item in worker.country.datasets if item.identity == dataset_name),
        None,
    )
    if dataset is None:
        msg = (
            f"the default v2 worker, .py {worker.version}, has no dataset "
            f"{dataset_name}"
        )
        raise DatasetNotInBundleError(msg)
    return dataset


def simulation_options(
    *, include_cliffs: bool, spm: dict[str, Any] | None
) -> dict[str, Any]:
    """Return the options both sides of a calculation share."""
    options: dict[str, Any] = {}
    if include_cliffs:
        options["include_cliffs"] = True
    if spm is not None:
        options["spm"] = spm
    return options


def _simulation_input(  # noqa: PLR0913  # the run, its role, its policy and what it calculates
    worker: EconomyWorker,
    scope: CalculationScope,
    *,
    evaluation_id: UUID,
    simulation_run_id: UUID,
    role: SimulationRole,
    policy: dict[str, Any],
) -> SimulationExecutionInput:
    country = worker.country
    dataset = scope.dataset
    return SimulationExecutionInput(
        evaluation_id=evaluation_id,
        simulation_execution_id=simulation_run_id,
        role=role,
        policy=policy,
        population=DatasetPopulationInput(
            artifact=DatasetArtifactReference(
                uri=dataset.uri,
                media_type=DatasetArtifactMediaType.HDF5,
                content_sha256=dataset.sha256,
            )
        ),
        year=scope.year,
        geography=GeographySelection(country=scope.country, region=scope.region_code),
        options=scope.options,
        bundle=BundleProvenance(
            policyengine_version=worker.policyengine_version,
            country_package_name=country.country_package_name,
            country_package_version=country.country_package_version,
            dataset=DatasetProvenance(
                identity=dataset.identity,
                uri=dataset.uri,
                artifact_revision=dataset.artifact_revision,
                data_package_name=country.data_package_name,
                data_package_version=country.data_package_version,
            ),
            bundle_manifest_sha256=worker.bundle_manifest_sha256,
        ),
    )


def _context(
    worker: EconomyWorker,
    origin: CallOrigin,
    *,
    run_id: UUID,
    report_run: ReportRunIds | None,
) -> Stage12InvocationContext:
    created_at = origin.created_at
    return Stage12InvocationContext(
        request_id=origin.request_id,
        environment=origin.environment,
        modal_environment=origin.modal_environment,
        worker_version=worker.version,
        modal_application=worker.application_name,
        simulation_callable=worker.simulation_callable,
        version_manifest_sha256=worker.version_manifest_sha256,
        bundle_manifest_sha256=worker.bundle_manifest_sha256,
        artifact_prefix=(
            f"stage-12-runs/{origin.environment}/{created_at:%Y}/{created_at:%m}/"
            f"{run_id}"
        ),
        created_at=created_at,
        retention_expires_at=created_at + ARTIFACT_RETENTION,
        report_run=report_run,
    )


def report_run_calculation(  # noqa: PLR0913  # the runs, both policies and what they calculate
    worker: EconomyWorker,
    scope: CalculationScope,
    origin: CallOrigin,
    *,
    run_ids: ReportRunIds,
    baseline_policy: dict[str, Any],
    reform_policy: dict[str, Any],
) -> RunCalculation:
    """Return the coordinator's report and context for the report run."""
    report_run_id = run_ids.report_run_id
    report = ReportExecutionInput(
        evaluation_id=report_run_id,
        baseline=_simulation_input(
            worker,
            scope,
            evaluation_id=report_run_id,
            simulation_run_id=run_ids.baseline_simulation_run_id,
            role=SimulationRole.BASELINE,
            policy=baseline_policy,
        ),
        reform=_simulation_input(
            worker,
            scope,
            evaluation_id=report_run_id,
            simulation_run_id=run_ids.reform_simulation_run_id,
            role=SimulationRole.REFORM,
            policy=reform_policy,
        ),
        requested_aggregates=REPORT_AGGREGATES,
    )
    context = _context(worker, origin, run_id=report_run_id, report_run=run_ids)
    return RunCalculation(
        payload=report.model_dump(mode="json"),
        context=context.model_dump(mode="json"),
    )


def simulation_run_calculation(
    worker: EconomyWorker,
    scope: CalculationScope,
    origin: CallOrigin,
    *,
    simulation_run_id: UUID,
    policy: dict[str, Any],
) -> RunCalculation:
    """Return the simulation function's input and context for a standalone run.

    The context names the run the function claims and records.
    """
    simulation = _simulation_input(
        worker,
        scope,
        evaluation_id=simulation_run_id,
        simulation_run_id=simulation_run_id,
        role=SimulationRole.STANDALONE,
        policy=policy,
    )
    context = _context(worker, origin, run_id=simulation_run_id, report_run=None)
    return RunCalculation(
        payload=simulation.model_dump(mode="json"),
        context={
            **context.model_dump(mode="json"),
            SIMULATION_RUN_ID_FIELD: str(simulation_run_id),
        },
    )
