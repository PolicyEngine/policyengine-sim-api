"""Rows for the economy simulation and report tests.

The catalog rows a request names are the Public API's, inserted as the
superuser. A run that already succeeded, and the report run that used it, are
written as the superuser too, through the service tables' models: the
service's role can neither mark a run succeeded nor record its output.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from policyengine_simulation_persistence import (
    EconomyPopulation,
    ReportRunStatus,
    ReportRunTrigger,
    RunVersions,
    SimulationChoice,
    SimulationStatus,
    SimulationType,
    report_runs,
    reports,
    simulation_runs,
    simulations,
)
from public_api_rows import (
    CatalogVersion,
    insert_catalog_version,
    insert_dataset,
    insert_model,
    insert_region,
)
from sqlalchemy import insert

if TYPE_CHECKING:
    from sqlalchemy import Connection

POLICYENGINE_VERSION = "6.17.1"
COUNTRY_PACKAGE_VERSION = "1.764.6"
VERSIONS = RunVersions(
    country_package_version=COUNTRY_PACKAGE_VERSION,
    policyengine_version=POLICYENGINE_VERSION,
)
OUTPUT_BUCKET_PREFIX = "gs://policyengine-v2-simulation-outputs-staging/"
CURRENT_LAW = SimulationChoice(policy_id=None, dynamic_id=None)
OPTIONS: dict[str, Any] = {"include_cliffs": False}
COMPLETED_AT = datetime(2026, 11, 1, tzinfo=UTC)


@dataclass(frozen=True)
class EconomyCatalog:
    """A country's catalog version, with one dataset and one region using it."""

    catalog: CatalogVersion
    dataset_id: UUID
    region_id: UUID

    def population(self, *, year: int = 2026) -> EconomyPopulation:
        """Return the population a request for the region and year uses."""
        return EconomyPopulation(
            tax_benefit_model_version_id=self.catalog.version_id,
            region_id=self.region_id,
            dataset_id=self.dataset_id,
            year=year,
        )


def insert_economy_catalog(
    connection: Connection,
    *,
    country: str = "us",
    parameter_names: tuple[str, ...] = (),
) -> EconomyCatalog:
    """Insert the country's model, a catalog version, a dataset and a region."""
    catalog = insert_catalog_version(
        connection,
        country=country,
        model_id=insert_model(connection, country),
        policyengine_version=POLICYENGINE_VERSION,
        parameter_names=parameter_names,
    )
    dataset_id = insert_dataset(connection, catalog, f"populace_{country}_2024")
    region_id = insert_region(
        connection, catalog, country, default_dataset_id=dataset_id
    )
    return EconomyCatalog(catalog=catalog, dataset_id=dataset_id, region_id=region_id)


@dataclass(frozen=True)
class StoredRun:
    """A simulation and one of its runs, as stored."""

    simulation_id: UUID
    simulation_run_id: UUID


@dataclass(frozen=True)
class RunShape:
    """What a stored economy simulation and its run hold.

    A succeeded run records an output file in the output bucket unless
    ``output_uri`` names another.
    """

    choice: SimulationChoice = CURRENT_LAW
    status: SimulationStatus = SimulationStatus.SUCCEEDED
    versions: RunVersions = VERSIONS
    output_uri: str | None = None
    completed_at: datetime = COMPLETED_AT


def insert_simulation_run(
    connection: Connection,
    population: EconomyPopulation,
    shape: RunShape | None = None,
) -> StoredRun:
    """Insert an economy simulation and one run of it, as the superuser."""
    shape = shape or RunShape()
    simulation_id = uuid4()
    run_id = uuid4()
    finished = shape.status in {SimulationStatus.SUCCEEDED, SimulationStatus.FAILED}
    succeeded = shape.status is SimulationStatus.SUCCEEDED
    connection.execute(
        insert(simulations).values(
            id=simulation_id,
            simulation_type=SimulationType.ECONOMY,
            dataset_id=population.dataset_id,
            policy_id=shape.choice.policy_id,
            dynamic_id=shape.choice.dynamic_id,
            tax_benefit_model_version_id=population.tax_benefit_model_version_id,
            region_id=population.region_id,
            year=population.year,
        )
    )
    connection.execute(
        insert(simulation_runs).values(
            id=run_id,
            simulation_id=simulation_id,
            year=population.year,
            tax_benefit_model_version_id=population.tax_benefit_model_version_id,
            country_package_version=shape.versions.country_package_version,
            policyengine_version=shape.versions.policyengine_version,
            dataset_id=population.dataset_id,
            region_id=population.region_id,
            status=shape.status,
            completed_at=shape.completed_at if finished else None,
            output_uri=(
                (shape.output_uri or f"{OUTPUT_BUCKET_PREFIX}outputs/{run_id}.parquet")
                if succeeded
                else None
            ),
            output_sha256="d" * 64 if succeeded else None,
        )
    )
    return StoredRun(simulation_id=simulation_id, simulation_run_id=run_id)


@dataclass(frozen=True)
class ReportRunOutcome:
    """How a stored report run ended, and the options its report has."""

    status: ReportRunStatus = ReportRunStatus.SUCCEEDED
    inputs: dict[str, Any] | None = None
    error_message: str | None = None


def insert_report_run_naming(
    connection: Connection,
    economy: EconomyCatalog,
    compared: tuple[StoredRun, StoredRun],
    outcome: ReportRunOutcome | None = None,
) -> UUID:
    """Insert a report comparing the two runs, and a run of it, as the superuser.

    ``compared`` holds the baseline's run, then the reform's. Return the
    report run's ID.
    """
    outcome = outcome or ReportRunOutcome()
    baseline, reform = compared
    report_id = uuid4()
    run_id = uuid4()
    connection.execute(
        insert(reports).values(
            id=report_id,
            label="An earlier report",
            country=economy.catalog.country,
            type="economy",
            tax_benefit_model_id=economy.catalog.model_id,
            baseline_simulation_id=baseline.simulation_id,
            reform_simulation_id=reform.simulation_id,
            dataset_id=economy.dataset_id,
            region_id=economy.region_id,
            year=2026,
            inputs=OPTIONS if outcome.inputs is None else outcome.inputs,
        )
    )
    finished = outcome.status in {ReportRunStatus.SUCCEEDED, ReportRunStatus.FAILED}
    connection.execute(
        insert(report_runs).values(
            id=run_id,
            report_id=report_id,
            baseline_simulation_run_id=baseline.simulation_run_id,
            reform_simulation_run_id=reform.simulation_run_id,
            country_package_version=COUNTRY_PACKAGE_VERSION,
            policyengine_version=POLICYENGINE_VERSION,
            status=outcome.status,
            trigger=ReportRunTrigger.INITIAL,
            completed_at=COMPLETED_AT if finished else None,
            error_message=outcome.error_message,
        )
    )
    return run_id


def insert_reusable_run(
    connection: Connection,
    economy: EconomyCatalog,
    shape: RunShape | None = None,
    *,
    inputs: dict[str, Any] | None = None,
) -> StoredRun:
    """Insert a run shaped ``shape`` that a succeeded report run used, with ``inputs``.

    The report run's other simulation run failed, so it is never reusable.
    """
    population = economy.population()
    reused = insert_simulation_run(connection, population, shape)
    other_side = insert_simulation_run(
        connection, population, RunShape(status=SimulationStatus.FAILED)
    )
    insert_report_run_naming(
        connection, economy, (reused, other_side), ReportRunOutcome(inputs=inputs)
    )
    return reused
