"""Read-only models of the Public API's tables a calculation is read from.

The Public API writes households, policies, dynamics and their parameter
values, and its model catalog; the simulation service only reads them, with
``SELECT``. Each table here names only the columns the service reads, so these
are not models of the whole tables: the Public API's other columns,
defaults, constraints and indexes are its own. The tables live in
``input_metadata``, apart from the models of the tables the service writes
(``service_metadata``) and from the tables those name by ``id`` alone
(``referenced_metadata``). ``tests/test_public_api_schema.py`` compares each
column with the Public API's table.
"""

from __future__ import annotations

from typing import Final

from sqlalchemy import (
    Column,
    DateTime,
    Enum,
    Integer,
    MetaData,
    String,
    Table,
    Uuid,
)
from sqlalchemy.dialects.postgresql import JSONB

from policyengine_simulation_persistence.tables import TABLE_SCHEMA

input_metadata = MetaData(schema=TABLE_SCHEMA)

# A country's tax-benefit model is named "policyengine-<country>".
MODEL_NAME_PREFIX: Final = "policyengine-"
# The kinds of geography a region covers, as the Public API's
# ``v2_region_type`` lists them.
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
# The UK region types a local-area breakdown covers.
UK_AREA_REGION_TYPES: Final = ("constituency", "local_authority")

tax_benefit_models = Table(
    "tax_benefit_models",
    input_metadata,
    Column("id", Uuid, nullable=False),
    Column("name", String(32), nullable=False),
)

# One published catalog version of a model; ``version`` is the .py
# (policyengine package) version it was published from.
tax_benefit_model_versions = Table(
    "tax_benefit_model_versions",
    input_metadata,
    Column("id", Uuid, nullable=False),
    Column("model_id", Uuid, nullable=False),
    Column("version", String(128), nullable=False),
)

# A region of one catalog version, unique by code within it, and the dataset
# it uses unless a calculation names another.
regions = Table(
    "regions",
    input_metadata,
    Column("id", Uuid, nullable=False),
    Column("code", String(255), nullable=False),
    # The enum names its schema itself, as the service tables' enums do.
    Column(
        "region_type",
        Enum(*REGION_TYPES, name="v2_region_type", schema=TABLE_SCHEMA),
        nullable=False,
    ),
    Column("tax_benefit_model_version_id", Uuid, nullable=False),
    Column("default_dataset_id", Uuid, nullable=False),
)

# A dataset of one catalog version, unique by name within it.
datasets = Table(
    "datasets",
    input_metadata,
    Column("id", Uuid, nullable=False),
    Column("name", String(255), nullable=False),
    Column("tax_benefit_model_version_id", Uuid, nullable=False),
)

# ``household_data`` lists the people and each group entity as records, and
# may hold a saved SPM selection; ``default_year`` is the year it describes.
households = Table(
    "households",
    input_metadata,
    Column("id", Uuid, nullable=False),
    Column("country_id", String(2), nullable=False),
    Column("default_year", Integer, nullable=True),
    Column("household_data", JSONB, nullable=False),
)

policies = Table(
    "policies",
    input_metadata,
    Column("id", Uuid, nullable=False),
    Column("country_id", String(2), nullable=False),
)

# A catalog version's parameters, each named once within it.
parameters = Table(
    "parameters",
    input_metadata,
    Column("id", Uuid, nullable=False),
    Column("name", String(512), nullable=False),
    Column("tax_benefit_model_version_id", Uuid, nullable=False),
)

# A parameter's value from ``start_date`` until ``end_date``, or with no end
# date indefinitely; a policy's own values name it in ``policy_id``, and a
# dynamic's in ``dynamic_id``.
parameter_values = Table(
    "parameter_values",
    input_metadata,
    Column("parameter_id", Uuid, nullable=False),
    Column("value_json", JSONB, nullable=False),
    Column("start_date", DateTime(timezone=True), nullable=False),
    Column("end_date", DateTime(timezone=True), nullable=True),
    Column("policy_id", Uuid, nullable=True),
    Column("dynamic_id", Uuid, nullable=True),
)

# Named parameter values a simulation applies beside its policy, such as
# behavioural responses. A dynamic belongs to no country.
dynamics = Table(
    "dynamics",
    input_metadata,
    Column("id", Uuid, nullable=False),
)
