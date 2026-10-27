"""Migration provisioning creates migrated simulations and reports, once each."""

from __future__ import annotations

from uuid import uuid4

import pytest
from migration_provisioning_support import (
    economy_report_definition,
    economy_simulation_definition,
    household_report_definition,
    household_simulation_definition,
    parse_definitions,
)
from policyengine_simulation_persistence import ReportKind
from policyengine_simulation_persistence.migration_provisioning import (
    ProvisionedReport,
    ProvisioningFailure,
    provision_reports,
)
from public_api_support import superuser_engine
from report_support import report_store
from simulation_support import economy_simulation, simulation_store
from sqlalchemy import text

pytestmark = pytest.mark.postgres


def _economy_report(rows, legacy_report_id=4512, **overrides):
    """Return an economy report of the US reform in California."""
    values = {
        "reform": economy_simulation_definition(9002, policy_id=str(rows.us_policy_id))
    }
    values.update(overrides)
    return economy_report_definition(legacy_report_id, **values)


def _household_report(rows, legacy_report_id=4513, **overrides):
    """Return a report comparing the US reform with current law for the household."""
    household_id = str(rows.us_household_id)
    values = {
        "baseline": household_simulation_definition(9003, household_id=household_id),
        "reform": household_simulation_definition(
            9004, household_id=household_id, policy_id=str(rows.us_policy_id)
        ),
    }
    values.update(overrides)
    return household_report_definition(legacy_report_id, **values)


def _provision(database, *report_definitions, dry_run=False):
    return provision_reports(
        parse_definitions(*report_definitions),
        simulation_store(database),
        report_store(database),
        dry_run=dry_run,
    )


def _row_counts(database) -> tuple[int, int]:
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
        _economy_report(public_api_rows, options={"target": "cliff"}),
        _household_report(public_api_rows),
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
    definitions = (_economy_report(public_api_rows), _household_report(public_api_rows))
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
        _economy_report(public_api_rows, 4512),
        _economy_report(
            public_api_rows,
            4514,
            reform=economy_simulation_definition(
                9005, policy_id=str(public_api_rows.us_policy_id), dataset_name="cps"
            ),
            baseline=economy_simulation_definition(9006, dataset_name="cps"),
        ),
        _economy_report(public_api_rows, 4515),
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
        _economy_report(public_api_rows, baseline=single, reform=single),
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
        _economy_report(public_api_rows),
        _household_report(public_api_rows),
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
    _provision(migrated_database, _economy_report(public_api_rows))

    result = _provision(migrated_database, _economy_report(public_api_rows, year=2027))

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
        _economy_report(
            public_api_rows,
            reform=economy_simulation_definition(9002, policy_id=str(policy_id)),
        ),
        _household_report(public_api_rows),
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
        _economy_report(public_api_rows, 4512),
        _economy_report(public_api_rows, 4515),
        _household_report(public_api_rows),
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
    definitions = (_economy_report(public_api_rows), _household_report(public_api_rows))
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
    _provision(migrated_database, _economy_report(public_api_rows, 4512))

    result = _provision(
        migrated_database, _economy_report(public_api_rows, 4515), dry_run=True
    )

    assert (result.new_simulations, result.existing_simulations) == (0, 2)
    assert (result.new_reports, result.existing_reports) == (1, 0)
    assert _row_counts(migrated_database) == (2, 1)


def test_a_dry_run_reports_rows_already_migrated_differently(
    migrated_database, public_api_rows
) -> None:
    _provision(migrated_database, _economy_report(public_api_rows))
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
        _economy_report(public_api_rows, year=2027),
        _household_report(public_api_rows),
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
    _provision(migrated_database, _economy_report(public_api_rows))

    result = _provision(
        migrated_database,
        _economy_report(
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
    assert (result.new_simulations, result.new_reports) == (2, 0)
