"""API v1's own tables that the simulation service's rows name, by ``id`` alone.

Some keys of the service's tables name rows API v1 writes: users, policies,
dynamics, households, datasets, regions and the model catalog. PostgreSQL checks
each of those keys when a row is inserted, as the referenced table's owner, so
the service's role needs no privilege for it, and a row naming one that doesn't
exist fails with SQLSTATE 23503. These classes give SQLAlchemy each key's
target. They aren't models of those tables: nothing reads or writes them here.
"""

from __future__ import annotations

from typing import ClassVar
from uuid import UUID  # noqa: TC003  # SQLAlchemy reads Mapped annotations at run time

from sqlalchemy import PrimaryKeyConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from policyengine_simulation_persistence.orm_base import ReferencedBase


class ReferencedDataset(ReferencedBase):
    """The ``id`` of a row of ``datasets``."""

    __tablename__ = "datasets"
    __table_args__: ClassVar = (PrimaryKeyConstraint("id", name="pk_datasets"),)

    id: Mapped[UUID] = mapped_column(Uuid)


class ReferencedDynamic(ReferencedBase):
    """The ``id`` of a row of ``dynamics``."""

    __tablename__ = "dynamics"
    __table_args__: ClassVar = (PrimaryKeyConstraint("id", name="pk_dynamics"),)

    id: Mapped[UUID] = mapped_column(Uuid)


class ReferencedHousehold(ReferencedBase):
    """The ``id`` of a row of ``households``."""

    __tablename__ = "households"
    __table_args__: ClassVar = (PrimaryKeyConstraint("id", name="pk_households"),)

    id: Mapped[UUID] = mapped_column(Uuid)


class ReferencedPolicy(ReferencedBase):
    """The ``id`` of a row of ``policies``."""

    __tablename__ = "policies"
    __table_args__: ClassVar = (PrimaryKeyConstraint("id", name="pk_policies"),)

    id: Mapped[UUID] = mapped_column(Uuid)


class ReferencedRegion(ReferencedBase):
    """The ``id`` of a row of ``regions``."""

    __tablename__ = "regions"
    __table_args__: ClassVar = (PrimaryKeyConstraint("id", name="pk_regions"),)

    id: Mapped[UUID] = mapped_column(Uuid)


class ReferencedTaxBenefitModelVersion(ReferencedBase):
    """The ``id`` of a row of ``tax_benefit_model_versions``."""

    __tablename__ = "tax_benefit_model_versions"
    __table_args__: ClassVar = (
        PrimaryKeyConstraint("id", name="pk_tax_benefit_model_versions"),
    )

    id: Mapped[UUID] = mapped_column(Uuid)


class ReferencedTaxBenefitModel(ReferencedBase):
    """The ``id`` of a row of ``tax_benefit_models``."""

    __tablename__ = "tax_benefit_models"
    __table_args__: ClassVar = (
        PrimaryKeyConstraint("id", name="pk_tax_benefit_models"),
    )

    id: Mapped[UUID] = mapped_column(Uuid)


class ReferencedUser(ReferencedBase):
    """The ``id`` of a row of ``users``."""

    __tablename__ = "users"
    __table_args__: ClassVar = (PrimaryKeyConstraint("id", name="pk_users"),)

    id: Mapped[UUID] = mapped_column(Uuid)


referenced_metadata = ReferencedBase.metadata
