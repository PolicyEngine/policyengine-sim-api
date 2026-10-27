"""The export file of v1 report definitions that migration provisioning reads."""

from __future__ import annotations

from uuid import uuid4

import pytest
from migration_provisioning_support import (
    US_HOUSEHOLD_ID,
    US_REFORM_POLICY_ID,
    definitions_json,
    economy_report_definition,
    economy_simulation_definition,
    household_report_definition,
    household_simulation_definition,
    parse_definitions,
)
from policyengine_simulation_persistence import (
    NewReport,
    NewSimulation,
    ReportKind,
    SimulationKind,
)
from policyengine_simulation_persistence.migration_provisioning import (
    REPORT_DEFINITIONS_SCHEMA_VERSION,
    EconomyReportDefinition,
    HouseholdReportDefinition,
    ReportDefinitionsFile,
)
from pydantic import ValidationError


def _error_messages(error: pytest.ExceptionInfo[ValidationError]) -> str:
    return " | ".join(
        ".".join(str(part) for part in detail["loc"]) + ": " + detail["msg"]
        for detail in error.value.errors()
    )


def test_an_economy_report_becomes_two_simulations_and_a_report() -> None:
    definitions = parse_definitions(
        economy_report_definition(options={"target": "cliff"})
    )

    (report,) = definitions.reports
    assert isinstance(report, EconomyReportDefinition)
    assert [
        report.new_simulation(simulation) for simulation in report.simulations()
    ] == [
        NewSimulation(
            kind=SimulationKind.ECONOMY,
            country="us",
            region_code="state/ca",
            legacy_simulation_id=9001,
        ),
        NewSimulation(
            kind=SimulationKind.ECONOMY,
            country="us",
            policy_id=US_REFORM_POLICY_ID,
            region_code="state/ca",
            legacy_simulation_id=9002,
        ),
    ]
    baseline_id, reform_id = uuid4(), uuid4()
    assert report.new_report(baseline_id, reform_id) == NewReport(
        kind=ReportKind.ECONOMY,
        country="us",
        baseline_simulation_id=baseline_id,
        reform_simulation_id=reform_id,
        year=2026,
        options={"target": "cliff"},
        legacy_report_id=4512,
    )


def test_a_household_report_may_have_only_a_baseline() -> None:
    definitions = parse_definitions(household_report_definition())

    (report,) = definitions.reports
    assert isinstance(report, HouseholdReportDefinition)
    (baseline,) = report.simulations()
    assert report.new_simulation(baseline) == NewSimulation(
        kind=SimulationKind.HOUSEHOLD,
        country="us",
        household_id=US_HOUSEHOLD_ID,
        legacy_simulation_id=9003,
    )
    baseline_id = uuid4()
    assert report.new_report(baseline_id, None).reform_simulation_id is None


def test_a_household_comparison_has_two_policies_on_one_household() -> None:
    definitions = parse_definitions(
        household_report_definition(
            reform=household_simulation_definition(
                9004, policy_id=str(US_REFORM_POLICY_ID)
            )
        )
    )

    (report,) = definitions.reports
    assert [simulation.policy_id for simulation in report.simulations()] == [
        None,
        US_REFORM_POLICY_ID,
    ]


def test_a_single_simulation_economy_report_repeats_its_simulation() -> None:
    # v1 calculates an economy report with one simulation as that policy over
    # itself, so the export names the same simulation as baseline and reform.
    single = economy_simulation_definition(9001, policy_id=str(US_REFORM_POLICY_ID))

    definitions = parse_definitions(
        economy_report_definition(baseline=single, reform=single)
    )

    (report,) = definitions.reports
    assert [simulation.legacy_simulation_id for simulation in report.simulations()] == [
        9001,
        9001,
    ]


def test_the_schema_version_is_one() -> None:
    assert REPORT_DEFINITIONS_SCHEMA_VERSION == 1

    with pytest.raises(ValidationError) as error:
        ReportDefinitionsFile.model_validate_json(
            definitions_json(economy_report_definition(), schema_version=2)
        )

    assert _error_messages(error).startswith("schema_version:")


