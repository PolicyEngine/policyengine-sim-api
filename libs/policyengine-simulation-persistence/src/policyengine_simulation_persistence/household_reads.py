"""Read the Public API's rows a v2 household run is calculated from.

A household run calculates one stored household under one policy, or current
law, on one catalog version. The Simulation Entrypoint reads, from the Public
API's tables in the same database:

- the catalog version, by country and PolicyEngine.py version;
- the household, by country and ID: its document (people and entity records
  in v2 form, with its saved SPM choice) and its ``default_year``;
- the parameter values the policy sets, by parameter name, checked against
  the run's catalog version (``policy_parameters``).

The runtime role needs ``SELECT`` on ``HOUSEHOLD_READ_TABLES``, which the
owner of the Public API's tables grants. These tables are described here with
only the columns read, in a metadata object of their own; nothing here writes
them. The tests' copies of them are checked against the Public API's columns.

This is v2 code: the v1 ``run_simulation`` code does not use it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    Column,
    DateTime,
    Integer,
    MetaData,
    String,
    Table,
    Uuid,
    select,
)
from sqlalchemy.dialects.postgresql import JSONB

if TYPE_CHECKING:
    from datetime import datetime
    from uuid import UUID

    from sqlalchemy import Engine

household_read_metadata = MetaData()

tax_benefit_models = Table(
    "tax_benefit_models",
    household_read_metadata,
    Column("id", Uuid(as_uuid=True), primary_key=True),
    Column("name", String(32)),
)
tax_benefit_model_versions = Table(
    "tax_benefit_model_versions",
    household_read_metadata,
    Column("id", Uuid(as_uuid=True), primary_key=True),
    Column("model_id", Uuid(as_uuid=True)),
    Column("version", String(128)),
)
households = Table(
    "households",
    household_read_metadata,
    Column("id", Uuid(as_uuid=True), primary_key=True),
    Column("country_id", String(2)),
    Column("default_year", Integer),
    Column("household_data", JSONB),
)
policies = Table(
    "policies",
    household_read_metadata,
    Column("id", Uuid(as_uuid=True), primary_key=True),
    Column("country_id", String(2)),
)
parameters = Table(
    "parameters",
    household_read_metadata,
    Column("id", Uuid(as_uuid=True), primary_key=True),
    Column("name", String(512)),
    Column("tax_benefit_model_version_id", Uuid(as_uuid=True)),
)
parameter_values = Table(
    "parameter_values",
    household_read_metadata,
    Column("parameter_id", Uuid(as_uuid=True)),
    Column("value_json", JSONB),
    Column("start_date", DateTime(timezone=True)),
    Column("end_date", DateTime(timezone=True)),
    Column("policy_id", Uuid(as_uuid=True)),
)
# The Public API tables the runtime role reads; the operator grants SELECT.
HOUSEHOLD_READ_TABLES = tuple(household_read_metadata.tables)
# The household functions take the Public API's policy JSON, whose periods end
# on this date when a value has no end.
OPEN_END_DATE = "2100-12-31"
# A refused policy's error names at most this many parameters.
MAX_LISTED_PARAMETERS = 10


class UnknownHouseholdError(LookupError):
    """The Public API has no such household in the country."""


class UnknownPolicyError(LookupError):
    """The Public API has no such policy in the country."""


class UnknownCatalogVersionError(LookupError):
    """The country has no catalog version for the PolicyEngine.py version."""


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
class HouseholdCatalogVersion:
    """One published PolicyEngine.py version of one country's catalog."""

    id: UUID
    country: str
    policyengine_version: str


@dataclass(frozen=True)
class StoredHousehold:
    """A household as the Public API stores it.

    ``document`` lists the people and each group entity as records with an
    ``id`` and ``values``, each person naming its groups, and may hold the
    saved SPM choice (``spm``). ``default_year`` is the year its values name,
    or None when it has none.
    """

    id: UUID
    country: str
    default_year: int | None
    document: dict[str, Any]


def _period_key(start: datetime, end: datetime | None) -> str:
    """Return the Public API's policy period: ``YYYY-MM-DD.YYYY-MM-DD``.

    The Public API stores each date as midnight UTC; the dates are read in
    UTC whatever the database session's time zone. A value without an end
    runs to ``OPEN_END_DATE``.
    """
    start_date = f"{start.astimezone(UTC):%Y-%m-%d}"
    end_date = OPEN_END_DATE if end is None else f"{end.astimezone(UTC):%Y-%m-%d}"
    return f"{start_date}.{end_date}"


class HouseholdInputReader:
    """Typed SQLAlchemy Core reads of the Public API's household-run rows."""

    def __init__(self, engine: Engine) -> None:
        """Use ``engine``, which connects as the runtime role."""
        self.engine = engine

    def catalog_version(
        self, country: str, policyengine_version: str
    ) -> HouseholdCatalogVersion:
        """Return the country's catalog version for a PolicyEngine.py version.

        Raise UnknownCatalogVersionError when the country has none.
        """
        with self.engine.connect() as connection:
            version_id = connection.execute(
                select(tax_benefit_model_versions.c.id)
                .join(
                    tax_benefit_models,
                    tax_benefit_models.c.id == tax_benefit_model_versions.c.model_id,
                )
                .where(
                    tax_benefit_models.c.name == f"policyengine-{country}",
                    tax_benefit_model_versions.c.version == policyengine_version,
                )
            ).scalar_one_or_none()
        if version_id is None:
            msg = (
                f"{country} has no catalog version for PolicyEngine.py "
                f"{policyengine_version}"
            )
            raise UnknownCatalogVersionError(msg)
        return HouseholdCatalogVersion(
            id=version_id, country=country, policyengine_version=policyengine_version
        )

    def household(self, country: str, household_id: UUID) -> StoredHousehold:
        """Return the country's household; raise UnknownHouseholdError otherwise."""
        with self.engine.connect() as connection:
            row = connection.execute(
                select(households.c.default_year, households.c.household_data).where(
                    households.c.id == household_id,
                    households.c.country_id == country,
                )
            ).first()
        if row is None:
            msg = f"{country} has no household {household_id}"
            raise UnknownHouseholdError(msg)
        return StoredHousehold(
            id=household_id,
            country=country,
            default_year=row.default_year,
            document=row.household_data,
        )

    def policy_parameters(
        self, country: str, policy_id: UUID, catalog_version_id: UUID
    ) -> dict[str, dict[str, Any]]:
        """Return the values the country's policy sets, by name and period.

        The values are stored against parameters of the catalog version the
        policy was saved on; a run on another version applies them by
        parameter name. Raise UnknownPolicyError when the country has no such
        policy, and PolicyParametersMissingError when the run's version lacks
        a parameter it changes.
        """
        with self.engine.connect() as connection:
            policy = connection.execute(
                select(policies.c.id).where(
                    policies.c.id == policy_id, policies.c.country_id == country
                )
            ).scalar_one_or_none()
            if policy is None:
                msg = f"{country} has no policy {policy_id}"
                raise UnknownPolicyError(msg)
            values = connection.execute(
                select(
                    parameters.c.name,
                    parameter_values.c.value_json,
                    parameter_values.c.start_date,
                    parameter_values.c.end_date,
                )
                .join(parameters, parameters.c.id == parameter_values.c.parameter_id)
                .where(parameter_values.c.policy_id == policy_id)
                .order_by(parameters.c.name, parameter_values.c.start_date)
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
            changes.setdefault(row.name, {})[
                _period_key(row.start_date, row.end_date)
            ] = row.value_json
        return changes
