"""simulation.simulations: what to run, independent of model versions."""

from __future__ import annotations

from uuid import uuid4

import pytest
from policyengine_simulation_persistence import (
    NewSimulation,
    SimulationKind,
    SimulationRecord,
    create_simulation_engine,
)
from policyengine_simulation_persistence.simulations import simulations
from public_api_support import delete_public_api_row
from simulation_support import (
    economy_simulation,
    household_simulation,
    simulation_store,
)
from sqlalchemy import insert
from sqlalchemy.exc import IntegrityError

pytestmark = pytest.mark.postgres


def test_an_economy_simulation_names_a_region_by_code(
    migrated_database, public_api_rows
) -> None:
    store = simulation_store(migrated_database)

    created = store.create(
        economy_simulation(public_api_rows, dataset_name="populace_us_2024")
    )

    assert isinstance(created, SimulationRecord)
    assert created.kind is SimulationKind.ECONOMY
    assert created.country == "us"
    assert created.policy_id == public_api_rows.us_policy_id
    assert created.region_code == "state/ca"
    assert created.dataset_name == "populace_us_2024"
    assert created.household_id is None
    assert created.created_at.tzinfo is not None
    assert store.get(created.id) == created


def test_a_null_dataset_name_means_the_region_default(
    migrated_database, public_api_rows
) -> None:
    created = simulation_store(migrated_database).create(
        economy_simulation(public_api_rows)
    )

    assert created.dataset_name is None


def test_current_law_has_no_policy(migrated_database, public_api_rows) -> None:
    created = simulation_store(migrated_database).create(
        economy_simulation(public_api_rows, policy_id=None)
    )

    assert created.policy_id is None


def test_a_household_simulation_names_its_household_and_dynamic(
    migrated_database, public_api_rows
) -> None:
    created = simulation_store(migrated_database).create(
        household_simulation(
            public_api_rows,
            policy_id=public_api_rows.us_policy_id,
            dynamic_id=public_api_rows.dynamic_id,
        )
    )

    assert created.kind is SimulationKind.HOUSEHOLD
    assert created.household_id == public_api_rows.us_household_id
    assert created.dynamic_id == public_api_rows.dynamic_id
    assert created.region_code is None


def _mismatched_population(rows, case: str) -> NewSimulation:
    household = {"kind": SimulationKind.HOUSEHOLD, "household_id": rows.us_household_id}
    economy = {"kind": SimulationKind.ECONOMY, "region_code": "state/ca"}
    values = {
        "household-without-household": {**household, "household_id": None},
        "household-with-region": {**household, "region_code": "state/ca"},
        "household-with-dataset": {**household, "dataset_name": "populace_us_2024"},
        "economy-without-region": {**economy, "region_code": None},
        "economy-with-household": {**economy, "household_id": rows.us_household_id},
    }[case]
    return NewSimulation(country="us", **values)


@pytest.mark.parametrize(
    "case",
    [
        "household-without-household",
        "household-with-region",
        "household-with-dataset",
        "economy-without-region",
        "economy-with-household",
    ],
)
def test_the_population_must_match_the_kind(
    migrated_database, public_api_rows, case
) -> None:
    with pytest.raises(IntegrityError, match="population_matches_kind"):
        simulation_store(migrated_database).create(
            _mismatched_population(public_api_rows, case)
        )


@pytest.mark.parametrize(
    ("kind", "reference"),
    [
        (SimulationKind.ECONOMY, "policy"),
        (SimulationKind.HOUSEHOLD, "household"),
    ],
)
def test_a_policy_or_household_must_be_of_the_simulation_country(
    migrated_database, public_api_rows, kind, reference
) -> None:
    if reference == "policy":
        new_simulation = economy_simulation(
            public_api_rows, country="uk", region_code="country/wales"
        )
        constraint = "fk_simulations_policy_country"
    else:
        new_simulation = household_simulation(public_api_rows, country="uk")
        constraint = "fk_simulations_household_country"
    assert new_simulation.kind is kind

    with pytest.raises(IntegrityError, match=constraint):
        simulation_store(migrated_database).create(new_simulation)


def test_an_unknown_dynamic_is_rejected(migrated_database, public_api_rows) -> None:
    with pytest.raises(IntegrityError, match="fk_simulations_dynamic"):
        simulation_store(migrated_database).create(
            economy_simulation(public_api_rows, dynamic_id=uuid4())
        )


def _simulation_using(rows, table_name: str) -> NewSimulation:
    if table_name == "households":
        return household_simulation(rows)
    if table_name == "dynamics":
        return economy_simulation(rows, dynamic_id=rows.dynamic_id)
    return economy_simulation(rows)


@pytest.mark.parametrize(
    ("table_name", "row_attribute"),
    [
        ("policies", "us_policy_id"),
        ("households", "us_household_id"),
        ("dynamics", "dynamic_id"),
    ],
)
def test_the_public_api_cannot_delete_a_row_a_simulation_uses(
    migrated_database, public_api_rows, table_name, row_attribute
) -> None:
    simulation_store(migrated_database).create(
        _simulation_using(public_api_rows, table_name)
    )

    with pytest.raises(IntegrityError, match="violates foreign key constraint"):
        delete_public_api_row(
            migrated_database.superuser_url,
            table_name,
            getattr(public_api_rows, row_attribute),
        )


def test_a_migrated_simulation_is_created_once(
    migrated_database, public_api_rows
) -> None:
    store = simulation_store(migrated_database)
    first = store.create_migrated(
        economy_simulation(public_api_rows, legacy_simulation_id=4021)
    )

    again = store.create_migrated(
        economy_simulation(
            public_api_rows, legacy_simulation_id=4021, region_code="state/ny"
        )
    )

    assert again == first
    assert store.get_by_legacy_simulation_id(4021) == first
    assert store.get_by_legacy_simulation_id(4022) is None


def test_a_migrated_simulation_needs_its_legacy_id(
    migrated_database, public_api_rows
) -> None:
    with pytest.raises(ValueError, match="legacy_simulation_id"):
        simulation_store(migrated_database).create_migrated(
            economy_simulation(public_api_rows)
        )


def test_two_simulations_cannot_share_a_legacy_id(
    migrated_database, public_api_rows
) -> None:
    store = simulation_store(migrated_database)
    store.create(economy_simulation(public_api_rows, legacy_simulation_id=7))

    with pytest.raises(IntegrityError, match="legacy_simulation_id"):
        store.create(economy_simulation(public_api_rows, legacy_simulation_id=7))


def test_an_unknown_simulation_reads_as_none(migrated_database) -> None:
    assert simulation_store(migrated_database).get(uuid4()) is None


@pytest.mark.parametrize(
    ("column", "value", "constraint"),
    [
        ("kind", "microsimulation", "ck_simulations_kind"),
        ("country", "USA", "ck_simulations_country"),
        ("region_code", "state ca", "ck_simulations_region_code"),
        ("dataset_name", "populace us", "ck_simulations_dataset_name"),
        ("legacy_simulation_id", 0, "ck_simulations_legacy_simulation_id"),
    ],
)
def test_the_database_checks_each_simulation_field(
    migrated_database, public_api_rows, column, value, constraint
) -> None:
    """The database checks what NewSimulation also checks, for any other writer."""
    values = {
        "id": uuid4(),
        "kind": "economy",
        "country": "us",
        "region_code": "state/ca",
        column: value,
    }
    engine = create_simulation_engine(migrated_database.runtime_url)
    try:
        with (
            pytest.raises(IntegrityError, match=constraint),
            engine.begin() as connection,
        ):
            connection.execute(insert(simulations).values(**values))
    finally:
        engine.dispose()
