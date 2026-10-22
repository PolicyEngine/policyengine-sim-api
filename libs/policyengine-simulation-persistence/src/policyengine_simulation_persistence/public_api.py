"""The Public API tables that rows in the simulation schema refer to.

The Public API owns these tables and creates and changes them with its own
migration chain, in the same database as the ``simulation`` schema. They are
described here only so the simulation tables can declare foreign keys to them:
each lists just the key columns those foreign keys use, in a metadata object of
its own, so the simulation chain never compares, creates or changes them.

The tables have no schema here because PostgreSQL reports a foreign key into
``public`` without a schema when ``public`` is on the search path; naming one
would make every schema comparison report the foreign keys as changed. The
migrations name ``public`` explicitly.

Every foreign key into these tables is ``ON DELETE RESTRICT``: the Public API
cannot delete a row a simulation uses. Creating the foreign keys needs
``REFERENCES`` on each table for the migrator role; the runtime role needs no
privilege on them, because PostgreSQL runs a foreign key's check as the owner
of the referenced table.
"""

from __future__ import annotations

from sqlalchemy import Column, MetaData, String, Table, UniqueConstraint, Uuid

public_api_metadata = MetaData()

policies = Table(
    "policies",
    public_api_metadata,
    Column("id", Uuid(as_uuid=True), primary_key=True),
    Column("country_id", String(2), nullable=False),
)
households = Table(
    "households",
    public_api_metadata,
    Column("id", Uuid(as_uuid=True), primary_key=True),
    Column("country_id", String(2), nullable=False),
)
dynamics = Table(
    "dynamics",
    public_api_metadata,
    Column("id", Uuid(as_uuid=True), primary_key=True),
)
# A catalog version: one published set of model metadata for one country and
# one policyengine.py version. Each publication creates new region and dataset
# rows, unique by code or name within their version.
tax_benefit_model_versions = Table(
    "tax_benefit_model_versions",
    public_api_metadata,
    Column("id", Uuid(as_uuid=True), primary_key=True),
)
regions = Table(
    "regions",
    public_api_metadata,
    Column("id", Uuid(as_uuid=True), primary_key=True),
    Column("tax_benefit_model_version_id", Uuid(as_uuid=True), nullable=False),
    Column("code", String(255), nullable=False),
    UniqueConstraint(
        "tax_benefit_model_version_id", "code", name="uq_regions_model_version_code"
    ),
)
datasets = Table(
    "datasets",
    public_api_metadata,
    Column("id", Uuid(as_uuid=True), primary_key=True),
    Column("tax_benefit_model_version_id", Uuid(as_uuid=True), nullable=False),
    UniqueConstraint(
        "id", "tax_benefit_model_version_id", name="uq_datasets_id_model_version"
    ),
)
