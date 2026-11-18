"""The declarative bases of the library's mapped classes, and their shared columns.

The library models API v1's tables in four groups, each with its own
declarative base and ``MetaData``. One base can't hold them all: the read-only
models in ``InputBase`` and the key targets in ``ReferencedBase`` describe some
of the same tables, and a ``MetaData`` holds one table of each name. Keys
between the groups name the other group's mapped attribute, such as
``ReportRun.id``.

Every ``MetaData`` names the ``public`` schema, so statements reach API v1's
tables whatever the role's ``search_path``. A ``MetaData`` holds a group's
tables once the module declaring them is imported, so each of those modules
exports its own: ``service_metadata`` from ``tables``, ``referenced_metadata``
from ``referenced_tables``, ``input_metadata`` from ``input_tables`` and
``result_metadata`` from ``result_tables``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar, Final
from uuid import uuid4

from sqlalchemy import DateTime, Enum, MetaData, Uuid, text
from sqlalchemy.orm import DeclarativeBase, mapped_column

if TYPE_CHECKING:
    from datetime import datetime
    from enum import StrEnum
    from uuid import UUID

    from sqlalchemy import Table
    from sqlalchemy.orm import MappedColumn

TABLE_SCHEMA: Final = "public"


class _TableModel:
    """Types ``__table__`` as what every class here maps: one ``Table``."""

    __table__: ClassVar[Table]


class ServiceBase(_TableModel, DeclarativeBase):
    """Base of the tables the simulation service creates rows in."""

    metadata = MetaData(schema=TABLE_SCHEMA)


class ReferencedBase(_TableModel, DeclarativeBase):
    """Base of API v1's tables that the service's keys name, by ``id`` alone."""

    metadata = MetaData(schema=TABLE_SCHEMA)


class InputBase(_TableModel, DeclarativeBase):
    """Base of the read-only models of the tables a calculation is read from."""

    metadata = MetaData(schema=TABLE_SCHEMA)


class ResultBase(_TableModel, DeclarativeBase):
    """Base of the result tables the Modal worker role inserts into.

    The role holds only ``INSERT`` on them, so an insert doesn't read the row's
    server defaults back, which would need ``SELECT``.
    """

    __mapper_args__: ClassVar = {"eager_defaults": False}

    metadata = MetaData(schema=TABLE_SCHEMA)


def enum_type(values: type[StrEnum], name: str) -> Enum:
    """Store each member's value, under the Postgres type API v1's chain names.

    The type names its schema itself: SQLAlchemy 2.0 doesn't give an enum the
    metadata's schema, so it would otherwise follow the role's search_path.
    """
    return Enum(
        values,
        name=name,
        schema=TABLE_SCHEMA,
        values_callable=lambda members: [member.value for member in members],
    )


def identifier_column() -> MappedColumn[UUID]:
    """Return a row's ``id``, generated here unless the row is given one."""
    return mapped_column(Uuid, nullable=False, default=uuid4)


def server_timestamp_column() -> MappedColumn[datetime]:
    """Return a timezone-aware timestamp the database defaults to ``now()``."""
    return mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
    )


def optional_timestamp_column() -> MappedColumn[datetime | None]:
    """Return a timezone-aware timestamp that is empty until it is set."""
    return mapped_column(DateTime(timezone=True), nullable=True)
