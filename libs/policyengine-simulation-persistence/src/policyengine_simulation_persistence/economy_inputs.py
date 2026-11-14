"""What an economy request is read from: its catalog rows and its sides' values.

A request names a region code, a dataset name (or the region's default) and
a year, for the .py version of the default worker, and each side's stored
policy and dynamic. ``read_catalog_rows`` reads the catalog version's model,
region and dataset; ``read_report_sides`` and ``read_window_sides`` read
each side's values on that catalog version, with the runs a side may reuse
when a search is given. ``EconomyRunStore`` runs these in its transactions.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, Literal, get_args

from policyengine_simulation_persistence.economy_run_records import (
    CatalogDataset,
    CatalogRegion,
    EconomyPopulation,
    ReusableRun,
    RunVersions,
    SimulationChoice,
    find_reusable_run,
    find_reusable_run_of,
    read_dataset,
    read_dataset_by_id,
    read_model_id,
    read_region,
)
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
    from collections.abc import Callable, Iterable
    from uuid import UUID

    from sqlalchemy import Connection

    from policyengine_simulation_persistence.policy_reform import PolicyReform

# Which of a report's simulations a value belongs to.
type ReportSide = Literal["baseline", "reform"]
# Finds a report side's reusable run, given the side, its policy and dynamic,
# and the run the other side reuses, which it must not.
type ReusableRunSearch = Callable[
    [ReportSide, SimulationChoice, UUID | None], ReusableRun | None
]
# The errors a simulation's own policy or dynamic can raise.
type SideRefusal = (
    UnknownPolicyError
    | UnknownDynamicError
    | MissingPolicyParametersError
    | OverlappingReformError
)
# Caught by the type itself, so the two never name different errors.
SIDE_ERRORS: tuple[type[SideRefusal], ...] = get_args(SideRefusal.__value__)


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


def read_catalog_rows(
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


def read_simulation_values(
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


def read_side_values(
    connection: Connection,
    request: EconomyRequest,
    catalog: EconomyCatalogRows,
    side: ReportSide,
    choice: SimulationChoice,
) -> SimulationInputs:
    """Read one report side's values; raise ReportSideError naming a refused side."""
    try:
        return read_simulation_values(
            connection, request.country, catalog.catalog_version_id, choice
        )
    except SIDE_ERRORS as error:
        raise ReportSideError(side, error) from error


def read_report_sides(  # noqa: PLR0913  # the request's rows, both sides and the search
    connection: Connection,
    request: EconomyRequest,
    catalog: EconomyCatalogRows,
    *,
    baseline: SimulationChoice,
    reform: SimulationChoice,
    search: ReusableRunSearch | None,
) -> EconomyReportInputs:
    """Read both sides' values and, with ``search``, each side's reusable run.

    The reform never gets the run the baseline reuses.
    """
    sides: list[SimulationInputs] = []
    excluded_run_id: UUID | None = None
    requested: tuple[tuple[ReportSide, SimulationChoice], ...] = (
        ("baseline", baseline),
        ("reform", reform),
    )
    for side, choice in requested:
        inputs = read_side_values(connection, request, catalog, side, choice)
        reusable_run = None if search is None else search(side, choice, excluded_run_id)
        if reusable_run is not None:
            excluded_run_id = reusable_run.simulation_run_id
            inputs = replace(inputs, reusable_run=reusable_run)
        sides.append(inputs)
    return EconomyReportInputs(catalog=catalog, baseline=sides[0], reform=sides[1])


def read_window_sides(  # noqa: PLR0913  # the request's rows, both sides and each year's search
    connection: Connection,
    request: EconomyRequest,
    catalog: EconomyCatalogRows,
    *,
    baseline: SimulationChoice,
    reform: SimulationChoice,
    searches: Iterable[tuple[int, ReusableRunSearch | None]],
) -> BudgetWindowInputs:
    """Read both sides' values, and each year's reusable runs with its search.

    A year without a search reuses nothing; in each year the reform never
    gets the run the baseline reuses.
    """
    baseline_inputs = read_side_values(
        connection, request, catalog, "baseline", baseline
    )
    reform_inputs = read_side_values(connection, request, catalog, "reform", reform)
    reusable_runs: dict[int, tuple[ReusableRun | None, ReusableRun | None]] = {}
    for year, search in searches:
        baseline_run = reform_run = None
        if search is not None:
            baseline_run = search("baseline", baseline, None)
            reform_run = search(
                "reform",
                reform,
                None if baseline_run is None else baseline_run.simulation_run_id,
            )
        reusable_runs[year] = (baseline_run, reform_run)
    return BudgetWindowInputs(
        catalog=catalog,
        baseline=baseline_inputs,
        reform=reform_inputs,
        reusable_runs=reusable_runs,
    )


def search_by_definition(
    connection: Connection, population: EconomyPopulation, reuse: RunReuse
) -> ReusableRunSearch:
    """Search any simulation with the side's definition (a new report's sides)."""

    def search(
        _side: ReportSide, choice: SimulationChoice, excluded_run_id: UUID | None
    ) -> ReusableRun | None:
        return find_reusable_run(
            connection,
            choice=choice,
            population=population,
            versions=reuse.versions,
            inputs=reuse.inputs,
            output_uri_prefix=reuse.output_uri_prefix,
            excluded_run_id=excluded_run_id,
        )

    return search


def search_own_runs(
    connection: Connection,
    population: EconomyPopulation,
    reuse: RunReuse,
    simulation_ids: dict[ReportSide, UUID],
) -> ReusableRunSearch:
    """Search only each side's own simulation's runs (a stored report's re-run)."""

    def search(
        side: ReportSide, _choice: SimulationChoice, excluded_run_id: UUID | None
    ) -> ReusableRun | None:
        return find_reusable_run_of(
            connection,
            simulation_id=simulation_ids[side],
            population=population,
            versions=reuse.versions,
            inputs=reuse.inputs,
            output_uri_prefix=reuse.output_uri_prefix,
            excluded_run_id=excluded_run_id,
        )

    return search
