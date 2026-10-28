"""The Public API tables the simulation schema refers to or the household path reads.

In production these tables belong to the Public API's own migration chain, in
the same database as the simulation schema. The tests create only the columns
and constraints the simulation schema's foreign keys and the household reads
use, with the Public API's constraint names, owned by the superuser, and
grant the migrator ``REFERENCES`` on them, as the operator does in
production. Every column the household reads select has the type and
nullability the Public API's migrations give it
(``test_public_api_copies``): ``households.household_data`` and
``parameter_values.value_json`` are ``jsonb``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from migration_support import MIGRATOR_ROLE
from policyengine_simulation_persistence import HOUSEHOLD_READ_TABLES, RUNTIME_ROLE
from sqlalchemy import create_engine, text

if TYPE_CHECKING:
    from sqlalchemy import Engine

PUBLIC_API_TABLE_STATEMENTS = (
    "CREATE TABLE public.tax_benefit_models ("
    " id uuid PRIMARY KEY,"
    " name varchar(32) NOT NULL,"
    " CONSTRAINT uq_tax_benefit_models_name UNIQUE (name))",
    "CREATE TABLE public.tax_benefit_model_versions ("
    " id uuid PRIMARY KEY,"
    " model_id uuid NOT NULL REFERENCES public.tax_benefit_models (id),"
    " version varchar(128) NOT NULL,"
    " CONSTRAINT uq_tax_benefit_model_versions_model_version"
    " UNIQUE (model_id, version))",
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
    " CONSTRAINT uq_regions_model_version_code"
    " UNIQUE (tax_benefit_model_version_id, code))",
    "CREATE TABLE public.policies ("
    " id uuid PRIMARY KEY,"
    " country_id varchar(2) NOT NULL,"
    " CONSTRAINT uq_policies_id_country UNIQUE (id, country_id))",
    "CREATE TABLE public.households ("
    " id uuid PRIMARY KEY,"
    " country_id varchar(2) NOT NULL,"
    " default_year integer,"
    " household_data jsonb NOT NULL,"
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
    " value_json jsonb NOT NULL,"
    " start_date timestamptz NOT NULL,"
    " end_date timestamptz,"
    " policy_id uuid REFERENCES public.policies (id),"
    " dynamic_id uuid REFERENCES public.dynamics (id),"
    " CONSTRAINT ck_parameter_values_effective_period"
    " CHECK (end_date IS NULL OR end_date >= start_date),"
    " CONSTRAINT ck_parameter_values_single_owner"
    " CHECK (policy_id IS NULL OR dynamic_id IS NULL),"
    " CONSTRAINT uq_parameter_values_policy_parameter_start_date"
    " UNIQUE (policy_id, parameter_id, start_date))",
)
# The catalog versions' PolicyEngine.py versions, oldest first.
CATALOG_POLICYENGINE_VERSION = "6.9.0"
NEWER_CATALOG_POLICYENGINE_VERSION = "6.10.0"
# A stored household document; these rows are only referred to, never read.
EMPTY_HOUSEHOLD_DOCUMENT = '{"people": []}'
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

    The runtime role may read the tables a household run is calculated from
    (``HOUSEHOLD_READ_TABLES``), as the operator grants in production.
    """
    engine = superuser_engine(superuser_url, isolation_level="AUTOCOMMIT")
    grant = (
        "GRANT REFERENCES ON "
        + ", ".join(f"public.{table}" for table in REFERENCED_TABLES)
        + f" TO {MIGRATOR_ROLE}"
    )
    read_grant = (
        "GRANT SELECT ON "
        + ", ".join(f"public.{table}" for table in HOUSEHOLD_READ_TABLES)
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

    Two US catalog versions (PolicyEngine.py ``CATALOG_POLICYENGINE_VERSION``
    and ``NEWER_CATALOG_POLICYENGINE_VERSION``) each have California
    (``state/ca``) with its own default dataset, as each publication creates
    new rows.
    """

    us_model_id: UUID
    uk_model_id: UUID
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
            "INSERT INTO public.tax_benefit_models (id, name) VALUES "
            "(:us_model, 'policyengine-us'), (:uk_model, 'policyengine-uk')",
            {"us_model": rows.us_model_id, "uk_model": rows.uk_model_id},
        ),
        (
            "INSERT INTO public.tax_benefit_model_versions (id, model_id, version) "
            "VALUES (:version, :us_model, :policyengine_version), "
            "(:newer_version, :us_model, :newer_policyengine_version)",
            {
                "version": rows.catalog_version_id,
                "newer_version": rows.newer_catalog_version_id,
                "us_model": rows.us_model_id,
                "policyengine_version": CATALOG_POLICYENGINE_VERSION,
                "newer_policyengine_version": NEWER_CATALOG_POLICYENGINE_VERSION,
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
            "INSERT INTO public.regions "
            "(id, code, tax_benefit_model_version_id, default_dataset_id) VALUES "
            "(gen_random_uuid(), 'state/ca', :version, :dataset), "
            "(gen_random_uuid(), 'state/ca', :newer_version, :newer_dataset)",
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
        us_model_id=uuid4(),
        uk_model_id=uuid4(),
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
            "INSERT INTO public.households "
            "(id, country_id, default_year, household_data) VALUES "
            "(:us_household, 'us', 2026, :document), "
            "(:uk_household, 'uk', 2026, :document)",
            {
                "us_household": rows.us_household_id,
                "uk_household": rows.uk_household_id,
                "document": EMPTY_HOUSEHOLD_DOCUMENT,
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
