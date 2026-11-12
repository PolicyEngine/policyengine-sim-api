"""A stored dynamic's parameter values, and combining them with a policy's.

The reads run against a database the Public API's own v2 migration chain built
(``PUBLIC_API_SCHEMA_DATABASE_URL``), as the simulation service's role, in a
transaction rolled back afterwards. Combining needs no database.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from conftest import act_as_the_service, rolled_back_transaction
from policyengine_simulation_persistence import (
    MissingPolicyParametersError,
    OverlappingReformError,
    UnknownDynamicError,
    combine_reforms,
    read_dynamic_reform,
)
from public_api_rows import (
    StoredValue,
    insert_catalog_version,
    insert_dynamic,
    insert_model,
)

START = datetime(2026, 1, 1, tzinfo=UTC)
END = datetime(2027, 12, 31, tzinfo=UTC)
ELASTICITY = "gov.simulation.labor_supply_responses.elasticities.income"
SUBSTITUTION = "gov.simulation.labor_supply_responses.elasticities.substitution.all"
CTC_AMOUNT = "gov.irs.credits.ctc.amount.base[0].amount"


@pytest.mark.public_api_schema
def test_a_dynamic_is_read_as_its_values_by_parameter_name_and_period(
    public_api_database_url,
) -> None:
    with rolled_back_transaction(public_api_database_url) as connection:
        catalog = insert_catalog_version(
            connection,
            country="us",
            model_id=insert_model(connection, "us"),
            policyengine_version="6.17.1",
            parameter_names=[ELASTICITY, SUBSTITUTION],
        )
        dynamic_id = insert_dynamic(
            connection,
            catalog,
            [
                StoredValue(ELASTICITY, -0.05, START, END),
                StoredValue(SUBSTITUTION, 0.25, START),
            ],
        )
        act_as_the_service(connection)

        reform = read_dynamic_reform(
            connection, dynamic_id=dynamic_id, catalog_version_id=catalog.version_id
        )

    assert reform == {
        ELASTICITY: {"2026-01-01.2027-12-31": -0.05},
        SUBSTITUTION: {"2026-01-01.2200-12-31": 0.25},
    }


@pytest.mark.public_api_schema
def test_an_unknown_dynamic_is_refused(public_api_database_url) -> None:
    with rolled_back_transaction(public_api_database_url) as connection:
        catalog = insert_catalog_version(
            connection,
            country="us",
            model_id=insert_model(connection, "us"),
            policyengine_version="6.17.1",
        )
        act_as_the_service(connection)
        missing_dynamic_id = uuid4()

        with pytest.raises(UnknownDynamicError, match=str(missing_dynamic_id)):
            read_dynamic_reform(
                connection,
                dynamic_id=missing_dynamic_id,
                catalog_version_id=catalog.version_id,
            )


@pytest.mark.public_api_schema
def test_a_dynamic_changing_parameters_the_run_lacks_names_each_of_them(
    public_api_database_url,
) -> None:
    with rolled_back_transaction(public_api_database_url) as connection:
        model_id = insert_model(connection, "us")
        saved_on = insert_catalog_version(
            connection,
            country="us",
            model_id=model_id,
            policyengine_version="6.16.0",
            parameter_names=[ELASTICITY, SUBSTITUTION],
        )
        running = insert_catalog_version(
            connection,
            country="us",
            model_id=model_id,
            policyengine_version="6.17.1",
            parameter_names=[ELASTICITY],
        )
        dynamic_id = insert_dynamic(
            connection,
            saved_on,
            [
                StoredValue(ELASTICITY, -0.05, START),
                StoredValue(SUBSTITUTION, 0.25, START),
            ],
        )
        act_as_the_service(connection)

        with pytest.raises(MissingPolicyParametersError) as refusal:
            read_dynamic_reform(
                connection,
                dynamic_id=dynamic_id,
                catalog_version_id=running.version_id,
            )

    assert refusal.value.parameter_names == (SUBSTITUTION,)
    assert refusal.value.source == "dynamic"
    assert str(refusal.value).startswith("the dynamic sets parameters")


def test_a_policy_and_a_dynamic_combine_by_parameter() -> None:
    policy = {CTC_AMOUNT: {"2026-01-01.2200-12-31": 3_000}}
    dynamic = {ELASTICITY: {"2026-01-01.2200-12-31": -0.05}}

    assert combine_reforms(policy, dynamic) == {
        CTC_AMOUNT: {"2026-01-01.2200-12-31": 3_000},
        ELASTICITY: {"2026-01-01.2200-12-31": -0.05},
    }
    assert combine_reforms(policy, {}) == policy
    assert combine_reforms({}, dynamic) == dynamic


def test_a_parameter_the_policy_and_the_dynamic_both_set_is_refused() -> None:
    policy = {
        ELASTICITY: {"2026-01-01.2200-12-31": -0.1},
        CTC_AMOUNT: {"2026-01-01.2200-12-31": 3_000},
    }
    dynamic = {
        ELASTICITY: {"2030-01-01.2200-12-31": -0.05},
        SUBSTITUTION: {"2026-01-01.2200-12-31": 0.25},
    }

    with pytest.raises(OverlappingReformError) as refusal:
        combine_reforms(policy, dynamic)

    assert refusal.value.parameter_names == (ELASTICITY,)
