"""Reading the Public API's household, policy and catalog rows a household run needs."""

from __future__ import annotations

import json
from dataclasses import dataclass
from uuid import UUID, uuid4

import pytest
from policyengine_simulation_persistence import (
    HouseholdInputReader,
    PolicyParametersMissingError,
    UnknownCatalogVersionError,
    UnknownHouseholdError,
    UnknownPolicyError,
    create_simulation_engine,
)
from public_api_support import superuser_engine
from sqlalchemy import create_engine, text

pytestmark = pytest.mark.postgres

CTC_AMOUNT = "gov.irs.credits.ctc.amount.base[0].amount"
STANDARD_DEDUCTION = "gov.irs.deductions.standard.amount.SINGLE"
RETIRED_PARAMETER = "gov.irs.credits.retired_credit.amount"
US_DOCUMENT = {
    "people": [
        {
            "id": "person-1",
            "values": {"age": {"2025": 40}},
            "memberships": {"household": "household-1"},
        }
    ],
    "household": [{"id": "household-1", "values": {"state_name": {"2025": "CA"}}}],
    "spm": {"scenario": "ce_trend"},
}


@dataclass(frozen=True)
class HouseholdRows:
    """The identifiers of the rows each test reads."""

    older_version_id: UUID
    version_id: UUID
    uk_version_id: UUID
    household_id: UUID
    uk_household_id: UUID
    policy_id: UUID
    retired_policy_id: UUID
    empty_policy_id: UUID


def _insert_rows(superuser_url: str) -> HouseholdRows:
    rows = HouseholdRows(
        older_version_id=uuid4(),
        version_id=uuid4(),
        uk_version_id=uuid4(),
        household_id=uuid4(),
        uk_household_id=uuid4(),
        policy_id=uuid4(),
        retired_policy_id=uuid4(),
        empty_policy_id=uuid4(),
    )
    us_model, uk_model = uuid4(), uuid4()
    older_parameters = {name: uuid4() for name in (CTC_AMOUNT, RETIRED_PARAMETER)}
    statements: list[tuple[str, dict[str, object]]] = [
        (
            "INSERT INTO public.tax_benefit_models (id, name) VALUES "
            "(:us, 'policyengine-us'), (:uk, 'policyengine-uk')",
            {"us": us_model, "uk": uk_model},
        ),
        (
            "INSERT INTO public.tax_benefit_model_versions (id, model_id, version) "
            "VALUES (:older, :us, '6.5.0'), (:version, :us, '6.7.5'), "
            "(:uk_version, :uk, '6.7.5')",
            {
                "older": rows.older_version_id,
                "version": rows.version_id,
                "uk_version": rows.uk_version_id,
                "us": us_model,
                "uk": uk_model,
            },
        ),
        *(
            (
                "INSERT INTO public.parameters (id, name, "
                "tax_benefit_model_version_id) VALUES (:id, :name, :version)",
                {"id": parameter_id, "name": name, "version": rows.older_version_id},
            )
            for name, parameter_id in older_parameters.items()
        ),
        *(
            (
                "INSERT INTO public.parameters (id, name, "
                "tax_benefit_model_version_id) VALUES (gen_random_uuid(), :name, "
                ":version)",
                {"name": name, "version": rows.version_id},
            )
            for name in (CTC_AMOUNT, STANDARD_DEDUCTION)
        ),
        (
            "INSERT INTO public.policies (id, country_id) VALUES "
            "(:policy, 'us'), (:retired, 'us'), (:empty, 'us')",
            {
                "policy": rows.policy_id,
                "retired": rows.retired_policy_id,
                "empty": rows.empty_policy_id,
            },
        ),
        (
            "INSERT INTO public.parameter_values (id, parameter_id, value_json, "
            "start_date, end_date, policy_id) VALUES "
            "(gen_random_uuid(), :ctc, '3000', '2026-01-01T00:00:00Z', NULL, "
            ":policy), "
            "(gen_random_uuid(), :ctc, '2500', '2025-01-01T00:00:00Z', "
            "'2025-12-31T00:00:00Z', :policy), "
            "(gen_random_uuid(), :retired_parameter, 'true', "
            "'2026-01-01T00:00:00Z', NULL, :retired)",
            {
                "ctc": older_parameters[CTC_AMOUNT],
                "retired_parameter": older_parameters[RETIRED_PARAMETER],
                "policy": rows.policy_id,
                "retired": rows.retired_policy_id,
            },
        ),
        (
            "INSERT INTO public.households (id, country_id, default_year, "
            "household_data) VALUES (:household, 'us', 2025, :document), "
            "(:uk_household, 'uk', NULL, :uk_document)",
            {
                "household": rows.household_id,
                "uk_household": rows.uk_household_id,
                "document": json.dumps(US_DOCUMENT),
                "uk_document": json.dumps({"people": [], "benunit": []}),
            },
        ),
    ]
    engine = superuser_engine(superuser_url)
    try:
        with engine.begin() as connection:
            for statement, parameters in statements:
                connection.execute(text(statement), parameters)
    finally:
        engine.dispose()
    return rows


