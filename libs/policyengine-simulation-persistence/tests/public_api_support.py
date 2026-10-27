"""The Public API tables the simulation schema refers to, reduced to their keys.

In production these tables belong to the Public API's own migration chain, in
the same database as the simulation schema. The tests create only the columns
and unique constraints the simulation schema's foreign keys use, with the
Public API's constraint names, owned by the superuser, and grant the migrator
``REFERENCES`` on them, as the operator does in production. The runtime role
may only read the catalog and policy tables (``CATALOG_READ_TABLES``).

Every column the catalog reads use has the type and nullability the Public
API's migrations give it: ``regions.region_type`` is the PostgreSQL enum
``v2_region_type``, which has no comparison with text values, and
``parameter_values.value_json`` is ``json``, not ``jsonb``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from migration_support import MIGRATOR_ROLE
from policyengine_simulation_persistence.catalog_reads import CATALOG_READ_TABLES
from policyengine_simulation_persistence.schema import RUNTIME_ROLE
from sqlalchemy import create_engine, text

if TYPE_CHECKING:
    from sqlalchemy import Engine

# The values and order of the Public API's baseline migration.
REGION_TYPES = (
    "national",
    "country",
    "state",
    "congressional_district",
    "constituency",
    "local_authority",
    "city",
    "place",
)
PUBLIC_API_TABLE_STATEMENTS = (
    "CREATE TYPE public.v2_region_type AS ENUM ("
    + ", ".join(f"'{region_type}'" for region_type in REGION_TYPES)
    + ")",
    "CREATE TABLE public.tax_benefit_models ("
    " id uuid PRIMARY KEY,"
    " name varchar(32) NOT NULL UNIQUE)",
    "CREATE TABLE public.tax_benefit_model_versions ("
    " id uuid PRIMARY KEY,"
    " model_id uuid REFERENCES public.tax_benefit_models (id),"
    " version varchar(128))",
    "CREATE TABLE public.datasets ("
    " id uuid PRIMARY KEY,"
    " name varchar(255) NOT NULL,"
    " tax_benefit_model_version_id uuid NOT NULL"
    " REFERENCES public.tax_benefit_model_versions (id),"
    " CONSTRAINT uq_datasets_model_version_name"
    " UNIQUE (tax_benefit_model_version_id, name),"
    " CONSTRAINT uq_datasets_id_model_version"
    " UNIQUE (id, tax_benefit_model_version_id))",
    "CREATE TABLE public.regions ("
    " id uuid PRIMARY KEY,"
    " code varchar(255) NOT NULL,"
    " tax_benefit_model_version_id uuid NOT NULL"
    " REFERENCES public.tax_benefit_model_versions (id),"
    " default_dataset_id uuid NOT NULL,"
    " label varchar(255) NOT NULL,"
    " region_type public.v2_region_type NOT NULL,"
    " requires_filter boolean NOT NULL DEFAULT false,"
    " filter_field varchar(128),"
    " filter_value varchar(255),"
    " filter_strategy varchar(64),"
    " state_code varchar(16),"
    " CONSTRAINT uq_regions_model_version_code"
    " UNIQUE (tax_benefit_model_version_id, code))",
    "CREATE TABLE public.policies ("
    " id uuid PRIMARY KEY,"
    " country_id varchar(2) NOT NULL,"
    " tax_benefit_model_version_id uuid"
    " REFERENCES public.tax_benefit_model_versions (id),"
    " CONSTRAINT uq_policies_id_country UNIQUE (id, country_id))",
    "CREATE TABLE public.households ("
    " id uuid PRIMARY KEY,"
    " country_id varchar(2) NOT NULL,"
    " CONSTRAINT uq_households_id_country UNIQUE (id, country_id))",
    "CREATE TABLE public.dynamics (id uuid PRIMARY KEY)",
    "CREATE TABLE public.parameters ("
    " id uuid PRIMARY KEY,"
    " name varchar(512) NOT NULL,"
    " tax_benefit_model_version_id uuid NOT NULL"
    " REFERENCES public.tax_benefit_model_versions (id),"
    " CONSTRAINT uq_parameters_model_version_name"
    " UNIQUE (tax_benefit_model_version_id, name))",
    "CREATE TABLE public.parameter_values ("
    " id uuid PRIMARY KEY,"
    " parameter_id uuid NOT NULL REFERENCES public.parameters (id),"
    " value_json json NOT NULL,"
    " start_date timestamptz NOT NULL,"
    " end_date timestamptz,"
    " policy_id uuid REFERENCES public.policies (id),"
    " dynamic_id uuid REFERENCES public.dynamics (id))",
)
REFERENCED_TABLES = (
    "tax_benefit_model_versions",
    "datasets",
    "regions",
    "policies",
    "households",
    "dynamics",
)


def superuser_engine(superuser_url: str, **options: object) -> Engine:
    """Return an engine that connects to the test database as the superuser."""
    return create_engine(
        superuser_url.replace("postgresql://", "postgresql+psycopg://", 1),
        **options,
    )


def create_public_api_tables(superuser_url: str, *, grant_references: bool) -> None:
    """Create the referenced Public API tables in a new test database.

    The runtime role may read the catalog and policy tables a v2 economy run
    resolves its region, dataset and policy from, as the operator grants in
    production (``CATALOG_READ_TABLES``).
    """
    engine = superuser_engine(superuser_url, isolation_level="AUTOCOMMIT")
    grant = (
        "GRANT REFERENCES ON "
        + ", ".join(f"public.{table}" for table in REFERENCED_TABLES)
        + f" TO {MIGRATOR_ROLE}"
    )
    read_grant = (
        "GRANT SELECT ON "
        + ", ".join(f"public.{table}" for table in CATALOG_READ_TABLES)
        + f" TO {RUNTIME_ROLE}"
    )
    statements = (
        (*PUBLIC_API_TABLE_STATEMENTS, grant, read_grant)
        if grant_references
        else (*PUBLIC_API_TABLE_STATEMENTS, read_grant)
    )
    try:
        with engine.connect() as connection:
            for statement in statements:
                connection.execute(text(statement))
    finally:
        engine.dispose()


@dataclass(frozen=True)
class PublicApiRows:
    """Public API rows a simulation or run can refer to.

    Two catalog versions each have California (``state/ca``) with its own
    default dataset, as each publication creates new rows.
    """

    us_policy_id: UUID
    uk_policy_id: UUID
    us_household_id: UUID
    uk_household_id: UUID
    dynamic_id: UUID
    catalog_version_id: UUID
    newer_catalog_version_id: UUID
    dataset_id: UUID
    newer_dataset_id: UUID


def _catalog_statements(rows: PublicApiRows) -> tuple[tuple[str, dict], ...]:
    return (
        (
            "INSERT INTO public.tax_benefit_model_versions (id) VALUES "
            "(:version), (:newer_version)",
            {
                "version": rows.catalog_version_id,
                "newer_version": rows.newer_catalog_version_id,
            },
        ),
        (
            "INSERT INTO public.datasets (id, name, tax_benefit_model_version_id) "
            "VALUES (:dataset, 'populace_us_2024', :version), "
            "(:newer_dataset, 'populace_us_2024_acs_local', :newer_version)",
            {
                "dataset": rows.dataset_id,
                "newer_dataset": rows.newer_dataset_id,
                "version": rows.catalog_version_id,
                "newer_version": rows.newer_catalog_version_id,
            },
        ),
        (
            "INSERT INTO public.regions (id, code, tax_benefit_model_version_id, "
            "default_dataset_id, label, region_type) VALUES "
            "(gen_random_uuid(), 'state/ca', :version, :dataset, 'California', "
            "'state'), "
            "(gen_random_uuid(), 'state/ca', :newer_version, :newer_dataset, "
            "'California', 'state')",
            {
                "version": rows.catalog_version_id,
                "newer_version": rows.newer_catalog_version_id,
                "dataset": rows.dataset_id,
                "newer_dataset": rows.newer_dataset_id,
            },
        ),
    )


def insert_public_api_rows(superuser_url: str) -> PublicApiRows:
    """Insert rows into the referenced Public API tables, as their owner."""
    rows = PublicApiRows(
        us_policy_id=uuid4(),
        uk_policy_id=uuid4(),
        us_household_id=uuid4(),
        uk_household_id=uuid4(),
        dynamic_id=uuid4(),
        catalog_version_id=uuid4(),
        newer_catalog_version_id=uuid4(),
        dataset_id=uuid4(),
        newer_dataset_id=uuid4(),
    )
    statements = (
        *_catalog_statements(rows),
        (
            "INSERT INTO public.policies (id, country_id) VALUES "
            "(:us_policy, 'us'), (:uk_policy, 'uk')",
            {"us_policy": rows.us_policy_id, "uk_policy": rows.uk_policy_id},
        ),
        (
            "INSERT INTO public.households (id, country_id) VALUES "
            "(:us_household, 'us'), (:uk_household, 'uk')",
            {
                "us_household": rows.us_household_id,
                "uk_household": rows.uk_household_id,
            },
        ),
        (
            "INSERT INTO public.dynamics (id) VALUES (:dynamic)",
            {"dynamic": rows.dynamic_id},
        ),
    )
    engine = superuser_engine(superuser_url)
    try:
        with engine.begin() as connection:
            for statement, parameters in statements:
                connection.execute(text(statement), parameters)
    finally:
        engine.dispose()
    return rows


def delete_public_api_row(superuser_url: str, table_name: str, row_id: UUID) -> None:
    """Delete one Public API row as its owner, as the Public API would."""
    assert table_name in REFERENCED_TABLES
    engine = superuser_engine(superuser_url)
    try:
        with engine.begin() as connection:
            connection.execute(
                text(f"DELETE FROM public.{table_name} WHERE id = :row_id"),  # noqa: S608  # table names come from REFERENCED_TABLES
                {"row_id": row_id},
            )
    finally:
        engine.dispose()
