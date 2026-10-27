"""Read the Public API's catalog and policy rows a v2 economy run is resolved from.

A v2 economy run uses one catalog version: one published policyengine.py
version of one country's model. The Simulation Entrypoint reads, from the
Public API's tables in the same database:

- the catalog version, by country and policyengine.py version;
- the run's region and dataset rows in that version, with the household
  filter that selects the region (``population``);
- the parameter values its policy, and dynamic if any, set, by parameter
  name, checked against the run's version (``parameter_changes``);
- the names of the UK's constituencies and local authorities, which a stored
  result is read back with (``area_labels``).

The runtime role needs ``SELECT`` on ``CATALOG_READ_TABLES``, which the owner
of the Public API's tables grants. These tables are described here with only
the columns read, in a metadata object of their own; nothing here writes them.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    DateTime,
    MetaData,
    String,
    Table,
    Uuid,
    select,
)

from policyengine_simulation_persistence.economy_result_storage import (
    EconomyResultLabels,
    GeographyLabel,
)

if TYPE_CHECKING:
    from collections.abc import Mapping
    from datetime import datetime
    from uuid import UUID

    from sqlalchemy import ColumnElement, Connection, Engine, RowMapping

catalog_metadata = MetaData()

tax_benefit_models = Table(
    "tax_benefit_models",
    catalog_metadata,
    Column("id", Uuid(as_uuid=True), primary_key=True),
    Column("name", String(32)),
)
tax_benefit_model_versions = Table(
    "tax_benefit_model_versions",
    catalog_metadata,
    Column("id", Uuid(as_uuid=True), primary_key=True),
    Column("model_id", Uuid(as_uuid=True)),
    Column("version", String(128)),
)
regions = Table(
    "regions",
    catalog_metadata,
    Column("id", Uuid(as_uuid=True), primary_key=True),
    Column("code", String(255)),
    Column("label", String(255)),
    Column("region_type", String(64)),
    Column("requires_filter", Boolean),
    Column("filter_field", String(128)),
    Column("filter_value", String(255)),
    Column("filter_strategy", String(64)),
    Column("state_code", String(16)),
    Column("tax_benefit_model_version_id", Uuid(as_uuid=True)),
    Column("default_dataset_id", Uuid(as_uuid=True)),
)
datasets = Table(
    "datasets",
    catalog_metadata,
    Column("id", Uuid(as_uuid=True), primary_key=True),
    Column("name", String(255)),
    Column("tax_benefit_model_version_id", Uuid(as_uuid=True)),
)
parameters = Table(
    "parameters",
    catalog_metadata,
    Column("id", Uuid(as_uuid=True), primary_key=True),
    Column("name", String(512)),
    Column("tax_benefit_model_version_id", Uuid(as_uuid=True)),
)
parameter_values = Table(
    "parameter_values",
    catalog_metadata,
    Column("id", Uuid(as_uuid=True), primary_key=True),
    Column("parameter_id", Uuid(as_uuid=True)),
    Column("value_json", JSON),
    Column("start_date", DateTime(timezone=True)),
    Column("end_date", DateTime(timezone=True)),
    Column("policy_id", Uuid(as_uuid=True)),
    Column("dynamic_id", Uuid(as_uuid=True)),
)
# The Public API tables the runtime role reads; the operator grants SELECT.
CATALOG_READ_TABLES = tuple(catalog_metadata.tables)

ROW_FILTER = "row_filter"
STATE_REGION_TYPE = "state"
NATIONAL_REGION_TYPE = "national"
UK_AREA_REGION_TYPES = ("constituency", "local_authority")
# A refused policy's error names at most this many parameters.
MAX_LISTED_PARAMETERS = 10


class CatalogError(LookupError):
    """The catalog version has no such region or dataset, or cannot run it."""


class PolicyParametersMissingError(ValueError):
    """The policy changes parameters the run's catalog version does not have."""

    def __init__(self, names: tuple[str, ...]) -> None:
        """Name the missing parameters (at most ``MAX_LISTED_PARAMETERS``)."""
        self.names = names
        shown = ", ".join(names[:MAX_LISTED_PARAMETERS])
        more = len(names) - MAX_LISTED_PARAMETERS
        suffix = f" and {more} more" if more > 0 else ""
        super().__init__(
            "the policy changes parameters this catalog version does not have: "
            f"{shown}{suffix}"
        )


@dataclass(frozen=True)
class CatalogVersion:
    """One published policyengine.py version of one country's catalog."""

    id: UUID
    country: str
    policyengine_version: str


