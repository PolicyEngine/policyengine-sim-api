"""Read-only mapped classes of API v1's tables a calculation is read from.

API v1 writes households, policies, dynamics and their parameter values, and
its model catalog; the simulation service only reads them, with ``SELECT``.
Each class names only the columns the service reads and the table's primary
key, so these are not models of the whole tables: API v1's other columns,
defaults, constraints and indexes are its own. The classes derive from
``InputBase``, apart from the models of the tables the service writes
(``ServiceBase``) and from the tables those name by ``id`` alone
(``ReferencedBase``). ``tests/test_public_api_schema.py`` compares each column
with API v1's table.
"""

from __future__ import annotations

from datetime import datetime  # noqa: TC003  # SQLAlchemy reads Mapped annotations
from typing import Any, Final
from uuid import UUID  # noqa: TC003  # SQLAlchemy reads Mapped annotations at run time

from sqlalchemy import DateTime, Enum, Integer, String, Uuid
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from policyengine_simulation_persistence.orm_base import TABLE_SCHEMA, InputBase

# A country's tax-benefit model is named "policyengine-<country>".
MODEL_NAME_PREFIX: Final = "policyengine-"
# The kinds of geography a region covers, as API v1's ``v2_region_type`` lists
# them.
REGION_TYPES: Final = (
    "national",
    "country",
    "state",
    "congressional_district",
    "constituency",
    "local_authority",
    "city",
    "place",
)
# The UK region types a report on the local-area dataset calculates.
UK_AREA_REGION_TYPES: Final = ("constituency", "local_authority")


class TaxBenefitModel(InputBase):
    """A country's tax-benefit model."""

    __tablename__ = "tax_benefit_models"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    name: Mapped[str] = mapped_column(String(32))


class TaxBenefitModelVersion(InputBase):
    """One published catalog version of a model.

    ``version`` is the .py (policyengine package) version it was published from.
    """

    __tablename__ = "tax_benefit_model_versions"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    model_id: Mapped[UUID] = mapped_column(Uuid)
    version: Mapped[str] = mapped_column(String(128))


class Region(InputBase):
    """A region of one catalog version, unique by code within it.

    ``default_dataset_id`` is the dataset it uses unless a calculation names
    another.
    """

    __tablename__ = "regions"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    code: Mapped[str] = mapped_column(String(255))
    # The enum names its schema itself, as the service tables' enums do.
    region_type: Mapped[str] = mapped_column(
        Enum(*REGION_TYPES, name="v2_region_type", schema=TABLE_SCHEMA)
    )
    tax_benefit_model_version_id: Mapped[UUID] = mapped_column(Uuid)
    default_dataset_id: Mapped[UUID] = mapped_column(Uuid)


class Dataset(InputBase):
    """A dataset of one catalog version, unique by name within it."""

    __tablename__ = "datasets"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    tax_benefit_model_version_id: Mapped[UUID] = mapped_column(Uuid)


class Household(InputBase):
    """A stored household.

    ``household_data`` lists the people and each group entity as records, and
    may hold a saved SPM selection; ``default_year`` is the year it describes.
    """

    __tablename__ = "households"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    country_id: Mapped[str] = mapped_column(String(2))
    default_year: Mapped[int | None] = mapped_column(Integer)
    household_data: Mapped[dict[str, Any]] = mapped_column(JSONB)


class Policy(InputBase):
    """A stored policy; its values are ``ParameterValue`` rows."""

    __tablename__ = "policies"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    country_id: Mapped[str] = mapped_column(String(2))


class Parameter(InputBase):
    """A catalog version's parameter, named once within it."""

    __tablename__ = "parameters"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    name: Mapped[str] = mapped_column(String(512))
    tax_benefit_model_version_id: Mapped[UUID] = mapped_column(Uuid)


class ParameterValue(InputBase):
    """A parameter's value from ``start_date`` until ``end_date``.

    With no end date it applies indefinitely. A policy's own values name it in
    ``policy_id``, and a dynamic's in ``dynamic_id``.
    """

    __tablename__ = "parameter_values"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    parameter_id: Mapped[UUID] = mapped_column(Uuid)
    value_json: Mapped[Any] = mapped_column(JSONB, nullable=False)
    start_date: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    end_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    policy_id: Mapped[UUID | None] = mapped_column(Uuid)
    dynamic_id: Mapped[UUID | None] = mapped_column(Uuid)


class Dynamic(InputBase):
    """Named parameter values a simulation applies beside its policy.

    Behavioural responses are an example. A dynamic belongs to no country.
    """

    __tablename__ = "dynamics"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)


input_metadata = InputBase.metadata
