"""Insert the Public API's own rows a calculation is read from, for tests.

The Public API writes households, policies, their parameter values and its
model catalog; the tests insert them as the superuser, in a transaction they
roll back, before acting as the simulation service's role.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.sql import bindparam

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping
    from datetime import datetime

    from sqlalchemy import Connection


@dataclass(frozen=True)
class CatalogVersion:
    """A country's tax-benefit model and one published version of its catalog."""

    country: str
    model_id: UUID
    version_id: UUID
    policyengine_version: str


@dataclass(frozen=True)
class StoredValue:
    """One parameter value a policy sets, as the Public API stores it."""

    parameter_name: str
    value: object
    start_date: datetime
    end_date: datetime | None = None


def _content_hash(identifier: UUID) -> str:
    return sha256(identifier.bytes).hexdigest()


def insert_model(connection: Connection, country: str) -> UUID:
    """Insert the country's tax-benefit model, named as the Public API names it."""
    model_id = uuid4()
    connection.execute(
        text("INSERT INTO public.tax_benefit_models (id, name) VALUES (:id, :name)"),
        {"id": model_id, "name": f"policyengine-{country}"},
    )
    return model_id


def insert_catalog_version(
    connection: Connection,
    *,
    country: str,
    model_id: UUID,
    policyengine_version: str,
    parameter_names: Iterable[str] = (),
) -> CatalogVersion:
    """Insert a catalog version of the model and its named parameters."""
    version_id = uuid4()
    connection.execute(
        text(
            "INSERT INTO public.tax_benefit_model_versions "
            "(id, model_id, version, current_law_id, metadata_time_periods) "
            "VALUES (:id, :model_id, :version, 1, '[]')"
        ),
        {"id": version_id, "model_id": model_id, "version": policyengine_version},
    )
    for name in parameter_names:
        connection.execute(
            text(
                "INSERT INTO public.parameters "
                "(id, name, tax_benefit_model_version_id) "
                "VALUES (:id, :name, :version_id)"
            ),
            {"id": uuid4(), "name": name, "version_id": version_id},
        )
    return CatalogVersion(
        country=country,
        model_id=model_id,
        version_id=version_id,
        policyengine_version=policyengine_version,
    )


def _parameter_ids(connection: Connection, version_id: UUID) -> dict[str, UUID]:
    rows = connection.execute(
        text(
            "SELECT name, id FROM public.parameters "
            "WHERE tax_benefit_model_version_id = :version_id"
        ),
        {"version_id": version_id},
    )
    return {row.name: row.id for row in rows}


def insert_policy(
    connection: Connection,
    catalog: CatalogVersion,
    values: Iterable[StoredValue] = (),
) -> UUID:
    """Insert a policy saved on ``catalog`` and the values it sets.

    Each value names a parameter of that catalog version by name.
    """
    policy_id = uuid4()
    connection.execute(
        text(
            "INSERT INTO public.policies (id, country_id, tax_benefit_model_id, "
            "tax_benefit_model_version_id, canonicalization_version, content_hash) "
            "VALUES (:id, :country, :model_id, :version_id, 1, :content_hash)"
        ),
        {
            "id": policy_id,
            "country": catalog.country,
            "model_id": catalog.model_id,
            "version_id": catalog.version_id,
            "content_hash": _content_hash(policy_id),
        },
    )
    parameter_ids = _parameter_ids(connection, catalog.version_id)
    insert_value = text(
        "INSERT INTO public.parameter_values "
        "(id, parameter_id, value_json, start_date, end_date, policy_id) "
        "VALUES (:id, :parameter_id, :value, :start_date, :end_date, :policy_id)"
    ).bindparams(bindparam("value", type_=JSONB))
    for stored in values:
        connection.execute(
            insert_value,
            {
                "id": uuid4(),
                "parameter_id": parameter_ids[stored.parameter_name],
                "value": stored.value,
                "start_date": stored.start_date,
                "end_date": stored.end_date,
                "policy_id": policy_id,
            },
        )
    return policy_id


def insert_dynamic(
    connection: Connection,
    catalog: CatalogVersion,
    values: Iterable[StoredValue] = (),
) -> UUID:
    """Insert a dynamic and the values it sets on ``catalog``'s parameters.

    A dynamic belongs to no country; each value names a parameter of that
    catalog version by name.
    """
    dynamic_id = uuid4()
    connection.execute(
        text("INSERT INTO public.dynamics (id, name) VALUES (:id, :name)"),
        {"id": dynamic_id, "name": f"dynamic {dynamic_id}"},
    )
    parameter_ids = _parameter_ids(connection, catalog.version_id)
    insert_value = text(
        "INSERT INTO public.parameter_values "
        "(id, parameter_id, value_json, start_date, end_date, dynamic_id) "
        "VALUES (:id, :parameter_id, :value, :start_date, :end_date, :dynamic_id)"
    ).bindparams(bindparam("value", type_=JSONB))
    for stored in values:
        connection.execute(
            insert_value,
            {
                "id": uuid4(),
                "parameter_id": parameter_ids[stored.parameter_name],
                "value": stored.value,
                "start_date": stored.start_date,
                "end_date": stored.end_date,
                "dynamic_id": dynamic_id,
            },
        )
    return dynamic_id


def insert_dataset(
    connection: Connection, catalog: CatalogVersion, name: str, *, year: int = 2024
) -> UUID:
    """Insert a dataset of ``catalog`` with this name."""
    dataset_id = uuid4()
    connection.execute(
        text(
            "INSERT INTO public.datasets (id, name, year, is_output_dataset, "
            "tax_benefit_model_version_id) "
            "VALUES (:id, :name, :year, false, :version_id)"
        ),
        {
            "id": dataset_id,
            "name": name,
            "year": year,
            "version_id": catalog.version_id,
        },
    )
    return dataset_id


def insert_region(
    connection: Connection,
    catalog: CatalogVersion,
    code: str,
    *,
    default_dataset_id: UUID,
    region_type: str = "national",
) -> UUID:
    """Insert a region of ``catalog`` with this code and default dataset."""
    region_id = uuid4()
    connection.execute(
        text(
            "INSERT INTO public.regions (id, code, label, region_type, "
            "requires_filter, default_dataset_id, tax_benefit_model_version_id) "
            "VALUES (:id, :code, :code, CAST(:region_type AS v2_region_type), "
            "false, :dataset_id, :version_id)"
        ),
        {
            "id": region_id,
            "code": code,
            "region_type": region_type,
            "dataset_id": default_dataset_id,
            "version_id": catalog.version_id,
        },
    )
    return region_id


def insert_household(
    connection: Connection,
    *,
    country: str,
    default_year: int | None,
    document: Mapping[str, object],
) -> UUID:
    """Insert a stored household of ``country`` with this document."""
    household_id = uuid4()
    connection.execute(
        text(
            "INSERT INTO public.households (id, household_data, country_id, "
            "default_year, canonicalization_version, content_hash) "
            "VALUES (:id, :document, :country, :default_year, 1, :content_hash)"
        ).bindparams(bindparam("document", type_=JSONB)),
        {
            "id": household_id,
            "document": dict(document),
            "country": country,
            "default_year": default_year,
            "content_hash": _content_hash(household_id),
        },
    )
    return household_id
