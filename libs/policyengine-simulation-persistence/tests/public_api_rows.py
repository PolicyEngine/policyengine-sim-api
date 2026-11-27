"""Insert API v1's own rows a calculation is read from, for tests.

API v1 writes households, policies, dynamics, their parameter values and its
model catalog; the tests insert them as the superuser, in a transaction they
roll back, before acting as the simulation service's role.

API v1 keeps two kinds of parameter apart (C1 · 50): a dynamic's parameters
(``dynamic_parameters``), with their current-law values and every dynamic's
values in ``dynamic_parameter_values``, and a policy's (``parameters``), with
their current-law values and every policy's values in ``parameter_values``.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from typing import TYPE_CHECKING, Final, Literal
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.sql import bindparam

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping
    from datetime import datetime

    from sqlalchemy import Connection, TextClause

# Which of API v1's tables a parameter is stored in.
type ParameterKind = Literal["policy", "dynamic"]

# API v1's ``DYNAMIC_PARAMETER_PREFIXES``: a name under one of its country's
# prefixes is a dynamic parameter, and any other a policy parameter.
DYNAMIC_PARAMETER_PREFIXES: Final[Mapping[str, tuple[str, ...]]] = {
    "uk": ("gov.simulation.", "gov.dynamic."),
    "us": ("gov.simulation.", "simulation."),
}


@dataclass(frozen=True)
class CatalogVersion:
    """A country's tax-benefit model and one published version of its catalog."""

    country: str
    model_id: UUID
    version_id: UUID
    policyengine_version: str


@dataclass(frozen=True)
class StoredValue:
    """One parameter value a policy or dynamic sets, as API v1 stores it."""

    parameter_name: str
    value: object
    start_date: datetime
    end_date: datetime | None = None


def _stand_in_content_hash(identifier: UUID) -> str:
    """Return a 64-character hash of a new row's ID, in place of its content's.

    API v1 hashes a row's content; hashing the ID instead keeps every row a
    test inserts distinct, even when two have the same content.
    """
    return sha256(identifier.bytes).hexdigest()


def insert_model(connection: Connection, country: str) -> UUID:
    """Insert the country's tax-benefit model, named as API v1 names it."""
    model_id = uuid4()
    connection.execute(
        text("INSERT INTO public.tax_benefit_models (id, name) VALUES (:id, :name)"),
        {"id": model_id, "name": f"policyengine-{country}"},
    )
    return model_id


def parameter_kind(country: str, parameter_name: str) -> ParameterKind:
    """Return the kind of the country's parameter, by API v1's prefixes."""
    if parameter_name.startswith(DYNAMIC_PARAMETER_PREFIXES[country]):
        return "dynamic"
    return "policy"


@dataclass(frozen=True)
class _ParameterStatements:
    """The statements that store one kind of parameter and its values.

    ``owned_value`` stores a value a policy or dynamic sets, binding its ID as
    ``owner_id``; ``current_law_value`` stores one with no owner.
    """

    parameter: TextClause
    parameter_ids: TextClause
    current_law_value: TextClause
    owned_value: TextClause


_PARAMETER_STATEMENTS: Final[Mapping[ParameterKind, _ParameterStatements]] = {
    "policy": _ParameterStatements(
        parameter=text(
            "INSERT INTO public.parameters (id, name, tax_benefit_model_version_id) "
            "VALUES (:id, :name, :version_id)"
        ),
        parameter_ids=text(
            "SELECT name, id FROM public.parameters "
            "WHERE tax_benefit_model_version_id = :version_id"
        ),
        current_law_value=text(
            "INSERT INTO public.parameter_values "
            "(id, parameter_id, value_json, start_date, end_date) "
            "VALUES (:id, :parameter_id, :value, :start_date, :end_date)"
        ).bindparams(bindparam("value", type_=JSONB)),
        owned_value=text(
            "INSERT INTO public.parameter_values "
            "(id, parameter_id, value_json, start_date, end_date, policy_id) "
            "VALUES (:id, :parameter_id, :value, :start_date, :end_date, :owner_id)"
        ).bindparams(bindparam("value", type_=JSONB)),
    ),
    "dynamic": _ParameterStatements(
        parameter=text(
            "INSERT INTO public.dynamic_parameters "
            "(id, name, tax_benefit_model_version_id) "
            "VALUES (:id, :name, :version_id)"
        ),
        parameter_ids=text(
            "SELECT name, id FROM public.dynamic_parameters "
            "WHERE tax_benefit_model_version_id = :version_id"
        ),
        current_law_value=text(
            "INSERT INTO public.dynamic_parameter_values "
            "(id, dynamic_parameter_id, value_json, start_date, end_date) "
            "VALUES (:id, :parameter_id, :value, :start_date, :end_date)"
        ).bindparams(bindparam("value", type_=JSONB)),
        owned_value=text(
            "INSERT INTO public.dynamic_parameter_values "
            "(id, dynamic_parameter_id, value_json, start_date, end_date, "
            "dynamic_id) "
            "VALUES (:id, :parameter_id, :value, :start_date, :end_date, :owner_id)"
        ).bindparams(bindparam("value", type_=JSONB)),
    ),
}


