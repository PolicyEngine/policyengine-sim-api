"""The declarative base of the comparison path's mapped classes."""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from sqlalchemy import MetaData
from sqlalchemy.orm import DeclarativeBase

if TYPE_CHECKING:
    from sqlalchemy import Table


class _TableModel:
    """Types ``__table__`` as what every class here maps: one ``Table``."""

    __table__: ClassVar[Table]


class ComparisonBase(_TableModel, DeclarativeBase):
    """Base of the comparison tables, in the ``public`` schema."""

    metadata = MetaData(
        schema="public",
        naming_convention={
            "ix": "ix_%(table_name)s_%(column_0_N_name)s",
            "uq": "uq_%(table_name)s_%(column_0_N_name)s",
            "ck": "%(constraint_name)s",
            "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
            "pk": "pk_%(table_name)s",
        },
    )