def test_an_unknown_report_kind_is_rejected() -> None:
    with pytest.raises(ValidationError) as error:
        parse_definitions(economy_report_definition(kind="budget_window"))

    assert "reports.0:" in _error_messages(error)


@pytest.mark.parametrize(
    ("report_definition", "location"),
    [
        (economy_report_definition(reform=None), "reports.0.economy.reform"),
        (
            economy_report_definition(
                baseline=economy_simulation_definition(
                    9001, household_id=str(US_HOUSEHOLD_ID)
                )
            ),
            "reports.0.economy.baseline.household_id",
        ),
        (
            household_report_definition(
                baseline=household_simulation_definition(9003, region_code="state/ca")
            ),
            "reports.0.household.baseline.region_code",
        ),
        (
            economy_report_definition(dynamic_id=str(uuid4())),
            "reports.0.economy.dynamic_id",
        ),
    ],
    ids=[
        "economy without a reform",
        "economy simulation with a household",
        "household simulation with a region",
        "a field the format does not have",
    ],
)
def test_a_report_must_have_the_fields_of_its_kind(
    report_definition: dict, location: str
) -> None:
    with pytest.raises(ValidationError) as error:
        parse_definitions(report_definition)

    assert _error_messages(error).startswith(location + ":")


@pytest.mark.parametrize("field_name", ["policy_id", "dataset_name"])
def test_nullable_simulation_fields_must_still_be_given(field_name: str) -> None:
    baseline = economy_simulation_definition(9001)
    del baseline[field_name]

    with pytest.raises(ValidationError) as error:
        parse_definitions(economy_report_definition(baseline=baseline))

    assert _error_messages(error) == (
        f"reports.0.economy.baseline.{field_name}: Field required"
    )


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"country": "USA"}, "country must be a lowercase two-letter code"),
        (
            {"baseline": economy_simulation_definition(9001, region_code="state ca")},
            "region_code must be 1 to 255 characters with no whitespace",
        ),
        (
            {"baseline": economy_simulation_definition(9001, dataset_name="")},
            "dataset_name must be 1 to 255 characters with no whitespace",
        ),
        ({"year": 1899}, "greater than or equal to 1900"),
        ({"year": 2201}, "less than or equal to 2200"),
        ({"legacy_report_id": 0}, "greater than 0"),
        (
            {"baseline": economy_simulation_definition(-3)},
            "greater than 0",
        ),
        ({"options": ["target"]}, "Input should be an object"),
    ],
    ids=[
        "country",
        "region code",
        "dataset name",
        "year too early",
        "year too late",
        "report ID",
        "simulation ID",
        "options",
    ],
)
def test_malformed_values_are_rejected(overrides: dict, message: str) -> None:
    with pytest.raises(ValidationError) as error:
        parse_definitions(economy_report_definition(**overrides))

    assert message in _error_messages(error)


@pytest.mark.parametrize(
    "overrides",
    [
        {"legacy_report_id": "4512"},
        {"legacy_report_id": True},
        {"year": "2026"},
        {"baseline": economy_simulation_definition(9001.0)},
    ],
    ids=["report ID as text", "report ID as a boolean", "year as text", "float ID"],
)
def test_numbers_are_not_converted(overrides: dict) -> None:
    with pytest.raises(ValidationError):
        parse_definitions(economy_report_definition(**overrides))


def test_an_economy_report_compares_policies_on_one_region_and_dataset() -> None:
    with pytest.raises(ValidationError) as error:
        parse_definitions(
            economy_report_definition(
                reform=economy_simulation_definition(
                    9002, policy_id=str(US_REFORM_POLICY_ID), region_code="state/ny"
                )
            )
        )

    assert "the same region_code and dataset_name" in _error_messages(error)


