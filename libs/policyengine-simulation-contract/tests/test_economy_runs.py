"""The calls the Simulation Entrypoint starts for v2 economy runs."""

from __future__ import annotations

from uuid import uuid4

import pytest
from policyengine_simulation_contract.economy_runs import (
    ECONOMY_COMPARISON_FUNCTION,
    ECONOMY_SIMULATION_FUNCTIONS,
    CatalogPopulation,
    CatalogRowFilter,
    EconomyComparisonCall,
    EconomyComparisonSide,
    EconomySimulationCall,
    policy_affects_labor_supply,
)
from pydantic import ValidationError

OUTPUT_SHA256 = "a" * 64


def _state_filter(fips: str) -> CatalogRowFilter:
    return CatalogRowFilter(field="state_fips", value=fips)


def _call(**changes: object) -> EconomySimulationCall:
    values: dict[str, object] = {
        "simulation_run_id": uuid4(),
        "country": "us",
        "year": 2026,
        "policyengine_version": "6.7.5",
        "policy": {"gov.irs.credits.ctc.amount.base[0].amount": {"2026-01-01": 3000}},
        "population": CatalogPopulation(
            region_code="state/ca",
            dataset_name="populace_us_2024_acs_local",
            row_filter=_state_filter("6"),
            state_code="CA",
        ),
        "artifact_environment": "staging",
    }
    values.update(changes)
    return EconomySimulationCall.model_validate(values)


@pytest.mark.parametrize(
    ("stored", "household_value"),
    [
        ("6", 6),
        ("601", 601),
        ("WALES", "WALES"),
        ("E14001063", "E14001063"),
        ('["NORTH_EAST","LONDON"]', ["NORTH_EAST", "LONDON"]),
        ('["1","2"]', [1, 2]),
    ],
)
def test_a_catalog_filter_value_becomes_the_value_households_are_compared_with(
    stored: str, household_value: object
) -> None:
    row_filter = CatalogRowFilter(field="region", value=stored)

    assert row_filter.household_value() == household_value


@pytest.mark.parametrize("stored", ["[]", '["NORTH_EAST", 3]', "[1", '[""]'])
def test_a_malformed_catalog_filter_value_is_refused(stored: str) -> None:
    with pytest.raises(ValidationError):
        CatalogRowFilter(field="region", value=stored)


def test_a_call_round_trips_as_json() -> None:
    call = _call()

    assert EconomySimulationCall.model_validate_json(call.model_dump_json()) == call


def test_a_national_population_names_each_state_filter_and_none_of_its_own() -> None:
    population = CatalogPopulation(
        region_code="us",
        dataset_name="populace_us_2024",
        state_filters={"state/ca": _state_filter("6"), "state/ny": _state_filter("36")},
    )

    assert population.row_filter is None
    assert population.state_filters is not None
    assert population.state_filters["state/ny"].household_value() == 36


def test_a_population_cannot_have_a_filter_and_state_filters() -> None:
    with pytest.raises(ValidationError, match="national"):
        CatalogPopulation(
            region_code="state/ca",
            dataset_name="populace_us_2024",
            row_filter=_state_filter("6"),
            state_filters={"state/ca": _state_filter("6")},
        )


def test_state_filters_are_keyed_by_state_region_codes() -> None:
    with pytest.raises(ValidationError, match="state"):
        CatalogPopulation(
            region_code="us",
            dataset_name="populace_us_2024",
            state_filters={"congressional_district/CA-01": _state_filter("601")},
        )


def test_a_state_code_is_two_capital_letters() -> None:
    with pytest.raises(ValidationError):
        CatalogPopulation(
            region_code="state/ca",
            dataset_name="populace_us_2024",
            row_filter=_state_filter("6"),
            state_code="california",
        )


def test_a_call_refuses_a_policy_value_that_is_not_keyed_by_period() -> None:
    with pytest.raises(ValidationError):
        _call(policy={"gov.irs.credits.ctc.amount.base[0].amount": 3000})


def test_a_call_refuses_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        EconomySimulationCall.model_validate(
            {**_call().model_dump(mode="json"), "region": "state/ca"}
        )


def test_each_country_has_its_simulation_function() -> None:
    assert ECONOMY_SIMULATION_FUNCTIONS == {
        "us": "run_economy_simulation_us",
        "uk": "run_economy_simulation_uk",
    }
    assert ECONOMY_COMPARISON_FUNCTION == "compare_economy_simulations"


@pytest.mark.parametrize(
    ("country", "parameter", "affects"),
    [
        ("us", "gov.simulation.labor_supply_responses.elasticities.income", True),
        (
            "us",
            "gov.simulation.labor_supply_responses.elasticities.substitution.all",
            True,
        ),
        ("us", "gov.simulation.labor_supply_responses.elasticities.incomes", False),
        ("us", "gov.irs.credits.ctc.amount.base[0].amount", False),
        ("uk", "gov.simulation.labour_supply_responses.income_elasticity", True),
        ("uk", "gov.simulation.labor_supply_responses.substitution_elasticity", True),
        ("uk", "gov.simulation.labor_supply_responses.elasticities.income", False),
    ],
)
def test_a_policy_affects_labour_supply_when_it_changes_an_elasticity(
    country: str, parameter: str, *, affects: bool
) -> None:
    policy = {parameter: {"2026-01-01": 0.1}}

    assert policy_affects_labor_supply(country, policy) is affects


def test_a_comparison_names_both_sides_outputs() -> None:
    side = EconomyComparisonSide(
        simulation_run_id=uuid4(),
        policy={},
        output_uri="gs://bucket/economy-simulation-runs/staging/run.parquet",
        output_sha256=OUTPUT_SHA256,
    )

    call = EconomyComparisonCall(
        report_run_id=uuid4(),
        country="uk",
        year=2026,
        policyengine_version="6.7.5",
        region_code="country/wales",
        dataset_name="enhanced_frs_2023_24",
        baseline=side,
        reform=side,
    )

    assert EconomyComparisonCall.model_validate_json(call.model_dump_json()) == call


def test_a_comparison_side_names_its_output_by_a_cloud_storage_uri() -> None:
    with pytest.raises(ValidationError):
        EconomyComparisonSide(
            simulation_run_id=uuid4(),
            policy={},
            output_uri="https://example.org/run.parquet",
            output_sha256=OUTPUT_SHA256,
        )


def test_only_a_national_region_runs_without_a_household_filter() -> None:
    CatalogPopulation(region_code="uk", dataset_name="enhanced_frs_2023_24")

    with pytest.raises(ValidationError, match="household filter"):
        CatalogPopulation(region_code="place/CA-44000", dataset_name="populace_us_2024")
