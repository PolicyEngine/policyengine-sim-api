"""The economy store: each read and write in a transaction of its own.

The Simulation Entrypoint creates an economy report, or a standalone economy
simulation, in three steps, each its own transaction, with the Modal call
started between them: it reads the request's inputs, creates the rows, then
records the call or fails the run it started. ``EconomyRunStore`` runs the
reads and writes of ``economy_run_records`` and ``policy_reform`` that way.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from policyengine_simulation_persistence.economy_run_records import (
    CatalogDataset,
    CatalogRegion,
    EconomyPopulation,
    ReusableRun,
    RunVersions,
    SimulationChoice,
    fail_unstarted_report_run,
    fail_unstarted_simulation_run,
    find_reusable_run,
    insert_economy_report,
    insert_economy_simulation,
    read_dataset,
    read_dataset_by_id,
    read_model_id,
    read_region,
    record_report_run_call,
    record_simulation_run_call,
)
from policyengine_simulation_persistence.engine import bounded_transaction
from policyengine_simulation_persistence.household_job_records import (
    read_catalog_version_id,
)
from policyengine_simulation_persistence.household_job_store import (
    UnknownCatalogVersionError,
)
from policyengine_simulation_persistence.policy_reform import (
    combine_reforms,
    read_dynamic_reform,
    read_policy_reform,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from contextlib import AbstractContextManager
    from uuid import UUID

    from sqlalchemy import Connection, Engine

    from policyengine_simulation_persistence.economy_run_records import (
        NewEconomyReport,
        NewEconomySimulation,
    )
    from policyengine_simulation_persistence.policy_reform import PolicyReform
    from policyengine_simulation_persistence.tables import (
        ReportRunStatus,
        SimulationStatus,
    )

type TransactionFactory = Callable[[], AbstractContextManager[Connection]]


class UnknownRegionError(LookupError):
    """The catalog version has no region with the requested code."""


class UnknownDatasetError(LookupError):
    """The catalog version has no dataset with the requested name."""


@dataclass(frozen=True)
class EconomyRequest:
    """What an economy simulation or report request names, apart from policies.

    ``policyengine_version`` is the .py version the calculation runs on,
    which chooses the country's catalog version. ``dataset_name`` None means
    the region's default dataset.
    """

    country: str
    policyengine_version: str
    region_code: str
    dataset_name: str | None
    year: int


@dataclass(frozen=True)
class RunReuse:
    """How a report looks for simulation runs to reuse (``find_reusable_run``).

    ``versions`` are those the new runs would calculate on, ``inputs`` the
    report's options, and ``output_uri_prefix`` the output bucket's
    ``gs://<bucket>/``.
    """

    versions: RunVersions
    inputs: dict[str, Any]
    output_uri_prefix: str


@dataclass(frozen=True)
class EconomyCatalogRows:
    """The catalog rows a request is calculated on."""

    catalog_version_id: UUID
    model_id: UUID
    region: CatalogRegion
    dataset: CatalogDataset

    def population(self, year: int) -> EconomyPopulation:
        """Return what every simulation of the request calculates on."""
        return EconomyPopulation(
            tax_benefit_model_version_id=self.catalog_version_id,
            region_id=self.region.id,
            dataset_id=self.dataset.id,
            year=year,
        )


@dataclass(frozen=True)
class SimulationInputs:
    """One simulation's policy and dynamic, and the values they set together.

    ``reusable_run`` names a succeeded run the simulation may use instead of
    a new one, when the request looked for one and one exists.
    """

    choice: SimulationChoice
    reform: PolicyReform
    reusable_run: ReusableRun | None = None


@dataclass(frozen=True)
class EconomySimulationInputs:
    """What a standalone economy simulation is read from."""

    catalog: EconomyCatalogRows
    simulation: SimulationInputs


@dataclass(frozen=True)
class EconomyReportInputs:
    """What an economy report is read from: the catalog rows and both sides."""

    catalog: EconomyCatalogRows
    baseline: SimulationInputs
    reform: SimulationInputs


class EconomyRunStore:
    """Read and write economy simulations and reports, each in its own transaction."""

    def __init__(self, transaction: TransactionFactory) -> None:
        """Open each transaction with ``transaction``, which commits on success."""
        self._transaction = transaction

    @classmethod
    def from_engine(
        cls, engine: Engine, *, statement_timeout_milliseconds: int
    ) -> EconomyRunStore:
        """Use ``engine``, bounding each statement to the given time."""

        def transaction() -> AbstractContextManager[Connection]:
            return bounded_transaction(
                engine, statement_timeout_milliseconds=statement_timeout_milliseconds
            )

        return cls(transaction)

    def read_simulation_inputs(
        self, request: EconomyRequest, choice: SimulationChoice
    ) -> EconomySimulationInputs:
        """Read a standalone simulation's catalog rows and values together.

        Raise the errors ``read_report_inputs`` names.
        """
        with self._transaction() as connection:
            catalog = _catalog_rows(connection, request)
            simulation = _simulation_inputs(
                connection, request.country, catalog.catalog_version_id, choice
            )
        return EconomySimulationInputs(catalog=catalog, simulation=simulation)

    def read_report_inputs(
        self,
        request: EconomyRequest,
        *,
        baseline: SimulationChoice,
        reform: SimulationChoice,
        reuse: RunReuse | None,
    ) -> EconomyReportInputs:
        """Read a report's catalog rows, both sides' values and reusable runs.

        With ``reuse``, each side gets the run ``find_reusable_run`` finds,
        and the reform never gets the baseline's. Raise
        UnknownCatalogVersionError, UnknownRegionError, UnknownDatasetError,
        the policy reader's UnknownPolicyError, UnknownDynamicError or
        MissingPolicyParametersError, or OverlappingReformError.
        """
        with self._transaction() as connection:
            catalog = _catalog_rows(connection, request)
            population = catalog.population(request.year)
            sides = []
            excluded_run_id = None
            for choice in (baseline, reform):
                inputs = _simulation_inputs(
                    connection, request.country, catalog.catalog_version_id, choice
                )
                if reuse is not None:
                    reusable_run = find_reusable_run(
                        connection,
                        choice=choice,
                        population=population,
                        versions=reuse.versions,
                        inputs=reuse.inputs,
                        output_uri_prefix=reuse.output_uri_prefix,
                        excluded_run_id=excluded_run_id,
                    )
                    if reusable_run is not None:
                        excluded_run_id = reusable_run.simulation_run_id
                        inputs = SimulationInputs(
                            choice=inputs.choice,
                            reform=inputs.reform,
                            reusable_run=reusable_run,
                        )
                sides.append(inputs)
        return EconomyReportInputs(catalog=catalog, baseline=sides[0], reform=sides[1])

    def create_report(self, report: NewEconomyReport) -> None:
        """Create the report, its new simulations and its runs."""
        with self._transaction() as connection:
            insert_economy_report(connection, report)

    def create_simulation(self, simulation: NewEconomySimulation) -> None:
        """Create the simulation and its run; see ``insert_economy_simulation``."""
        with self._transaction() as connection:
            insert_economy_simulation(connection, simulation)

    def record_report_call(
        self, report_run_id: UUID, modal_call_id: str
    ) -> ReportRunStatus | None:
        """Record the report run's call ID while it is pending with none."""
        with self._transaction() as connection:
            return record_report_run_call(
                connection, report_run_id=report_run_id, modal_call_id=modal_call_id
            )

    def fail_unstarted_report(
        self,
        report_run_id: UUID,
        simulation_run_ids: Sequence[UUID],
        error_message: str,
    ) -> bool:
        """Fail the report run, and its new simulation runs, while it is unstarted."""
        with self._transaction() as connection:
            return fail_unstarted_report_run(
                connection,
                report_run_id=report_run_id,
                simulation_run_ids=simulation_run_ids,
                error_message=error_message,
            )

    def record_simulation_call(
        self, simulation_run_id: UUID, modal_call_id: str
    ) -> SimulationStatus | None:
        """Record the simulation run's call ID while it is pending with none."""
        with self._transaction() as connection:
            return record_simulation_run_call(
                connection,
                simulation_run_id=simulation_run_id,
                modal_call_id=modal_call_id,
            )

    def fail_unstarted_simulation(
        self, simulation_run_id: UUID, error_message: str
    ) -> bool:
        """Fail the simulation run while it is pending with no call ID."""
        with self._transaction() as connection:
            return fail_unstarted_simulation_run(
                connection,
                simulation_run_id=simulation_run_id,
                error_message=error_message,
            )