def test_a_household_report_compares_policies_on_one_household() -> None:
    with pytest.raises(ValidationError) as error:
        parse_definitions(
            household_report_definition(
                reform=household_simulation_definition(9004, household_id=str(uuid4()))
            )
        )

    assert "the same household_id" in _error_messages(error)


def test_each_v1_report_appears_once() -> None:
    with pytest.raises(ValidationError) as error:
        parse_definitions(
            economy_report_definition(4512),
            household_report_definition(4512),
            economy_report_definition(4600),
            economy_report_definition(4600),
        )

    assert "v1 reports appear more than once: 4512, 4600" in _error_messages(error)


def test_a_simulation_shared_by_reports_is_described_the_same_way() -> None:
    shared_baseline = economy_simulation_definition(9001)

    definitions = parse_definitions(
        economy_report_definition(4512, baseline=shared_baseline),
        economy_report_definition(4514, baseline=shared_baseline),
    )

    assert len(definitions.reports) == 2


def test_a_simulation_described_two_ways_is_rejected() -> None:
    with pytest.raises(ValidationError) as error:
        parse_definitions(
            economy_report_definition(4512),
            economy_report_definition(
                4514,
                baseline=economy_simulation_definition(9001, dataset_name="cps_2024"),
                reform=economy_simulation_definition(
                    9002,
                    policy_id=str(US_REFORM_POLICY_ID),
                    dataset_name="cps_2024",
                ),
            ),
        )

    assert (
        "v1 simulations are described differently in different reports: 9001, 9002"
        in _error_messages(error)
    )


def test_policy_and_household_ids_are_uuids() -> None:
    with pytest.raises(ValidationError) as error:
        parse_definitions(
            economy_report_definition(
                reform=economy_simulation_definition(9002, policy_id="17")
            )
        )

    assert _error_messages(error).startswith("reports.0.economy.reform.policy_id:")


@pytest.mark.parametrize(
    "overrides",
    [
        {"legacy_report_id": 2**63},
        {"baseline": economy_simulation_definition(2**63)},
    ],
    ids=["report ID", "simulation ID"],
)
def test_ids_beyond_the_database_s_bigint_range_are_rejected(overrides) -> None:
    with pytest.raises(ValidationError) as error:
        parse_definitions(economy_report_definition(**overrides))

    assert "less than or equal to 9223372036854775807" in _error_messages(error)


@pytest.mark.parametrize(
    "options",
    [
        {"limit": float("nan")},
        {"limits": [1, float("inf")]},
        {"nested": {"floor": float("-inf")}},
    ],
    ids=["NaN", "Infinity in a list", "-Infinity in an object"],
)
def test_options_hold_only_finite_numbers(options: dict) -> None:
    with pytest.raises(ValidationError) as error:
        parse_definitions(economy_report_definition(options=options))

    assert "options must not hold NaN or Infinity" in _error_messages(error)


@pytest.mark.parametrize(
    "overrides",
    [
        {
            "baseline": economy_simulation_definition(9001, region_code="state/\x00ca"),
            "reform": economy_simulation_definition(
                9002, policy_id=str(US_REFORM_POLICY_ID), region_code="state/\x00ca"
            ),
        },
        {
            "baseline": economy_simulation_definition(9001, dataset_name="cps\x00"),
            "reform": economy_simulation_definition(
                9002, policy_id=str(US_REFORM_POLICY_ID), dataset_name="cps\x00"
            ),
        },
        {"options": {"note": "a\x00b"}},
        {"options": {"a\x00b": 1}},
        {"options": {"notes": [{"text": "\x00"}]}},
    ],
    ids=[
        "region code",
        "dataset name",
        "option value",
        "option key",
        "nested option value",
    ],
)
def test_text_never_holds_a_nul_character(overrides: dict) -> None:
    with pytest.raises(ValidationError) as error:
        parse_definitions(economy_report_definition(**overrides))

    assert "must not contain a NUL character" in _error_messages(error)