@dataclass(frozen=True)
class RegionFilter:
    """The household column and stored value that select a region's households."""

    field: str
    value: str


@dataclass(frozen=True)
class RegionPopulation:
    """A region and the dataset a run uses, as one catalog version has them.

    ``state_filters`` holds each US state's filter for a US national run,
    which calculates its states in separate segments; None otherwise.
    """

    region_code: str
    region_type: str
    dataset_id: UUID
    dataset_name: str
    row_filter: RegionFilter | None
    state_code: str | None
    state_filters: Mapping[str, RegionFilter] | None


def _country(model_name: str) -> str:
    return model_name.removeprefix("policyengine-")


def _period_key(start: datetime, end: datetime | None) -> str:
    """Return the policy period key .py reads: a date, or an inclusive range."""
    if end is None:
        return f"{start:%Y-%m-%d}"
    return f"{start:%Y-%m-%d}.{end:%Y-%m-%d}"


def _row_filter(row: RowMapping) -> RegionFilter | None:
    """Return a region's household filter; raise CatalogError for another kind."""
    if not row["requires_filter"]:
        return None
    if row["filter_strategy"] != ROW_FILTER:
        msg = (
            f"the region {row['code']} selects its households by "
            f"{row['filter_strategy']}, which economy runs cannot apply"
        )
        raise CatalogError(msg)
    return RegionFilter(field=row["filter_field"], value=row["filter_value"])


