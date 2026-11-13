"""The catalog rows an economy request is calculated on, read in its transaction.

A request names a country, the .py version it runs on, a region by code and
a year; the store reads the country's catalog version for that .py version,
its model, the region and the dataset: the region's default, a re-run's
stored dataset by name, or the catalog version's local-area dataset for a
report created with ``data_source``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from policyengine_simulation_persistence.economy_run_records import (
    CatalogDataset,
    CatalogRegion,
    EconomyPopulation,
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

if TYPE_CHECKING:
    from uuid import UUID

    from sqlalchemy import Connection


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
