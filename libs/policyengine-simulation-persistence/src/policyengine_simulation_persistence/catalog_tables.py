"""Mapped classes of the PostgreSQL catalog tables the privilege check reads.

Any role may read the catalog, so a table is found by name without the USAGE
privilege on its schema that ``to_regclass`` would need. PostgreSQL owns these
tables; each class names only the columns the check uses and the table's
primary key, and is only ever read.
"""

from __future__ import annotations

from sqlalchemy import Boolean, SmallInteger, String
from sqlalchemy.dialects.postgresql import OID
from sqlalchemy.orm import Mapped, mapped_column

from policyengine_simulation_persistence.orm_base import CatalogBase


class PgNamespace(CatalogBase):
    """A schema."""

    __tablename__ = "pg_namespace"

    oid: Mapped[int] = mapped_column(OID, primary_key=True)
    nspname: Mapped[str] = mapped_column(String)


class PgClass(CatalogBase):
    """A table or a table-like object, such as a view or an index."""

    __tablename__ = "pg_class"

    oid: Mapped[int] = mapped_column(OID, primary_key=True)
    relname: Mapped[str] = mapped_column(String)
    relnamespace: Mapped[int] = mapped_column(OID)
    relkind: Mapped[str] = mapped_column(String)


class PgAttribute(CatalogBase):
    """A column of a table, numbered from 1; system columns are below zero."""

    __tablename__ = "pg_attribute"

    attrelid: Mapped[int] = mapped_column(OID, primary_key=True)
    attnum: Mapped[int] = mapped_column(SmallInteger, primary_key=True)
    attname: Mapped[str] = mapped_column(String)
    attisdropped: Mapped[bool] = mapped_column(Boolean)
