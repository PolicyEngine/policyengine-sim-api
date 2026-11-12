"""The economy store: each read and write in a transaction of its own.

The Simulation Entrypoint creates an economy report, or a standalone economy
simulation, in three steps, each its own transaction, with the Modal call
started between them: it reads the request's inputs, creates the rows, then
records the call or fails the run it started. ``EconomyRunStore`` runs the
reads and writes of ``economy_run_records`` and ``policy_reform`` that way.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, get_args

from sqlalchemy.exc import SQLAlchemyError

from policyengine_simulation_persistence.budget_window_records import (
    insert_budget_window_report,
)
from policyengine_simulation_persistence.economy_run_reads import (
    read_economy_report,
    read_economy_simulation,
)
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
    MissingPolicyParametersError,
    OverlappingReformError,
    UnknownDynamicError,
    UnknownPolicyError,
    combine_reforms,
    read_dynamic_reform,
    read_policy_reform,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from contextlib import AbstractContextManager
    from uuid import UUID

    from sqlalchemy import Connection, Engine

    from policyengine_simulation_persistence.budget_window_records import (
        NewBudgetWindowReport,
    )
    from policyengine_simulation_persistence.economy_run_reads import (
        EconomyReportState,
        EconomySimulationState,
    )
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
# Which of a report's simulations a value belongs to.
type ReportSide = Literal["baseline", "reform"]
# The errors a simulation's own policy or dynamic can raise.
type SideRefusal = (
    UnknownPolicyError
    | UnknownDynamicError
    | MissingPolicyParametersError
    | OverlappingReformError
)
# Caught by the type itself, so the two never name different errors.
_SIDE_ERRORS: tuple[type[SideRefusal], ...] = get_args(SideRefusal.__value__)


class UnknownRegionError(LookupError):
    """The catalog version has no region with the requested code."""


class UnknownDatasetError(LookupError):
    """The catalog version has no dataset with the requested name."""


class ReportSideError(Exception):
    """A report side's policy or dynamic is refused.

    ``side`` names the side, and ``error`` is the policy reader's refusal:
    UnknownPolicyError, UnknownDynamicError, MissingPolicyParametersError or
    OverlappingReformError.
    """

    def __init__(self, side: ReportSide, error: SideRefusal) -> None:
        """Keep the side and the refusal."""
        super().__init__(f"the report's {side}: {error}")
        self.side: ReportSide = side
        self.error = error


class EconomyRecordOutcomeUnknownError(RuntimeError):
    """A create's statements ran, but its commit failed: the rows may exist.

    PostgreSQL may have applied the commit before the connection failed, so
    the caller must not answer that nothing was created.
    """


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


@dataclass(frozen=True)
class BudgetWindowInputs:
    """What a budget-window report is read from, and each year's reusable runs.

    ``baseline`` and ``reform`` hold each side's values, the same in every
    year. ``reusable_runs`` gives, by year, the baseline's and the reform's
    reusable run, each None when there is none.
    """

    catalog: EconomyCatalogRows
    baseline: SimulationInputs
    reform: SimulationInputs
    reusable_runs: dict[int, tuple[ReusableRun | None, ReusableRun | None]]


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

        Raise UnknownCatalogVersionError, UnknownRegionError or
        UnknownDatasetError, or the policy reader's UnknownPolicyError,
        UnknownDynamicError or MissingPolicyParametersError, or
        OverlappingReformError.
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
        UnknownCatalogVersionError, UnknownRegionError or UnknownDatasetError,
        or ReportSideError naming the side whose policy or dynamic is refused.
        """
        with self._transaction() as connection:
            catalog = _catalog_rows(connection, request)
            population = catalog.population(request.year)
            sides = []
            excluded_run_id = None
            sides_requested: tuple[tuple[ReportSide, SimulationChoice], ...] = (
                ("baseline", baseline),
                ("reform", reform),
            )
            for side, choice in sides_requested:
                try:
                    inputs = _simulation_inputs(
                        connection, request.country, catalog.catalog_version_id, choice
                    )
                except _SIDE_ERRORS as error:
                    raise ReportSideError(side, error) from error
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

    def read_budget_window_inputs(
        self,
        request: EconomyRequest,
        *,
        baseline: SimulationChoice,
        reform: SimulationChoice,
        reuse: RunReuse | None,
        years: Sequence[int],
    ) -> BudgetWindowInputs:
        """Read a window's catalog rows and both sides' values, then each year's runs.

        ``request.year`` is the window's first year. With ``reuse``, each
        year's sides get the runs ``find_reusable_run`` finds for that year,
        and the reform never gets the baseline's. Raise the errors
        ``read_report_inputs`` raises.
        """
        with self._transaction() as connection:
            catalog = _catalog_rows(connection, request)
            baseline_inputs = _side_inputs(
                connection, request, catalog, "baseline", baseline
            )
            reform_inputs = _side_inputs(connection, request, catalog, "reform", reform)
            reusable_runs: dict[int, tuple[ReusableRun | None, ReusableRun | None]] = {}
            for year in years:
                baseline_run = reform_run = None
                if reuse is not None:
                    population = catalog.population(year)
                    baseline_run = find_reusable_run(
                        connection,
                        choice=baseline,
                        population=population,
                        versions=reuse.versions,
                        inputs=reuse.inputs,
                        output_uri_prefix=reuse.output_uri_prefix,
                    )
                    reform_run = find_reusable_run(
                        connection,
                        choice=reform,
                        population=population,
                        versions=reuse.versions,
                        inputs=reuse.inputs,
                        output_uri_prefix=reuse.output_uri_prefix,
                        excluded_run_id=(
                            None
                            if baseline_run is None
                            else baseline_run.simulation_run_id
                        ),
                    )
                reusable_runs[year] = (baseline_run, reform_run)
        return BudgetWindowInputs(
            catalog=catalog,
            baseline=baseline_inputs,
            reform=reform_inputs,
            reusable_runs=reusable_runs,
        )

    def create_budget_window_report(self, report: NewBudgetWindowReport) -> None:
        """Create the window's report, its budget-window run and each year's runs.

        Raise ``insert_budget_window_report``'s errors, and
        EconomyRecordOutcomeUnknownError when the commit fails after the
        rows were inserted.
        """
        self._create(lambda connection: insert_budget_window_report(connection, report))

    def create_report(self, report: NewEconomyReport) -> None:
        """Create the report, its new simulations and its runs.

        Raise ``insert_economy_report``'s errors, and
        EconomyRecordOutcomeUnknownError when the commit fails after the
        rows were inserted.
        """
        self._create(lambda connection: insert_economy_report(connection, report))

    def create_simulation(self, simulation: NewEconomySimulation) -> None:
        """Create the simulation and its run; see ``insert_economy_simulation``.

        Raise EconomyRecordOutcomeUnknownError as ``create_report`` does.
        """
        self._create(
            lambda connection: insert_economy_simulation(connection, simulation)
        )

    def _create(self, insert: Callable[[Connection], None]) -> None:
        """Run ``insert`` in a transaction, telling a failed commit apart.

        An error from ``insert`` leaves nothing written and propagates. An
        error once it has run is the commit's, after which the rows may
        exist: EconomyRecordOutcomeUnknownError.
        """
        inserted = False
        try:
            with self._transaction() as connection:
                insert(connection)
                inserted = True
        except SQLAlchemyError as error:
            if not inserted:
                raise
            msg = "the create's commit failed after its rows were inserted"
            raise EconomyRecordOutcomeUnknownError(msg) from error

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

    def read_report(self, report_id: UUID) -> EconomyReportState | None:
        """Return the report with its newest run, or None when there is none."""
        with self._transaction() as connection:
            return read_economy_report(connection, report_id)

    def read_simulation(self, simulation_id: UUID) -> EconomySimulationState | None:
        """Return the simulation with its newest run, or None when there is none."""
        with self._transaction() as connection:
            return read_economy_simulation(connection, simulation_id)


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
    return SimulationInputs(
        choice=choice,
        reform=read_simulation_reform(
            connection,
            country=country,
            catalog_version_id=catalog_version_id,
            choice=choice,
        ),
    )


def read_simulation_reform(
    connection: Connection,
    *,
    country: str,
    catalog_version_id: UUID,
    choice: SimulationChoice,
) -> PolicyReform:
    """Read a simulation's policy and dynamic values on a catalog version, combined.

    A submission and a restart of its run read them this way. Raise the
    policy reader's errors, or OverlappingReformError.
    """
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
    return combine_reforms(policy_reform, dynamic_reform)


def _side_inputs(
    connection: Connection,
    request: EconomyRequest,
    catalog: EconomyCatalogRows,
    side: ReportSide,
    choice: SimulationChoice,
) -> SimulationInputs:
    """Read one report side's values; raise ReportSideError naming a refused side."""
    try:
        return _simulation_inputs(
            connection, request.country, catalog.catalog_version_id, choice
        )
    except _SIDE_ERRORS as error:
        raise ReportSideError(side, error) from error
