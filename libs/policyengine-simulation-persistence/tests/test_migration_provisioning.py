"""Migration provisioning creates migrated simulations and reports, once each."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from uuid import uuid4

import pytest
from migration_provisioning_support import (
    economy_report_for_rows,
    economy_simulation_definition,
    household_report_for_rows,
    parse_definitions,
)
from policyengine_simulation_persistence import (
    ReportKind,
    ReportStore,
    SimulationStore,
)
from policyengine_simulation_persistence.migration_provisioning import (
    ProvisionedReport,
    ProvisioningFailure,
    provision_reports,
)
from public_api_support import superuser_engine
from report_support import report_store
from simulation_support import economy_simulation, simulation_store
from sqlalchemy import text
from sqlalchemy.exc import DataError

if TYPE_CHECKING:
    from conftest import DisposableDatabase
    from policyengine_simulation_persistence.migration_provisioning import (
        ProvisioningResult,
    )

pytestmark = pytest.mark.postgres


def _provision(
    database: DisposableDatabase,
    *report_definitions: dict[str, Any],
    dry_run: bool = False,
) -> ProvisioningResult:
    return provision_reports(
        parse_definitions(*report_definitions),
        simulation_store(database),
        report_store(database),
        dry_run=dry_run,
    )


def _row_counts(database: DisposableDatabase) -> tuple[int, int]:
    engine = superuser_engine(database.superuser_url)
    try:
        with engine.connect() as connection:
            return (
                connection.execute(
                    text("SELECT count(*) FROM simulation.simulations")
                ).scalar_one(),
                connection.execute(
                    text("SELECT count(*) FROM simulation.reports")
                ).scalar_one(),
            )
    finally:
        engine.dispose()


def test_each_report_gets_its_simulations_and_then_the_report(
    migrated_database, public_api_rows
) -> None:
    result = _provision(
        migrated_database,
        economy_report_for_rows(public_api_rows, options={"target": "cliff"}),
        household_report_for_rows(public_api_rows),
    )

    simulations = simulation_store(migrated_database)
    reports = report_store(migrated_database)
    baseline = simulations.get_by_legacy_simulation_id(9001)
    reform = simulations.get_by_legacy_simulation_id(9002)
    assert baseline is not None
    assert reform is not None
    assert (baseline.policy_id, baseline.region_code, baseline.dataset_name) == (
        None,
        "state/ca",
        None,
    )
    assert reform.policy_id == public_api_rows.us_policy_id
    economy = reports.get_by_legacy_report_id(4512)
    assert economy is not None
    assert economy.kind is ReportKind.ECONOMY
    assert (economy.baseline_simulation_id, economy.reform_simulation_id) == (
        baseline.id,
        reform.id,
    )
    assert (economy.year, economy.options) == (2026, {"target": "cliff"})
    household = reports.get_by_legacy_report_id(4513)
    assert household is not None
    assert household.kind is ReportKind.HOUSEHOLD
    assert result.reports == [
        ProvisionedReport(legacy_report_id=4512, report_id=economy.id, created=True),
        ProvisionedReport(legacy_report_id=4513, report_id=household.id, created=True),
    ]
    assert result.failures == []
    assert (result.new_simulations, result.existing_simulations) == (4, 0)
    assert (result.new_reports, result.existing_reports) == (2, 0)
    assert result.dry_run is False


def test_running_again_creates_nothing_and_maps_the_same_reports(
    migrated_database, public_api_rows
) -> None:
    definitions = (
        economy_report_for_rows(public_api_rows),
        household_report_for_rows(public_api_rows),
    )
    first = _provision(migrated_database, *definitions)
    counts_after_first_run = _row_counts(migrated_database)

    second = _provision(migrated_database, *definitions)

    assert _row_counts(migrated_database) == counts_after_first_run == (4, 2)
    assert [mapped.report_id for mapped in second.reports] == [
        mapped.report_id for mapped in first.reports
    ]
    assert [mapped.created for mapped in second.reports] == [False, False]
    assert (second.new_simulations, second.existing_simulations) == (0, 4)
    assert (second.new_reports, second.existing_reports) == (0, 2)
    assert second.failures == []


def test_a_simulation_several_reports_use_is_created_once(
    migrated_database, public_api_rows
) -> None:
    result = _provision(
        migrated_database,
        economy_report_for_rows(public_api_rows, 4512),
        economy_report_for_rows(
            public_api_rows,
            4514,
            reform=economy_simulation_definition(
                9005, policy_id=str(public_api_rows.us_policy_id), dataset_name="cps"
            ),
            baseline=economy_simulation_definition(9006, dataset_name="cps"),
        ),
        economy_report_for_rows(public_api_rows, 4515),
    )

    reports = report_store(migrated_database)
    first = reports.get_by_legacy_report_id(4512)
    third = reports.get_by_legacy_report_id(4515)
    assert first is not None
    assert third is not None
    assert first.baseline_simulation_id == third.baseline_simulation_id
    assert first.reform_simulation_id == third.reform_simulation_id
    assert (result.new_simulations, result.new_reports) == (4, 3)
    assert _row_counts(migrated_database) == (4, 3)


def test_a_single_simulation_economy_report_compares_it_with_itself(
    migrated_database, public_api_rows
) -> None:
    single = economy_simulation_definition(
        9001, policy_id=str(public_api_rows.us_policy_id)
    )

    result = _provision(
        migrated_database,
        economy_report_for_rows(public_api_rows, baseline=single, reform=single),
    )

    report = report_store(migrated_database).get_by_legacy_report_id(4512)
    assert report is not None
    assert report.baseline_simulation_id == report.reform_simulation_id
    assert (result.new_simulations, result.new_reports) == (1, 1)


def test_a_simulation_already_migrated_differently_fails_its_report_only(
    migrated_database, public_api_rows
) -> None:
    simulation_store(migrated_database).create_migrated(
        economy_simulation(
            public_api_rows,
            policy_id=None,
            region_code="state/ny",
            legacy_simulation_id=9001,
        )
    )

    result = _provision(
        migrated_database,
        economy_report_for_rows(public_api_rows),
        household_report_for_rows(public_api_rows),
    )

    assert result.failures == [
        ProvisioningFailure(
            legacy_report_id=4512,
            reason=(
                "v1 simulation 9001 is already migrated with a different region_code"
            ),
        )
    ]
    assert report_store(migrated_database).get_by_legacy_report_id(4512) is None
    assert [mapped.legacy_report_id for mapped in result.reports] == [4513]
    assert (result.new_simulations, result.existing_simulations) == (2, 0)


def test_a_report_already_migrated_differently_fails(
    migrated_database, public_api_rows
) -> None:
    _provision(migrated_database, economy_report_for_rows(public_api_rows))

    result = _provision(
        migrated_database, economy_report_for_rows(public_api_rows, year=2027)
    )

    assert result.failures == [
        ProvisioningFailure(
            legacy_report_id=4512,
            reason="v1 report 4512 is already migrated with a different year",
        )
    ]
    assert result.reports == []
    assert (result.existing_simulations, result.existing_reports) == (2, 0)


@pytest.mark.parametrize(
    ("reform_policy", "constraint"),
    [
        ("missing", "fk_simulations_policy_country"),
        ("uk", "fk_simulations_policy_country"),
    ],
    ids=["a policy the Public API does not have", "a policy of another country"],
)
def test_a_definition_the_database_rejects_fails_and_the_rest_continue(
    migrated_database, public_api_rows, reform_policy, constraint
) -> None:
    policy_id = uuid4() if reform_policy == "missing" else public_api_rows.uk_policy_id

    result = _provision(
        migrated_database,
        economy_report_for_rows(
            public_api_rows,
            reform=economy_simulation_definition(9002, policy_id=str(policy_id)),
        ),
        household_report_for_rows(public_api_rows),
    )

    (failure,) = result.failures
    assert failure.legacy_report_id == 4512
    assert failure.reason.startswith("the database rejected v1 simulation 9002: ")
    assert constraint in failure.reason
    assert "\n" not in failure.reason
    assert [mapped.legacy_report_id for mapped in result.reports] == [4513]
    # The baseline was created before the reform failed; a later run reuses it.
    assert (result.new_simulations, result.new_reports) == (3, 1)


def test_a_dry_run_writes_nothing_and_counts_what_a_run_would_create(
    migrated_database, public_api_rows
) -> None:
    result = _provision(
        migrated_database,
        economy_report_for_rows(public_api_rows, 4512),
        economy_report_for_rows(public_api_rows, 4515),
        household_report_for_rows(public_api_rows),
        dry_run=True,
    )

    assert _row_counts(migrated_database) == (0, 0)
    assert result.dry_run is True
    assert result.reports == []
    assert result.failures == []
    assert (result.new_simulations, result.existing_simulations) == (4, 0)
    assert (result.new_reports, result.existing_reports) == (3, 0)


def test_a_dry_run_after_a_run_maps_every_existing_report(
    migrated_database, public_api_rows
) -> None:
    definitions = (
        economy_report_for_rows(public_api_rows),
        household_report_for_rows(public_api_rows),
    )
    first = _provision(migrated_database, *definitions)

    dry_run = _provision(migrated_database, *definitions, dry_run=True)

    assert dry_run.reports == [
        ProvisionedReport(
            legacy_report_id=mapped.legacy_report_id,
            report_id=mapped.report_id,
            created=False,
        )
        for mapped in first.reports
    ]
    assert (dry_run.new_simulations, dry_run.existing_simulations) == (0, 4)
    assert (dry_run.new_reports, dry_run.existing_reports) == (0, 2)


def test_a_dry_run_counts_a_report_whose_simulations_all_exist(
    migrated_database, public_api_rows
) -> None:
    _provision(migrated_database, economy_report_for_rows(public_api_rows, 4512))

    result = _provision(
        migrated_database, economy_report_for_rows(public_api_rows, 4515), dry_run=True
    )

    assert (result.new_simulations, result.existing_simulations) == (0, 2)
    assert (result.new_reports, result.existing_reports) == (1, 0)
    assert _row_counts(migrated_database) == (2, 1)


def test_a_dry_run_reports_rows_already_migrated_differently(
    migrated_database, public_api_rows
) -> None:
    _provision(migrated_database, economy_report_for_rows(public_api_rows))
    simulation_store(migrated_database).create_migrated(
        economy_simulation(
            public_api_rows,
            policy_id=None,
            region_code="state/ny",
            legacy_simulation_id=9003,
        )
    )

    result = _provision(
        migrated_database,
        economy_report_for_rows(public_api_rows, year=2027),
        household_report_for_rows(public_api_rows),
        dry_run=True,
    )

    assert result.failures == [
        ProvisioningFailure(
            legacy_report_id=4512,
            reason="v1 report 4512 is already migrated with a different year",
        ),
        ProvisioningFailure(
            legacy_report_id=4513,
            reason=(
                "v1 simulation 9003 is already migrated with a different kind, "
                "household_id, region_code"
            ),
        ),
    ]
    assert _row_counts(migrated_database) == (3, 1)


def test_a_dry_run_reports_a_report_migrated_with_other_simulations(
    migrated_database, public_api_rows
) -> None:
    _provision(migrated_database, economy_report_for_rows(public_api_rows))

    result = _provision(
        migrated_database,
        economy_report_for_rows(
            public_api_rows,
            baseline=economy_simulation_definition(9101),
            reform=economy_simulation_definition(
                9102, policy_id=str(public_api_rows.us_policy_id)
            ),
        ),
        dry_run=True,
    )

    assert result.failures == [
        ProvisioningFailure(
            legacy_report_id=4512,
            reason=(
                "v1 report 4512 is already migrated, "
                "with simulations this run would create"
            ),
        )
    ]
    # A run creates nothing for a report that conflicts, so neither does this.
    assert (result.new_simulations, result.new_reports) == (0, 0)


@pytest.mark.parametrize(
    ("store_type", "subject"),
    [(SimulationStore, "v1 simulation 9001"), (ReportStore, "v1 report 4512")],
    ids=["a simulation", "a report"],
)
def test_a_value_the_database_cannot_store_fails_its_report_only(
    migrated_database, public_api_rows, monkeypatch, store_type, subject
) -> None:
    # The file check rejects the values known to be unstorable; any other
    # value the database rejects as data fails its report in the same way.
    create_migrated = store_type.create_migrated

    def reject_the_first_report(store, new_row):
        if getattr(new_row, "legacy_simulation_id", None) == 9001 or (
            getattr(new_row, "legacy_report_id", None) == 4512
        ):
            statement = "INSERT"
            raise DataError(
                statement, {}, ValueError("value out of range\nDETAIL: more")
            )
        return create_migrated(store, new_row)

    monkeypatch.setattr(store_type, "create_migrated", reject_the_first_report)

    result = _provision(
        migrated_database,
        economy_report_for_rows(public_api_rows),
        household_report_for_rows(public_api_rows),
    )

    assert result.failures == [
        ProvisioningFailure(
            legacy_report_id=4512,
            reason=f"the database rejected {subject}: value out of range",
        )
    ]
    assert [mapped.legacy_report_id for mapped in result.reports] == [4513]


def test_options_compare_as_the_json_numbers_the_database_stores(
    migrated_database, public_api_rows
) -> None:
    # JSONB keeps numbers as exact decimals, so 1e23 reads back as the integer
    # 100000000000000000000000, which is not the float 1e23.
    options = {
        "threshold": 1e23,
        "rate": 0.1,
        "nested": {"limits": [1.2345678901234567e19, 1e300, 5e-324, -0.0, 7]},
        "flag": True,
    }
    definition = economy_report_for_rows(public_api_rows, options=options)

    first = _provision(migrated_database, definition)
    second = _provision(migrated_database, definition)

    assert first.failures == []
    assert second.failures == []
    assert [mapped.created for mapped in first.reports + second.reports] == [
        True,
        False,
    ]


def test_a_later_run_reuses_the_simulations_a_failed_report_left(
    migrated_database, public_api_rows
) -> None:
    missing_policy = economy_simulation_definition(9002, policy_id=str(uuid4()))
    first = _provision(
        migrated_database,
        economy_report_for_rows(public_api_rows, reform=missing_policy),
    )
    baseline = simulation_store(migrated_database).get_by_legacy_simulation_id(9001)

    # The export is corrected and the command runs again.
    second = _provision(migrated_database, economy_report_for_rows(public_api_rows))

    report = report_store(migrated_database).get_by_legacy_report_id(4512)
    assert [failure.legacy_report_id for failure in first.failures] == [4512]
    assert baseline is not None
    assert report is not None
    assert report.baseline_simulation_id == baseline.id
    assert second.failures == []
    assert (second.new_simulations, second.existing_simulations) == (1, 1)
    assert (second.new_reports, second.existing_reports) == (1, 0)


def test_a_report_that_conflicts_with_an_existing_row_creates_nothing(
    migrated_database, public_api_rows
) -> None:
    # The runtime role cannot delete a row, so a run checks every existing row
    # a report needs before it creates any of them.
    simulation_store(migrated_database).create_migrated(
        economy_simulation(
            public_api_rows, region_code="state/ny", legacy_simulation_id=9002
        )
    )

    result = _provision(migrated_database, economy_report_for_rows(public_api_rows))

    assert result.failures == [
        ProvisioningFailure(
            legacy_report_id=4512,
            reason=(
                "v1 simulation 9002 is already migrated with a different region_code"
            ),
        )
    ]
    assert simulation_store(migrated_database).get_by_legacy_simulation_id(9001) is None
    assert _row_counts(migrated_database) == (1, 0)
    assert (result.new_simulations, result.existing_simulations) == (0, 0)


def test_a_run_creates_no_simulation_for_a_report_migrated_with_others(
    migrated_database, public_api_rows
) -> None:
    _provision(migrated_database, economy_report_for_rows(public_api_rows))

    result = _provision(
        migrated_database,
        economy_report_for_rows(
            public_api_rows,
            baseline=economy_simulation_definition(9101),
            reform=economy_simulation_definition(
                9102, policy_id=str(public_api_rows.us_policy_id)
            ),
        ),
    )

    assert result.failures == [
        ProvisioningFailure(
            legacy_report_id=4512,
            reason=(
                "v1 report 4512 is already migrated, "
                "with simulations this run would create"
            ),
        )
    ]
    assert _row_counts(migrated_database) == (2, 1)
