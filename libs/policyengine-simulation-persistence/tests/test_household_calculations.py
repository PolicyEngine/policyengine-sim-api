"""Household calculations: simulations found by what they calculate, and read back."""

from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

import pytest
from policyengine_simulation_persistence import (
    HouseholdCalculationStore,
    HouseholdOutput,
    RunError,
    RunErrorCode,
    RunStatus,
    create_simulation_engine,
    write_household_output,
)
from public_api_support import superuser_engine
from run_support import economy_run, household_run
from simulation_support import (
    economy_simulation,
    household_simulation,
    simulation_store,
)
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from transition_support import ManualClock, clocked_stores

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


def clocked_household_store(database, clock: ManualClock) -> HouseholdCalculationStore:
    """Return a household calculation store whose runs use ``clock``."""
    return HouseholdCalculationStore(
        create_simulation_engine(database.runtime_url), clock=clock
    )


def _calculation_run(store, rows, **overrides):
    simulation = store.find_or_create_simulation(household_simulation(rows))
    return store.start_run(household_run(simulation, rows, **overrides))


def test_a_submitted_calculation_reads_back_pending_without_output(
    migrated_database, public_api_rows
) -> None:
    store = household_store(migrated_database)
    started = _calculation_run(store, public_api_rows)

    calculation = store.read_calculation(started.run.id)

    assert started.created
    assert calculation.run.id == started.run.id
    assert calculation.run.status is RunStatus.PENDING
    assert calculation.simulation.household_id == public_api_rows.us_household_id
    assert calculation.output is None


def test_a_calculation_submitted_again_reuses_its_unfinished_run(
    migrated_database, public_api_rows
) -> None:
    store = household_store(migrated_database)
    first = _calculation_run(store, public_api_rows)

    again = _calculation_run(store, public_api_rows)

    assert again.run.id == first.run.id
    assert not again.created


def test_a_succeeded_calculation_reads_back_with_its_household(
    migrated_database, public_api_rows
) -> None:
    clock = ManualClock()
    store = clocked_household_store(migrated_database, clock)
    transitions = clocked_stores(migrated_database, clock).transitions
    run = _calculation_run(store, public_api_rows).run
    transitions.claim_simulation_dispatch([run.id])
    transitions.record_simulation_dispatched(
        [run.id], modal_call_id="fc-calculation", first_check_at=clock()
    )
    output = HouseholdOutput(
        schema_version=1,
        household={"people": {"you": {"age": {"2026": 40}}}},
        warnings=("A warning.",),
    )
    transitions.complete_simulation_call(
        "fc-calculation",
        lambda connection, runs: write_household_output(connection, runs[0], output),
    )

    calculation = store.read_calculation(run.id)

    assert calculation.run.status is RunStatus.SUCCEEDED
    assert calculation.output == output


def test_a_rejected_calculation_reads_back_with_its_error_detail(
    migrated_database, public_api_rows
) -> None:
    clock = ManualClock()
    store = clocked_household_store(migrated_database, clock)
    transitions = clocked_stores(migrated_database, clock).transitions
    run = _calculation_run(store, public_api_rows).run
    transitions.claim_simulation_dispatch([run.id])
    transitions.record_simulation_dispatched(
        [run.id], modal_call_id="fc-rejected", first_check_at=clock()
    )
    transitions.fail_simulation_call(
        "fc-rejected",
        RunError(
            RunErrorCode.HOUSEHOLD_CALCULATION_FAILED,
            "The household calculation failed: invalid_inputs.",
            detail={"code": "invalid_inputs", "message": "Unrecognized inputs"},
        ),
    )

    calculation = store.read_calculation(run.id)

    assert calculation.run.status is RunStatus.FAILED
    assert calculation.run.error_code == "household_calculation_failed"
    assert calculation.run.error_detail == {
        "code": "invalid_inputs",
        "message": "Unrecognized inputs",
    }
    assert calculation.output is None


def test_a_calculation_left_dispatching_fails_and_can_be_submitted_again(
    migrated_database, public_api_rows
) -> None:
    """The instance that claimed the run stopped before recording its call."""
    clock = ManualClock()
    store = clocked_household_store(migrated_database, clock)
    transitions = clocked_stores(migrated_database, clock).transitions
    run = _calculation_run(store, public_api_rows).run
    assert transitions.claim_simulation_dispatch([run.id])

    clock.advance(timedelta(minutes=11))
    transitions.fail_abandoned_dispatches(
        claimed_before=clock() - timedelta(minutes=10)
    )

    abandoned = store.read_calculation(run.id)
    assert abandoned.run.status is RunStatus.FAILED
    assert abandoned.run.error_code == "dispatch_abandoned"
    resubmitted = _calculation_run(store, public_api_rows)
    assert resubmitted.created
    assert resubmitted.run.attempt == 2


def test_only_a_household_run_reads_as_a_calculation(
    migrated_database, public_api_rows
) -> None:
    store = household_store(migrated_database)
    economy = simulation_store(migrated_database).create(
        economy_simulation(public_api_rows)
    )
    economy_run_record = store.start_run(economy_run(economy, public_api_rows)).run

    assert store.read_calculation(economy_run_record.id) is None
    assert store.read_calculation(uuid4()) is None
