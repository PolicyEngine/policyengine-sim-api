"""Sim's table models match the tables the Public API's migration chain builds.

Rule sim.persistence.models-match-public-api. ``PUBLIC_API_SCHEMA_DATABASE_URL``
names a database the Public API's v2 migration chain built at the pinned
commit (the ``public-api-schema`` job builds it). The models' own tables are
built from ``service_metadata`` in a disposable database, and both are read
through the same catalog queries. Without the Public API's database the
comparison skips, unless ``SIMULATION_REQUIRE_PUBLIC_API_SCHEMA=1`` (set in
that job), where it fails instead.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from policyengine_simulation_persistence import service_metadata
from schema_comparison import describe_tables, schema_differences, schema_from_json
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url

# The reference script sets it on GitHub Actions: the Public API commit built.
COMMIT_VARIABLE = "PUBLIC_API_SCHEMA_COMMIT"
PROOF_FIXTURES = (
    Path(__file__).resolve().parents[3]
    / "rules"
    / "fixtures"
    / "sim.persistence.models-match-public-api"
)


def _engine(database_url: str):
    return create_engine(make_url(database_url).set(drivername="postgresql+psycopg"))


# Not marked postgres: the Simulation database job, which selects that
# marker, has no Public API database, so the test could only skip there.
@pytest.mark.public_api_schema
def test_the_models_match_the_public_api_tables(
    disposable_database, public_api_database_url
) -> None:
    table_names = [table.name for table in service_metadata.tables.values()]
    models_engine = _engine(disposable_database.superuser_url)
    real_engine = _engine(public_api_database_url)
    try:
        # Tests may build the models' tables; runtime code never does.
        service_metadata.create_all(models_engine)
        with models_engine.connect() as connection:
            models = describe_tables(connection, table_names)
        with real_engine.connect() as connection:
            real = describe_tables(connection, table_names)
    finally:
        models_engine.dispose()
        real_engine.dispose()

    assert set(models.tables) == set(table_names)
    compared_with = os.environ.get(COMMIT_VARIABLE) or "an unrecorded commit"
    assert schema_differences(models, real) == [], (
        f"compared with the Public API at {compared_with}"
    )


# Marked so the rule's own CI jobs, which select public_api_schema, collect it.
@pytest.mark.public_api_schema
def test_rule_proof_models_match_public_api() -> None:
    """Proof for rule sim.persistence.models-match-public-api.

    ``public_api_schema.json`` describes ``simulation_runs`` as the Public
    API's chain builds it. The bad models copy ``status`` as
    ``character varying(32)``, where the Public API's column is the enum
    ``v2_simulation_status``, and leave out ``modal_call_id``, which a later
    revision added.
    """
    real = schema_from_json(PROOF_FIXTURES / "public_api_schema.json")
    bad = schema_from_json(PROOF_FIXTURES / "bad_models.json")
    good = schema_from_json(PROOF_FIXTURES / "good_models.json")

    assert schema_differences(bad, real) == [
        "simulation_runs.status: the model is character varying(32), the Public "
        "API's is enum v2_simulation_status",
        "simulation_runs.modal_call_id: the model has no such column",
    ]
    assert schema_differences(good, real) == []
