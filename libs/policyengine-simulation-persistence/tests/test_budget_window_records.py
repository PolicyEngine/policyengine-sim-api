"""A budget-window report's inputs: its catalog rows and both sides' values.

A budget window compares the same baseline and reform in each year of its
window, so both sides' values are read once for every year. These run
against a database the Public API's own v2 migration chain built
(``PUBLIC_API_SCHEMA_DATABASE_URL``), acting as the simulation service's
role in a transaction the test rolls back.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from conftest import act_as_the_service, economy_store_on, rolled_back_transaction
from economy_rows import CURRENT_LAW, POLICYENGINE_VERSION, insert_economy_catalog
from policyengine_simulation_persistence import (
    EconomyRequest,
    SimulationChoice,
)
from public_api_rows import StoredValue, insert_policy

pytestmark = pytest.mark.public_api_schema

START = datetime(2026, 1, 1, tzinfo=UTC)
CTC_AMOUNT = "gov.irs.credits.ctc.amount.base[0].amount"
REQUEST = EconomyRequest(
    country="us",
    policyengine_version=POLICYENGINE_VERSION,
    region_code="us",
    dataset_name=None,
    year=2026,
)


def test_a_windows_catalog_rows_and_both_sides_values_are_read(
    public_api_database_url,
) -> None:
    with rolled_back_transaction(public_api_database_url) as connection:
        economy = insert_economy_catalog(connection, parameter_names=(CTC_AMOUNT,))
        policy_id = insert_policy(
            connection, economy.catalog, [StoredValue(CTC_AMOUNT, 3_000, START)]
        )
        act_as_the_service(connection)
        reform_choice = SimulationChoice(policy_id=policy_id, dynamic_id=None)

        inputs = economy_store_on(connection).read_budget_window_inputs(
            REQUEST, baseline=CURRENT_LAW, reform=reform_choice
        )

    assert inputs.catalog.catalog_version_id == economy.catalog.version_id
    assert (inputs.baseline.choice, inputs.baseline.reform) == (CURRENT_LAW, {})
    assert inputs.reform.choice == reform_choice
    assert inputs.reform.reform == {CTC_AMOUNT: {"2026-01-01.2200-12-31": 3_000}}