def _catalog_rows(
    connection: Connection, request: EconomyRequest
) -> EconomyCatalogRows:
    """Read the catalog version, model, region and dataset a request names.

    Raise UnknownCatalogVersionError, UnknownRegionError or
    UnknownDatasetError.
    """
    country = request.country
    catalog_version_id = read_catalog_version_id(
        connection,
        country=country,
        policyengine_version=request.policyengine_version,
    )
    model_id = read_model_id(connection, country=country)
    if catalog_version_id is None or model_id is None:
        msg = f"{country} has no catalog version for .py {request.policyengine_version}"
        raise UnknownCatalogVersionError(msg)
    region = read_region(
        connection,
        catalog_version_id=catalog_version_id,
        region_code=request.region_code,
    )
    if region is None:
        msg = (
            f"{country}'s catalog version for .py {request.policyengine_version} "
            f"has no region {request.region_code}"
        )
        raise UnknownRegionError(msg)
    dataset = (
        read_dataset_by_id(
            connection,
            catalog_version_id=catalog_version_id,
            dataset_id=region.default_dataset_id,
        )
        if request.dataset_name is None
        else read_dataset(
            connection,
            catalog_version_id=catalog_version_id,
            name=request.dataset_name,
        )
    )
    if dataset is None:
        msg = (
            f"{country}'s catalog version for .py {request.policyengine_version} "
            f"has no dataset {request.dataset_name}"
        )
        raise UnknownDatasetError(msg)
    return EconomyCatalogRows(
        catalog_version_id=catalog_version_id,
        model_id=model_id,
        region=region,
        dataset=dataset,
    )


def _simulation_inputs(
    connection: Connection,
    country: str,
    catalog_version_id: UUID,
    choice: SimulationChoice,
) -> SimulationInputs:
    """Read one simulation's policy and dynamic values, combined."""
    policy_reform = (
        {}
        if choice.policy_id is None
        else read_policy_reform(
            connection,
            country=country,
            policy_id=choice.policy_id,
            catalog_version_id=catalog_version_id,
        )
    )
    dynamic_reform = (
        {}
        if choice.dynamic_id is None
        else read_dynamic_reform(
            connection,
            dynamic_id=choice.dynamic_id,
            catalog_version_id=catalog_version_id,
        )
    )
    return SimulationInputs(
        choice=choice, reform=combine_reforms(policy_reform, dynamic_reform)
    )