class CatalogReader:
    """Typed SQLAlchemy Core reads of the Public API's catalog and policy rows."""

    def __init__(self, engine: Engine) -> None:
        """Use ``engine``, which connects as the runtime role."""
        self.engine = engine

    def _version(self, condition: ColumnElement[bool]) -> CatalogVersion | None:
        with self.engine.connect() as connection:
            row = (
                connection.execute(
                    select(
                        tax_benefit_model_versions.c.id,
                        tax_benefit_model_versions.c.version,
                        tax_benefit_models.c.name,
                    )
                    .join(
                        tax_benefit_models,
                        tax_benefit_models.c.id
                        == tax_benefit_model_versions.c.model_id,
                    )
                    .where(condition)
                )
                .mappings()
                .first()
            )
        if row is None:
            return None
        return CatalogVersion(
            id=row["id"],
            country=_country(row["name"]),
            policyengine_version=row["version"],
        )

    def catalog_version(
        self, country: str, policyengine_version: str
    ) -> CatalogVersion | None:
        """Return the country's catalog version for a policyengine.py version."""
        return self._version(
            (tax_benefit_models.c.name == f"policyengine-{country}")
            & (tax_benefit_model_versions.c.version == policyengine_version)
        )

    def catalog_version_by_id(self, version_id: UUID) -> CatalogVersion | None:
        """Return the catalog version with this ID, or None."""
        return self._version(tax_benefit_model_versions.c.id == version_id)

    def population(
        self, version: CatalogVersion, region_code: str, dataset_name: str | None
    ) -> RegionPopulation:
        """Return the region and dataset a run of this version uses.

        ``dataset_name`` names a dataset of the version; None means the
        region's default dataset. Raise CatalogError when the version has no
        such region or dataset, or selects the region's households in a way
        economy runs cannot apply.
        """
        with self.engine.connect() as connection:
            region = (
                connection.execute(
                    select(regions).where(
                        regions.c.tax_benefit_model_version_id == version.id,
                        regions.c.code == region_code,
                    )
                )
                .mappings()
                .first()
            )
            if region is None:
                msg = (
                    f"catalog version {version.policyengine_version} of "
                    f"{version.country} has no region {region_code}"
                )
                raise CatalogError(msg)
            row_filter = _row_filter(region)
            if row_filter is None and region["region_type"] != NATIONAL_REGION_TYPE:
                msg = (
                    f"the region {region_code} has no household filter, so it "
                    "cannot run"
                )
                raise CatalogError(msg)
            dataset = self._dataset(connection, version, region, dataset_name)
            state_filters = (
                self._state_filters(connection, version)
                if region["region_type"] == NATIONAL_REGION_TYPE
                and version.country == "us"
                else None
            )
        return RegionPopulation(
            region_code=region_code,
            region_type=region["region_type"],
            dataset_id=dataset["id"],
            dataset_name=dataset["name"],
            row_filter=row_filter,
            state_code=region["state_code"],
            state_filters=state_filters,
        )

    @staticmethod
    def _dataset(
        connection: Connection,
        version: CatalogVersion,
        region: RowMapping,
        dataset_name: str | None,
    ) -> RowMapping:
        condition = (
            datasets.c.id == region["default_dataset_id"]
            if dataset_name is None
            else datasets.c.name == dataset_name
        )
        dataset = (
            connection.execute(
                select(datasets.c.id, datasets.c.name).where(
                    datasets.c.tax_benefit_model_version_id == version.id, condition
                )
            )
            .mappings()
            .first()
        )
        if dataset is None:
            msg = (
                f"catalog version {version.policyengine_version} of "
                f"{version.country} has no dataset {dataset_name}"
            )
            raise CatalogError(msg)
        return dataset

    @staticmethod
    def _state_filters(
        connection: Connection, version: CatalogVersion
    ) -> dict[str, RegionFilter]:
        rows = connection.execute(
            select(regions)
            .where(
                regions.c.tax_benefit_model_version_id == version.id,
                regions.c.region_type == STATE_REGION_TYPE,
            )
            .order_by(regions.c.code)
        ).mappings()
        filters: dict[str, RegionFilter] = {}
        for row in rows:
            row_filter = _row_filter(row)
            if row_filter is None:
                msg = f"the state region {row['code']} has no household filter"
                raise CatalogError(msg)
            filters[row["code"]] = row_filter
        return filters

    def parameter_changes(
        self,
        catalog_version_id: UUID,
        *,
        policy_id: UUID | None,
        dynamic_id: UUID | None,
    ) -> dict[str, dict[str, Any]]:
        """Return the values a policy and a dynamic set, by name and period.

        The values are stored against parameters of the catalog version the
        policy was saved on; a run on another version applies them by
        parameter name. Raise PolicyParametersMissingError when the run's
        version lacks a parameter they change, and ValueError when the policy
        and the dynamic both set a parameter for the same period.
        """
        owners = [
            condition
            for condition in (
                None
                if policy_id is None
                else parameter_values.c.policy_id == policy_id,
                None
                if dynamic_id is None
                else parameter_values.c.dynamic_id == dynamic_id,
            )
            if condition is not None
        ]
        if not owners:
            return {}
        with self.engine.connect() as connection:
            values = connection.execute(
                select(
                    parameters.c.name,
                    parameter_values.c.value_json,
                    parameter_values.c.start_date,
                    parameter_values.c.end_date,
                    parameter_values.c.dynamic_id,
                )
                .join(parameters, parameters.c.id == parameter_values.c.parameter_id)
                .where(owners[0] if len(owners) == 1 else owners[0] | owners[1])
                .order_by(
                    parameter_values.c.dynamic_id.is_not(None),
                    parameters.c.name,
                    parameter_values.c.start_date,
                )
            ).all()
            names = sorted({row.name for row in values})
            available = set(
                connection.execute(
                    select(parameters.c.name).where(
                        parameters.c.tax_benefit_model_version_id == catalog_version_id,
                        parameters.c.name.in_(names),
                    )
                ).scalars()
            )
        missing = tuple(name for name in names if name not in available)
        if missing:
            raise PolicyParametersMissingError(missing)
        changes: dict[str, dict[str, Any]] = {}
        for row in values:
            periods = changes.setdefault(row.name, {})
            key = _period_key(row.start_date, row.end_date)
            if key in periods:
                msg = f"the policy and the dynamic both set {row.name} for {key}"
                raise ValueError(msg)
            periods[key] = row.value_json
        return changes

    def area_labels(self, catalog_version_id: UUID) -> EconomyResultLabels:
        """Return the names of the version's UK constituencies and local authorities.

        A stored result names each area by its code without its type. The
        catalog has no map positions, so they are left out.
        """
        with self.engine.connect() as connection:
            rows = connection.execute(
                select(regions.c.code, regions.c.label, regions.c.region_type).where(
                    regions.c.tax_benefit_model_version_id == catalog_version_id,
                    regions.c.region_type.in_(UK_AREA_REGION_TYPES),
                )
            ).all()
        labels: dict[str, dict[str, GeographyLabel]] = {
            region_type: {} for region_type in UK_AREA_REGION_TYPES
        }
        for row in rows:
            code = row.code.partition("/")[2]
            labels[row.region_type][code] = GeographyLabel(
                name=row.label, x=None, y=None
            )
        return EconomyResultLabels(
            constituencies=labels["constituency"],
            local_authorities=labels["local_authority"],
        )
