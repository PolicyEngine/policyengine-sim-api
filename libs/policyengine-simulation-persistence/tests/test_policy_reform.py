"""A stored policy's parameter values, as the reform values a calculation applies.

The period tests need no database. The reads run against a database the
Public API's own v2 migration chain built (``PUBLIC_API_SCHEMA_DATABASE_URL``),
as the simulation service's role, in a transaction rolled back afterwards.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from uuid import uuid4

import pytest
from conftest import act_as_the_service, rolled_back_transaction
from policyengine_simulation_persistence import (
    MissingPolicyParametersError,
    UnknownPolicyError,
    read_policy_reform,
    reform_period,
)
from public_api_rows import (
    StoredValue,
    insert_catalog_version,
    insert_model,
    insert_policy,
)

START = datetime(2026, 1, 1, tzinfo=UTC)
END = datetime(2027, 12, 31, tzinfo=UTC)
CTC_AMOUNT = "gov.irs.credits.ctc.amount.base[0].amount"
STANDARD_DEDUCTION = "gov.irs.deductions.standard.amount.SINGLE"


def test_a_value_with_an_end_date_applies_from_its_start_to_its_end() -> None:
    assert reform_period(START, END) == "2026-01-01.2027-12-31"


def test_a_value_without_an_end_date_applies_to_the_end_of_2200() -> None:
    """A value with no end date, and none after it, runs to the end of 2200.

    Household jobs and simulation runs are for years up to 2200; .py's own
    conversion stops there at 2100. A range is read the same way by every
    country's model; a start date alone is not (``test_household_calculation.py``
    in the executor shows both).
    """
    assert reform_period(START, None) == "2026-01-01.2200-12-31"


def test_a_value_without_an_end_date_stops_before_the_next_value_starts() -> None:
    """.py's rule: the parameter's next later value takes over."""
    assert (
        reform_period(START, None, next_start_date=datetime(2028, 7, 1, tzinfo=UTC))
        == "2026-01-01.2028-06-30"
    )


def test_dates_are_read_in_utc() -> None:
    eastern = timezone(timedelta(hours=-5))

    assert (
        reform_period(
            datetime(2025, 12, 31, 19, tzinfo=eastern),
            datetime(2026, 12, 31, 19, tzinfo=eastern),
        )
        == "2026-01-01.2027-01-01"
    )


def test_a_value_starting_after_2200_without_an_end_ends_where_it_starts() -> None:
    """No calculable year reaches it, and its range never ends before it starts."""
    assert reform_period(datetime(2250, 1, 1, tzinfo=UTC), None) == (
        "2250-01-01.2250-01-01"
    )


@pytest.mark.public_api_schema
def test_a_policy_is_read_as_its_values_by_parameter_name_and_period(
    public_api_database_url,
) -> None:
    with rolled_back_transaction(public_api_database_url) as connection:
        model_id = insert_model(connection, "us")
        saved_on = insert_catalog_version(
            connection,
            country="us",
            model_id=model_id,
            policyengine_version="6.16.1",
            parameter_names=[CTC_AMOUNT, STANDARD_DEDUCTION],
        )
        run_on = insert_catalog_version(
            connection,
            country="us",
            model_id=model_id,
            policyengine_version="6.17.1",
            parameter_names=[CTC_AMOUNT, STANDARD_DEDUCTION],
        )
        policy_id = insert_policy(
            connection,
            saved_on,
            [
                StoredValue(CTC_AMOUNT, 3_000, START, END),
                StoredValue(CTC_AMOUNT, 4_000, datetime(2028, 1, 1, tzinfo=UTC)),
                StoredValue(STANDARD_DEDUCTION, [1, 2], START),
            ],
        )
        act_as_the_service(connection)

        reform = read_policy_reform(
            connection,
            country="us",
            policy_id=policy_id,
            catalog_version_id=run_on.version_id,
        )

    assert reform == {
        CTC_AMOUNT: {
            "2026-01-01.2027-12-31": 3_000,
            "2028-01-01.2200-12-31": 4_000,
        },
        STANDARD_DEDUCTION: {"2026-01-01.2200-12-31": [1, 2]},
    }


def _reform_of(public_api_database_url, values: list[StoredValue]) -> dict:
    """Store a US policy setting ``values`` and read it as the service's role."""
    with rolled_back_transaction(public_api_database_url) as connection:
        catalog = insert_catalog_version(
            connection,
            country="us",
            model_id=insert_model(connection, "us"),
            policyengine_version="6.17.1",
            parameter_names=sorted({value.parameter_name for value in values}),
        )
        policy_id = insert_policy(connection, catalog, values)
        act_as_the_service(connection)
        return read_policy_reform(
            connection,
            country="us",
            policy_id=policy_id,
            catalog_version_id=catalog.version_id,
        )


@pytest.mark.public_api_schema
def test_a_value_without_an_end_date_gives_way_to_a_bounded_one_for_good(
    public_api_database_url,
) -> None:
    """After the bounded value ends, current law applies, as .py has it.

    The executor's ``test_py_reform_dicts.py`` shows .py's
    ``reform_dict_from_parameter_values`` giving these periods, with 2100
    where this has 2200.
    """
    reform = _reform_of(
        public_api_database_url,
        [
            StoredValue(CTC_AMOUNT, 3_000, START),
            StoredValue(
                CTC_AMOUNT,
                5_000,
                datetime(2028, 1, 1, tzinfo=UTC),
                datetime(2028, 12, 31, tzinfo=UTC),
            ),
        ],
    )

    assert reform == {
        CTC_AMOUNT: {
            "2026-01-01.2027-12-31": 3_000,
            "2028-01-01.2028-12-31": 5_000,
        }
    }