def insert_catalog_version(
    connection: Connection,
    *,
    country: str,
    model_id: UUID,
    policyengine_version: str,
    parameter_names: Iterable[str] = (),
) -> CatalogVersion:
    """Insert a catalog version of the model and its named parameters.

    As API v1's publication does, each name is stored in the table of its
    kind (``parameter_kind``).
    """
    version_id = uuid4()
    connection.execute(
        text(
            "INSERT INTO public.tax_benefit_model_versions "
            "(id, model_id, version, current_law_id, metadata_time_periods) "
            "VALUES (:id, :model_id, :version, 1, '[]')"
        ),
        {"id": version_id, "model_id": model_id, "version": policyengine_version},
    )
    catalog = CatalogVersion(
        country=country,
        model_id=model_id,
        version_id=version_id,
        policyengine_version=policyengine_version,
    )
    for name in parameter_names:
        insert_parameter(connection, catalog, name, kind=parameter_kind(country, name))
    return catalog


def insert_current_law_values(
    connection: Connection, catalog: CatalogVersion, values: Iterable[StoredValue]
) -> None:
    """Insert current-law values of ``catalog``'s parameters, as its publication does.

    Each value names a parameter of the catalog version and is stored with no
    owner, beside the other values of its parameter's kind.
    """
    for stored in values:
        kind = parameter_kind(catalog.country, stored.parameter_name)
        _insert_value(
            connection,
            _PARAMETER_STATEMENTS[kind].current_law_value,
            stored,
            parameter_id=_parameter_ids(connection, catalog.version_id, kind)[
                stored.parameter_name
            ],
        )


def insert_parameter(
    connection: Connection, catalog: CatalogVersion, name: str, *, kind: ParameterKind
) -> UUID:
    """Insert a parameter of ``catalog`` named ``name`` in the table of ``kind``.

    ``insert_catalog_version`` stores each name in the table of its own kind,
    as API v1 does. A test of a policy and a dynamic that set one parameter
    adds the name to the other table with this, a state API v1 never stores.
    """
    parameter_id = uuid4()
    connection.execute(
        _PARAMETER_STATEMENTS[kind].parameter,
        {"id": parameter_id, "name": name, "version_id": catalog.version_id},
    )
    return parameter_id


def _parameter_ids(
    connection: Connection, version_id: UUID, kind: ParameterKind
) -> dict[str, UUID]:
    rows = connection.execute(
        _PARAMETER_STATEMENTS[kind].parameter_ids, {"version_id": version_id}
    )
    return {row.name: row.id for row in rows}


def _insert_value(
    connection: Connection,
    statement: TextClause,
    stored: StoredValue,
    *,
    parameter_id: UUID,
    owner_id: UUID | None = None,
) -> None:
    connection.execute(
        statement,
        {
            "id": uuid4(),
            "parameter_id": parameter_id,
            "value": stored.value,
            "start_date": stored.start_date,
            "end_date": stored.end_date,
            **({} if owner_id is None else {"owner_id": owner_id}),
        },
    )


@dataclass(frozen=True)
class _SavedReform:
    """How a policy or dynamic is saved: its row and the kind of parameter it sets."""

    row: TextClause
    parameter_kind: ParameterKind


_POLICY = _SavedReform(
    row=text(
        "INSERT INTO public.policies (id, country_id, tax_benefit_model_id, "
        "tax_benefit_model_version_id, canonicalization_version, content_hash) "
        "VALUES (:id, :country, :model_id, :version_id, 1, :content_hash)"
    ),
    parameter_kind="policy",
)
_DYNAMIC = _SavedReform(
    row=text(
        "INSERT INTO public.dynamics (id, country_id, tax_benefit_model_id, "
        "tax_benefit_model_version_id, canonicalization_version, content_hash) "
        "VALUES (:id, :country, :model_id, :version_id, 1, :content_hash)"
    ),
    parameter_kind="dynamic",
)


def _insert_saved_reform(
    connection: Connection,
    catalog: CatalogVersion,
    values: Iterable[StoredValue],
    saved: _SavedReform,
) -> UUID:
    """Insert a policy or dynamic saved on ``catalog`` and the values it sets.

    API v1 stores both alike: on the catalog's country, model and version,
    with a canonicalization version and a content hash. Each value names a
    parameter of that catalog version by name, among the parameters of the
    kind the policy or dynamic sets.
    """
    saved_id = uuid4()
    connection.execute(
        saved.row,
        {
            "id": saved_id,
            "country": catalog.country,
            "model_id": catalog.model_id,
            "version_id": catalog.version_id,
            "content_hash": _stand_in_content_hash(saved_id),
        },
    )
    statements = _PARAMETER_STATEMENTS[saved.parameter_kind]
    parameter_ids = _parameter_ids(connection, catalog.version_id, saved.parameter_kind)
    for stored in values:
        _insert_value(
            connection,
            statements.owned_value,
            stored,
            parameter_id=parameter_ids[stored.parameter_name],
            owner_id=saved_id,
        )
    return saved_id


def insert_policy(
    connection: Connection,
    catalog: CatalogVersion,
    values: Iterable[StoredValue] = (),
) -> UUID:
    """Insert a policy saved on ``catalog`` and the values it sets.

    Each value names a policy parameter of ``catalog``.
    """
    return _insert_saved_reform(connection, catalog, values, _POLICY)


def insert_dynamic(
    connection: Connection,
    catalog: CatalogVersion,
    values: Iterable[StoredValue] = (),
) -> UUID:
    """Insert a dynamic saved on ``catalog`` and the values it sets.

    Each value names a dynamic parameter of ``catalog``.
    """
    return _insert_saved_reform(connection, catalog, values, _DYNAMIC)


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
            "content_hash": _stand_in_content_hash(household_id),
        },
    )
    return household_id