@pytest.fixture
def household_rows(migrated_database) -> HouseholdRows:
    """Insert the catalog, policy and household rows the tests read."""
    return _insert_rows(migrated_database.superuser_url)


@pytest.fixture
def reader(migrated_database) -> HouseholdInputReader:
    """Return a reader that connects as the runtime role."""
    return HouseholdInputReader(create_simulation_engine(migrated_database.runtime_url))


def test_a_catalog_version_is_found_by_country_and_policyengine_version(
    reader, household_rows
) -> None:
    version = reader.catalog_version("us", "6.7.5")

    assert version.id == household_rows.version_id
    assert version.country == "us"
    assert version.policyengine_version == "6.7.5"
    assert reader.catalog_version("uk", "6.7.5").id == household_rows.uk_version_id


@pytest.mark.parametrize(
    ("country", "policyengine_version"), [("us", "9.9.9"), ("uk", "6.5.0")]
)
def test_an_unpublished_catalog_version_is_unknown(
    reader, household_rows, country, policyengine_version
) -> None:
    with pytest.raises(UnknownCatalogVersionError, match=policyengine_version):
        reader.catalog_version(country, policyengine_version)


def test_a_household_is_read_with_its_document_and_default_year(
    reader, household_rows
) -> None:
    household = reader.household("us", household_rows.household_id)

    assert household.id == household_rows.household_id
    assert household.country == "us"
    assert household.default_year == 2025
    assert household.document == US_DOCUMENT
    assert reader.household("uk", household_rows.uk_household_id).default_year is None


@pytest.mark.parametrize("which", ["another-country", "missing"])
def test_a_household_of_another_country_or_none_is_unknown(
    reader, household_rows, which
) -> None:
    household_id = (
        household_rows.uk_household_id if which == "another-country" else uuid4()
    )

    with pytest.raises(UnknownHouseholdError, match=str(household_id)):
        reader.household("us", household_id)


def test_a_policys_values_are_read_by_name_on_the_runs_version(
    reader, household_rows
) -> None:
    # The policy was saved on 6.5.0's parameters; 6.7.5 has the same name.
    parameters = reader.policy_parameters(
        "us", household_rows.policy_id, household_rows.version_id
    )

    assert parameters == {
        CTC_AMOUNT: {
            "2025-01-01.2025-12-31": 2500,
            "2026-01-01.2100-12-31": 3000,
        }
    }


def test_a_policys_periods_are_read_in_utc_whatever_the_session_time_zone(
    migrated_database, household_rows
) -> None:
    engine = create_engine(
        migrated_database.runtime_url.replace(
            "postgresql://", "postgresql+psycopg://", 1
        ),
        connect_args={"options": "-c timezone=Pacific/Honolulu"},
    )
    try:
        parameters = HouseholdInputReader(engine).policy_parameters(
            "us", household_rows.policy_id, household_rows.version_id
        )
    finally:
        engine.dispose()

    assert set(parameters[CTC_AMOUNT]) == {
        "2025-01-01.2025-12-31",
        "2026-01-01.2100-12-31",
    }


def test_a_policy_changing_no_parameter_changes_nothing(reader, household_rows) -> None:
    assert (
        reader.policy_parameters(
            "us", household_rows.empty_policy_id, household_rows.version_id
        )
        == {}
    )


def test_a_parameter_the_runs_version_lacks_is_refused_by_name(
    reader, household_rows
) -> None:
    with pytest.raises(PolicyParametersMissingError, match=RETIRED_PARAMETER) as error:
        reader.policy_parameters(
            "us", household_rows.retired_policy_id, household_rows.version_id
        )

    assert error.value.names == (RETIRED_PARAMETER,)


@pytest.mark.parametrize("country", ["uk", "us"])
def test_a_policy_of_another_country_or_none_is_unknown(
    reader, household_rows, country
) -> None:
    policy_id = household_rows.policy_id if country == "uk" else uuid4()

    with pytest.raises(UnknownPolicyError, match=str(policy_id)):
        reader.policy_parameters(country, policy_id, household_rows.version_id)
