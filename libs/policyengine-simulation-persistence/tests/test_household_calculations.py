"""Household simulations are found by what they calculate, or created once."""

from __future__ import annotations

import pytest
from policyengine_simulation_persistence import (
    HouseholdCalculationStore,
    create_simulation_engine,
)
from public_api_support import superuser_engine
from simulation_support import (
    economy_simulation,
    household_simulation,
    simulation_store,
)
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

pytestmark = pytest.mark.postgres

EARNINGS_AXES = [
    [{"name": "employment_income", "count": 3, "min": 0, "max": 20_000}],
]


def household_store(database) -> HouseholdCalculationStore:
    """Return a household calculation store that connects as the runtime role."""
    return HouseholdCalculationStore(create_simulation_engine(database.runtime_url))


def test_the_same_household_policy_and_options_find_one_simulation(
    migrated_database, public_api_rows
) -> None:
    store = household_store(migrated_database)
    definition = household_simulation(
        public_api_rows,
        policy_id=public_api_rows.us_policy_id,
        options={"add_missing_variables": True},
    )

    created = store.find_or_create_simulation(definition)
    found = store.find_or_create_simulation(definition)

    assert found.id == created.id
    assert created.options == {"add_missing_variables": True}
    assert created.policy_id == public_api_rows.us_policy_id


def test_another_policy_household_dynamic_or_options_is_another_simulation(
    migrated_database, public_api_rows
) -> None:
    store = household_store(migrated_database)
    current_law = store.find_or_create_simulation(household_simulation(public_api_rows))
    variants = [
        household_simulation(public_api_rows, policy_id=public_api_rows.us_policy_id),
        household_simulation(public_api_rows, dynamic_id=public_api_rows.dynamic_id),
        household_simulation(public_api_rows, options={"axes": EARNINGS_AXES}),
        household_simulation(
            public_api_rows, country="uk", household_id=public_api_rows.uk_household_id
        ),
    ]

    variant_ids = {store.find_or_create_simulation(variant).id for variant in variants}

    assert len(variant_ids) == len(variants)
    assert current_law.id not in variant_ids
    found_again = store.find_or_create_simulation(household_simulation(public_api_rows))
    assert found_again.id == current_law.id


def test_options_match_by_value_whatever_their_key_order(
    migrated_database, public_api_rows
) -> None:
    store = household_store(migrated_database)

    first = store.find_or_create_simulation(
        household_simulation(
            public_api_rows,
            options={"axes": EARNINGS_AXES, "add_missing_variables": False},
        )
    )
    second = store.find_or_create_simulation(
        household_simulation(
            public_api_rows,
            options={"add_missing_variables": False, "axes": EARNINGS_AXES},
        )
    )

    assert second.id == first.id


def test_a_simulation_created_without_options_has_empty_options(
    migrated_database, public_api_rows
) -> None:
    created = simulation_store(migrated_database).create(
        household_simulation(public_api_rows)
    )

    found = household_store(migrated_database).find_or_create_simulation(
        household_simulation(public_api_rows)
    )

    assert created.options == {}
    assert found.id == created.id


def test_only_a_household_simulation_is_found_by_its_household(
    migrated_database, public_api_rows
) -> None:
    with pytest.raises(ValueError, match="household simulation"):
        household_store(migrated_database).find_or_create_simulation(
            economy_simulation(public_api_rows)
        )


def test_the_database_refuses_options_that_are_not_an_object(
    migrated_database, public_api_rows
) -> None:
    created = simulation_store(migrated_database).create(
        household_simulation(public_api_rows)
    )
    engine = superuser_engine(migrated_database.superuser_url)
    try:
        with (
            pytest.raises(IntegrityError, match="ck_simulations_options"),
            engine.begin() as connection,
        ):
            connection.execute(
                text(
                    "UPDATE simulation.simulations SET options = '[]'::jsonb "
                    "WHERE id = :simulation_id"
                ),
                {"simulation_id": created.id},
            )
    finally:
        engine.dispose()
