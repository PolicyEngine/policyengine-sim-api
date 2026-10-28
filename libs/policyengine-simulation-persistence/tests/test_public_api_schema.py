"""Sim's test copies of the Public API's tables match the Public API's own schema.

Rule sim.persistence.public-api-copies-match. ``PUBLIC_API_SCHEMA_DATABASE_URL``
names a database the Public API's v2 migration chain built (the CI job
``public-api-schema`` builds it from the Public API's default branch). The
copies are the tables ``public_api_support`` creates in a disposable
database. Without the variable the comparison skips, unless
``SIMULATION_REQUIRE_PUBLIC_API_SCHEMA=1`` (set in that job), where it fails
instead.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from public_api_schema import (
    ColumnShape,
    SchemaShape,
    TableShape,
    describe_tables,
    public_tables,
    schema_differences,
    schema_from_json,
)
from public_api_support import superuser_engine

pytestmark = pytest.mark.public_api_schema

REFERENCE_DATABASE_VARIABLE = "PUBLIC_API_SCHEMA_DATABASE_URL"
REQUIRE_REFERENCE_VARIABLE = "SIMULATION_REQUIRE_PUBLIC_API_SCHEMA"
PROOF_FIXTURES = (
    Path(__file__).resolve().parents[3]
    / "rules"
    / "fixtures"
    / "sim.persistence.public-api-copies-match"
)


@pytest.fixture
def public_api_schema_url() -> str:
    """Return the URL of the database the Public API's migrations built."""
    configured = os.environ.get(REFERENCE_DATABASE_VARIABLE, "")
    if not configured:
        message = f"{REFERENCE_DATABASE_VARIABLE} names no Public API database"
        if os.environ.get(REQUIRE_REFERENCE_VARIABLE) == "1":
            pytest.fail(message)
        pytest.skip(message)
    return configured


def test_the_copies_of_the_public_api_tables_match_its_schema(
    disposable_database, public_api_schema_url
) -> None:
    copies_engine = superuser_engine(disposable_database.superuser_url)
    real_engine = superuser_engine(public_api_schema_url)
    try:
        with copies_engine.connect() as connection:
            copies = describe_tables(connection, public_tables(connection))
        with real_engine.connect() as connection:
            real = describe_tables(connection, copies.tables)
    finally:
        copies_engine.dispose()
        real_engine.dispose()

    assert copies.tables
    assert schema_differences(copies, real) == []


def test_rule_proof_public_api_copies_match() -> None:
    """Proof for rule sim.persistence.public-api-copies-match.

    ``public_api_schema.json`` describes the Public API's tables as its v2
    migration chain builds them. The bad copies are the good ones with the
    mistake an earlier review found: ``regions.region_type`` copied as
    ``varchar(64)``, where the Public API's column is the enum
    ``v2_region_type``.
    """
    real = schema_from_json(PROOF_FIXTURES / "public_api_schema.json")
    bad = schema_from_json(PROOF_FIXTURES / "bad_copies.json")
    good = schema_from_json(PROOF_FIXTURES / "good_copies.json")

    assert schema_differences(bad, real) == [
        "regions.region_type: the copy is character varying(64), the Public "
        "API's is enum v2_region_type",
    ]
    assert schema_differences(good, real) == []


def _regions(**changes) -> SchemaShape:
    """Return a regions table with an enum column, changed as given."""
    columns = {
        "id": ColumnShape("uuid", nullable=False),
        "code": ColumnShape("character varying(255)", nullable=False),
        "region_type": ColumnShape("enum v2_region_type", nullable=False),
        "state_code": ColumnShape("character varying(16)", nullable=True),
    }
    columns.update(changes.pop("columns", {}))
    constraints = {
        "pk_regions": ("id",),
        "uq_regions_model_version_code": ("tax_benefit_model_version_id", "code"),
    }
    constraints.update(changes.pop("constraints", {}))
    enums = changes.pop("enums", {"v2_region_type": ("national", "state")})
    return SchemaShape(
        tables={"regions": TableShape(columns=columns, unique_constraints=constraints)},
        enums=enums,
    )


@pytest.mark.parametrize(
    ("copies", "real", "difference"),
    [
        (
            _regions(
                columns={
                    "state_code": ColumnShape("character varying(16)", nullable=False)
                }
            ),
            _regions(),
            "regions.state_code: the copy refuses null, the Public API's allows it",
        ),
        (
            _regions(columns={"parent_code": ColumnShape("text", nullable=True)}),
            _regions(),
            "regions.parent_code: the Public API's table has no such column",
        ),
        (
            _regions(constraints={"uq_regions_code": ("code",)}),
            _regions(),
            "regions: the Public API's table has no constraint uq_regions_code (code)",
        ),
        (
            _regions(
                constraints={
                    "uq_regions_model_version_code": (
                        "code",
                        "tax_benefit_model_version_id",
                    )
                }
            ),
            _regions(),
            "regions: uq_regions_model_version_code is (code, "
            "tax_benefit_model_version_id) in the copy, "
            "(tax_benefit_model_version_id, code) in the Public API",
        ),
        (
            _regions(),
            _regions(enums={"v2_region_type": ("national", "state", "city")}),
            "enum v2_region_type: the copy has (national, state), the Public API "
            "has (national, state, city)",
        ),
        (
            _regions(),
            _regions(enums={}),
            "enum v2_region_type: the Public API has no such enum in its public schema",
        ),
        (
            _regions(),
            SchemaShape(tables={}, enums=_regions().enums),
            "regions: the Public API has no such table",
        ),
    ],
    ids=[
        "nullability",
        "missing-column",
        "missing-constraint",
        "reordered-constraint",
        "enum-values",
        "missing-enum",
        "missing-table",
    ],
)
def test_each_difference_is_reported(copies, real, difference) -> None:
    assert schema_differences(copies, real) == [difference]
