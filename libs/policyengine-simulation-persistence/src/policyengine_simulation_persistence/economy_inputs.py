"""What an economy request is read from: its catalog rows and its sides' values.

A request names a region code, a dataset name (or the region's default, or
the catalog version's local-area dataset for a report created with
``data_source``) and a year, for the .py version of the default worker, and
each side's stored policy and dynamic. ``read_catalog_rows`` reads the
catalog version's model, region and dataset; ``read_report_sides`` and
``read_window_sides`` read each side's values on that catalog version.
``EconomyRunStore`` runs these in its transactions.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, get_args

from policyengine_simulation_persistence.economy_run_records import (
    CatalogDataset,
    CatalogRegion,
    EconomyPopulation,
    SimulationChoice,
    read_dataset,
    read_dataset_by_id,
    read_local_area_dataset,
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
    from uuid import UUID

    from sqlalchemy import Connection

    from policyengine_simulation_persistence.policy_reform import PolicyReform

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
SIDE_ERRORS: tuple[type[SideRefusal], ...] = get_args(SideRefusal.__value__)


class UnknownRegionError(LookupError):
    """The catalog version has no region with the requested code."""


class UnknownDatasetError(LookupError):
    """The catalog version has no dataset with the requested name."""


class LocalAreaDatasetMismatchError(LookupError):
    """The catalog doesn't give both area types the dataset the worker routes them to.

    ``declared_dataset`` is the dataset the default worker declares it
    calculates a report's local areas on. The catalog version's
    constituency and local-authority regions use another, or one each.
    """

    def __init__(self, declared_dataset: str) -> None:
        """Keep the declared dataset."""
        super().__init__(
            "the catalog version's constituency and local-authority regions do "
            f"not all use {declared_dataset}"
        )
        self.declared_dataset = declared_dataset


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
    """One simulation's policy and dynamic, and the values they set together."""

    choice: SimulationChoice
    reform: PolicyReform


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
    """What a budget-window report is read from: the catalog rows and both sides.

    ``baseline`` and ``reform`` hold each side's values, the same in every
    year.
    """

    catalog: EconomyCatalogRows
    baseline: SimulationInputs
    reform: SimulationInputs


def read_catalog_rows(
    connection: Connection,
    request: EconomyRequest,
    *,
    local_area_dataset_name: str | None = None,
) -> EconomyCatalogRows:
    """Read the catalog version, model, region and dataset a request names.

    The dataset is the region's default, or the one ``request`` names (a
    re-run's stored dataset), or, with ``local_area_dataset_name``, the
    catalog version's local-area dataset, which must have that name (a
    report created with ``data_source``). Raise ValueError, before any read,
    when both name a dataset; otherwise UnknownCatalogVersionError,
    UnknownRegionError, UnknownDatasetError or LocalAreaDatasetMismatchError.
    """
    if local_area_dataset_name is not None and request.dataset_name is not None:
        msg = (
            "a catalog read takes either a dataset name or the local-area "
            "dataset, not both"
        )
        raise ValueError(msg)
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
    if local_area_dataset_name is not None:
        dataset = read_local_area_dataset(
            connection, catalog_version_id=catalog_version_id
        )
        if dataset is None or dataset.name != local_area_dataset_name:
            raise LocalAreaDatasetMismatchError(local_area_dataset_name)
    elif request.dataset_name is None:
        dataset = read_dataset_by_id(
            connection,
            catalog_version_id=catalog_version_id,
            dataset_id=region.default_dataset_id,
        )
    else:
        dataset = read_dataset(
            connection, catalog_version_id=catalog_version_id, name=request.dataset_name
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


def read_report_sides(
    connection: Connection,
    request: EconomyRequest,
    catalog: EconomyCatalogRows,
    *,
    baseline: SimulationChoice,
    reform: SimulationChoice,
) -> EconomyReportInputs:
    """Read both sides' values, the baseline's first."""
    return EconomyReportInputs(
        catalog=catalog,
        baseline=read_side_values(connection, request, catalog, "baseline", baseline),
        reform=read_side_values(connection, request, catalog, "reform", reform),
    )


def read_window_sides(
    connection: Connection,
    request: EconomyRequest,
    catalog: EconomyCatalogRows,
    *,
    baseline: SimulationChoice,
    reform: SimulationChoice,
) -> BudgetWindowInputs:
    """Read both sides' values, the baseline's first, for every year of a window."""
    return BudgetWindowInputs(
        catalog=catalog,
        baseline=read_side_values(connection, request, catalog, "baseline", baseline),
        reform=read_side_values(connection, request, catalog, "reform", reform),
    )
