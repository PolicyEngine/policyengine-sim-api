"""simulation.reports: which simulations a report compares, its year and options."""

from __future__ import annotations

from uuid import uuid4

import pytest
from policyengine_simulation_persistence import (
    ReportKind,
    ReportRecord,
    create_simulation_engine,
)
from policyengine_simulation_persistence.reports import reports
from public_api_support import superuser_engine
from report_support import (
    economy_pair,
    economy_report,
    household_baseline,
    report_store,
)
from simulation_support import economy_simulation, simulation_store
from sqlalchemy import insert, text
from sqlalchemy.exc import IntegrityError

pytestmark = pytest.mark.postgres


def test_an_economy_report_compares_a_baseline_and_a_reform(
    migrated_database, public_api_rows
) -> None:
    baseline, reform = economy_pair(migrated_database, public_api_rows)
    store = report_store(migrated_database)

    created = store.create(
        economy_report(baseline, reform, options={"target": "cliffs"})
    )

    assert isinstance(created, ReportRecord)
    assert created.kind is ReportKind.ECONOMY
    assert created.baseline_simulation_id == baseline.id
    assert created.reform_simulation_id == reform.id
    assert created.year == 2026
    assert created.options == {"target": "cliffs"}
    assert created.legacy_report_id is None
    assert store.get(created.id) == created


def test_a_household_report_may_have_only_a_baseline(
    migrated_database, public_api_rows
) -> None:
    baseline = household_baseline(migrated_database, public_api_rows)

    created = report_store(migrated_database).create(
        economy_report(
            baseline,
            baseline,
            kind=ReportKind.HOUSEHOLD,
            reform_simulation_id=None,
        )
    )

    assert created.kind is ReportKind.HOUSEHOLD
    assert created.reform_simulation_id is None
    assert created.options == {}


def test_an_economy_report_needs_a_reform(migrated_database, public_api_rows) -> None:
    baseline, reform = economy_pair(migrated_database, public_api_rows)

    with pytest.raises(IntegrityError, match="economy_has_reform"):
        report_store(migrated_database).create(
            economy_report(baseline, reform, reform_simulation_id=None)
        )


def test_simulations_must_have_the_report_country(
    migrated_database, public_api_rows
) -> None:
    baseline, _ = economy_pair(migrated_database, public_api_rows)
    uk_reform = simulation_store(migrated_database).create(
        economy_simulation(
            public_api_rows,
            country="uk",
            policy_id=public_api_rows.uk_policy_id,
            region_code="country/wales",
        )
    )

    with pytest.raises(IntegrityError, match="fk_reports_reform_simulation"):
        report_store(migrated_database).create(economy_report(baseline, uk_reform))


def test_simulations_must_have_the_report_kind(
    migrated_database, public_api_rows
) -> None:
    baseline, reform = economy_pair(migrated_database, public_api_rows)

    with pytest.raises(IntegrityError, match="fk_reports_baseline_simulation"):
        report_store(migrated_database).create(
            economy_report(
                baseline,
                reform,
                kind=ReportKind.HOUSEHOLD,
                reform_simulation_id=None,
            )
        )


def test_an_unknown_simulation_is_rejected(migrated_database, public_api_rows) -> None:
    baseline, reform = economy_pair(migrated_database, public_api_rows)

    with pytest.raises(IntegrityError, match="fk_reports_reform_simulation"):
        report_store(migrated_database).create(
            economy_report(baseline, reform, reform_simulation_id=uuid4())
        )


@pytest.mark.parametrize(
    ("column", "value", "constraint"),
    [
        ("year", "1899", "ck_reports_year"),
        ("options", "'[]'::jsonb", "ck_reports_options"),
    ],
)
def test_the_database_checks_the_year_and_options(
    migrated_database, public_api_rows, column, value, constraint
) -> None:
    """The database checks what NewReport also checks, for any other writer."""
    baseline, reform = economy_pair(migrated_database, public_api_rows)
    values = {"year": "2026", "options": "'{}'::jsonb", column: value}
    engine = superuser_engine(migrated_database.superuser_url)
    try:
        with (
            pytest.raises(IntegrityError, match=constraint),
            engine.begin() as connection,
        ):
            connection.execute(
                text(
                    "INSERT INTO simulation.reports (id, kind, country, "  # noqa: S608  # fixed test values
                    "baseline_simulation_id, reform_simulation_id, year, options) "
                    f"VALUES (:id, 'economy', 'us', :baseline, :reform, "
                    f"{values['year']}, {values['options']})"
                ),
                {"id": uuid4(), "baseline": baseline.id, "reform": reform.id},
            )
    finally:
        engine.dispose()


def test_the_public_api_cannot_delete_a_simulation_a_report_uses(
    migrated_database, public_api_rows
) -> None:
    baseline, reform = economy_pair(migrated_database, public_api_rows)
    report_store(migrated_database).create(economy_report(baseline, reform))
    engine = superuser_engine(migrated_database.superuser_url)
    try:
        with (
            pytest.raises(IntegrityError, match="violates foreign key constraint"),
            engine.begin() as connection,
        ):
            connection.execute(
                text("DELETE FROM simulation.simulations WHERE id = :id"),
                {"id": baseline.id},
            )
    finally:
        engine.dispose()


def test_a_migrated_report_is_created_once(migrated_database, public_api_rows) -> None:
    baseline, reform = economy_pair(migrated_database, public_api_rows)
    store = report_store(migrated_database)
    first = store.create_migrated(economy_report(baseline, reform, legacy_report_id=91))

    again = store.create_migrated(
        economy_report(baseline, reform, legacy_report_id=91, year=2027)
    )

    assert again == first
    assert store.get_by_legacy_report_id(91) == first
    assert store.get_by_legacy_report_id(92) is None


def test_a_migrated_report_needs_its_legacy_id(
    migrated_database, public_api_rows
) -> None:
    baseline, reform = economy_pair(migrated_database, public_api_rows)

    with pytest.raises(ValueError, match="legacy_report_id"):
        report_store(migrated_database).create_migrated(
            economy_report(baseline, reform)
        )


def test_an_unknown_report_reads_as_none(migrated_database) -> None:
    assert report_store(migrated_database).get(uuid4()) is None


@pytest.mark.parametrize(
    ("column", "value", "constraint"),
    [
        ("kind", "microsimulation", "ck_reports_kind"),
        ("country", "GB", "ck_reports_country"),
        ("legacy_report_id", -4, "ck_reports_legacy_report_id"),
        ("reform_simulation_id", None, "ck_reports_economy_has_reform"),
    ],
)
def test_the_database_checks_each_report_field(
    migrated_database, public_api_rows, column, value, constraint
) -> None:
    """The database checks what NewReport also checks, for any other writer."""
    baseline, reform = economy_pair(migrated_database, public_api_rows)
    values = {
        "id": uuid4(),
        "kind": "economy",
        "country": "us",
        "baseline_simulation_id": baseline.id,
        "reform_simulation_id": reform.id,
        "year": 2026,
        column: value,
    }
    engine = create_simulation_engine(migrated_database.runtime_url)
    try:
        with (
            pytest.raises(IntegrityError, match=constraint),
            engine.begin() as connection,
        ):
            connection.execute(insert(reports).values(**values))
    finally:
        engine.dispose()