@pytest.mark.public_api_schema
def test_values_without_end_dates_run_one_after_another(
    public_api_database_url,
) -> None:
    """Each runs until the next starts; the last runs to the end of 2200."""
    reform = _reform_of(
        public_api_database_url,
        [
            StoredValue(CTC_AMOUNT, 1_000, START),
            StoredValue(CTC_AMOUNT, 2_000, datetime(2027, 7, 1, tzinfo=UTC)),
            StoredValue(CTC_AMOUNT, 3_000, datetime(2030, 1, 1, tzinfo=UTC)),
            StoredValue(STANDARD_DEDUCTION, 20_000, datetime(2029, 1, 1, tzinfo=UTC)),
        ],
    )

    assert reform == {
        CTC_AMOUNT: {
            "2026-01-01.2027-06-30": 1_000,
            "2027-07-01.2029-12-31": 2_000,
            "2030-01-01.2200-12-31": 3_000,
        },
        STANDARD_DEDUCTION: {"2029-01-01.2200-12-31": 20_000},
    }


@pytest.mark.public_api_schema
def test_a_policy_that_changes_nothing_is_read_as_no_values(
    public_api_database_url,
) -> None:
    with rolled_back_transaction(public_api_database_url) as connection:
        catalog = insert_catalog_version(
            connection,
            country="uk",
            model_id=insert_model(connection, "uk"),
            policyengine_version="6.17.1",
        )
        policy_id = insert_policy(connection, catalog)
        act_as_the_service(connection)

        reform = read_policy_reform(
            connection,
            country="uk",
            policy_id=policy_id,
            catalog_version_id=catalog.version_id,
        )

    assert reform == {}


@pytest.mark.public_api_schema
def test_a_policy_of_another_country_or_none_at_all_is_unknown(
    public_api_database_url,
) -> None:
    with rolled_back_transaction(public_api_database_url) as connection:
        catalog = insert_catalog_version(
            connection,
            country="us",
            model_id=insert_model(connection, "us"),
            policyengine_version="6.17.1",
        )
        policy_id = insert_policy(connection, catalog)
        act_as_the_service(connection)

        for country, unknown_policy_id in (("uk", policy_id), ("us", uuid4())):
            with pytest.raises(UnknownPolicyError) as raised:
                read_policy_reform(
                    connection,
                    country=country,
                    policy_id=unknown_policy_id,
                    catalog_version_id=catalog.version_id,
                )
            assert str(raised.value) == (f"{country} has no policy {unknown_policy_id}")


@pytest.mark.public_api_schema
def test_a_policy_changing_parameters_the_run_lacks_names_each_of_them(
    public_api_database_url,
) -> None:
    with rolled_back_transaction(public_api_database_url) as connection:
        model_id = insert_model(connection, "us")
        saved_on = insert_catalog_version(
            connection,
            country="us",
            model_id=model_id,
            policyengine_version="6.16.1",
            parameter_names=[CTC_AMOUNT, STANDARD_DEDUCTION, "gov.removed.b"],
        )
        run_on = insert_catalog_version(
            connection,
            country="us",
            model_id=model_id,
            policyengine_version="6.17.1",
            parameter_names=[CTC_AMOUNT],
        )
        policy_id = insert_policy(
            connection,
            saved_on,
            [
                StoredValue(CTC_AMOUNT, 3_000, START),
                StoredValue(STANDARD_DEDUCTION, 1, START),
                StoredValue("gov.removed.b", 2, START),
            ],
        )
        act_as_the_service(connection)

        with pytest.raises(MissingPolicyParametersError) as raised:
            read_policy_reform(
                connection,
                country="us",
                policy_id=policy_id,
                catalog_version_id=run_on.version_id,
            )

    assert raised.value.parameter_names == (STANDARD_DEDUCTION, "gov.removed.b")
    assert str(raised.value) == (
        "the policy sets parameters the catalog version does not have: "
        f"{STANDARD_DEDUCTION}, gov.removed.b"
    )


@pytest.mark.public_api_schema
def test_of_values_starting_the_same_day_the_latest_takes_over(
    public_api_database_url,
) -> None:
    """Periods are days, so the later start of one day takes that day over.

    The executor's ``test_py_reform_dicts.py`` shows .py calculating the same:
    it ends the earlier value before it starts, so only the later applies.
    """
    reform = _reform_of(
        public_api_database_url,
        [
            StoredValue(CTC_AMOUNT, 3_000, START),
            StoredValue(CTC_AMOUNT, 4_000, START + timedelta(hours=12)),
            StoredValue(CTC_AMOUNT, 5_000, datetime(2028, 1, 1, tzinfo=UTC)),
        ],
    )

    assert reform == {
        CTC_AMOUNT: {
            "2026-01-01.2027-12-31": 4_000,
            "2028-01-01.2200-12-31": 5_000,
        }
    }
