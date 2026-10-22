"""Economy results stored as rows and rebuilt exactly."""

from __future__ import annotations

import pytest
from policyengine_simulation_persistence import (
    EconomyComparisonRecorder,
    EconomyResultLabels,
    check_storable,
    create_simulation_engine,
    read_economy_comparison,
)
from pydantic import ValidationError
from result_support import (
    SAMPLE_NAMES,
    comparison_ready,
    labels_for,
    load_sample,
    result_stores,
)
from simulation_support import simulation_store
from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError

pytestmark = pytest.mark.postgres

RECORDER = EconomyComparisonRecorder()


def _write(stores, report_run, result) -> None:
    assert stores.transitions.complete_comparison(
        report_run.id,
        lambda connection, record: RECORDER.write(connection, record, result),
    )


def _read(database, report_run_id, labels):
    engine = create_simulation_engine(database.runtime_url)
    try:
        with engine.connect() as connection:
            return read_economy_comparison(connection, report_run_id, labels)
    finally:
        engine.dispose()


def test_the_sample_covers_every_case_the_plan_names() -> None:
    assert SAMPLE_NAMES == [
        "uk_constituency_2026",
        "uk_local_authority_2026",
        "uk_nation_scotland_2026",
        "uk_national_2026",
        "us_budget_window_year_2030",
        "us_congressional_district_ca_12_2026",
        "us_national_2026",
        "us_state_ca_2026",
    ]


@pytest.mark.parametrize("sample_name", SAMPLE_NAMES)
def test_a_stored_result_is_rebuilt_exactly(
    migrated_database, public_api_rows, sample_name
) -> None:
    result = load_sample(sample_name)
    stores = result_stores(migrated_database)
    report_run = comparison_ready(stores, migrated_database, public_api_rows)

    _write(stores, report_run, RECORDER.validate(result.model_dump(mode="json")))

    rebuilt = _read(migrated_database, report_run.id, labels_for(result))
    assert rebuilt.model_dump_json() == result.model_dump_json()
    assert rebuilt == result


UK_AREA_DISCLOSURE_FIELDS = {
    "disclosure_tier",
    "average_household_income_change_lower",
    "average_household_income_change_upper",
    "relative_household_income_change_lower",
    "relative_household_income_change_upper",
}


def test_each_uk_area_keeps_its_disclosure_tier_and_intervals(
    migrated_database, public_api_rows
) -> None:
    result = load_sample("uk_nation_scotland_2026")
    stores = result_stores(migrated_database)
    report_run = comparison_ready(stores, migrated_database, public_api_rows)

    _write(stores, report_run, RECORDER.validate(result.model_dump(mode="json")))

    rebuilt = _read(migrated_database, report_run.id, labels_for(result))
    for written, read in (
        (result.constituency_impact, rebuilt.constituency_impact),
        (result.local_authority_impact, rebuilt.local_authority_impact),
    ):
        assert written is not None
        assert read is not None
        assert {record.disclosure_tier for record in written.root} == {
            "reported",
            "approximate",
        }
        assert [
            record.model_dump(include=UK_AREA_DISCLOSURE_FIELDS) for record in read.root
        ] == [
            record.model_dump(include=UK_AREA_DISCLOSURE_FIELDS)
            for record in written.root
        ]


def test_each_report_run_reads_its_own_figures_of_a_reused_baseline(
    migrated_database, public_api_rows
) -> None:
    """A reused run's figures can differ by comparison, for example by SPM."""
    first = load_sample("us_state_ca_2026")
    child = first.poverty.poverty.child.model_copy(update={"baseline": 0.123456})
    second = first.model_copy(
        update={
            "budget": first.budget.model_copy(update={"budgetary_impact": 1.5}),
            "poverty": first.poverty.model_copy(
                update={
                    "poverty": first.poverty.poverty.model_copy(update={"child": child})
                }
            ),
        }
    )
    stores = result_stores(migrated_database)
    first_run = comparison_ready(stores, migrated_database, public_api_rows)
    _write(stores, first_run, first)
    baseline = stores.runs.get_simulation_run(first_run.baseline_simulation_run_id)
    second_run = comparison_ready(
        stores,
        migrated_database,
        public_api_rows,
        baseline=simulation_store(migrated_database).get(baseline.simulation_id),
    )
    assert second_run.baseline_simulation_run_id == first_run.baseline_simulation_run_id

    _write(stores, second_run, second)

    labels = labels_for(first)
    assert _read(migrated_database, first_run.id, labels) == first
    assert _read(migrated_database, second_run.id, labels) == second


def _executor_fallbacks(result):
    """Return the shapes today's executor emits when a breakdown fails."""
    categories = list(result.intra_decile.all)
    failed_intra = result.intra_decile.model_copy(
        update={
            "deciles": {category: [] for category in categories},
            "all": dict.fromkeys(categories, 0.0),
        }
    )
    no_deciles = result.decile.model_copy(update={"average": {}, "relative": {}})
    return result.model_copy(
        update={
            "intra_decile": failed_intra,
            "intra_wealth_decile": failed_intra,
            "wealth_decile": no_deciles,
        }
    )


def test_the_executors_fallback_shapes_are_rebuilt_exactly(
    migrated_database, public_api_rows
) -> None:
    result = _executor_fallbacks(load_sample("uk_national_2026"))
    stores = result_stores(migrated_database)
    report_run = comparison_ready(stores, migrated_database, public_api_rows)

    _write(stores, report_run, RECORDER.validate(result.model_dump(mode="json")))

    rebuilt = _read(migrated_database, report_run.id, labels_for(result))
    assert rebuilt.model_dump_json() == result.model_dump_json()


def test_an_empty_income_decile_map_is_rebuilt(
    migrated_database, public_api_rows
) -> None:
    sample = load_sample("us_budget_window_year_2030")
    result = sample.model_copy(
        update={
            "decile": sample.decile.model_copy(update={"average": {}, "relative": {}})
        }
    )
    stores = result_stores(migrated_database)
    report_run = comparison_ready(stores, migrated_database, public_api_rows)

    _write(stores, report_run, result)

    assert _read(migrated_database, report_run.id, labels_for(result)) == result


def test_reading_needs_a_succeeded_comparison(
    migrated_database, public_api_rows
) -> None:
    stores = result_stores(migrated_database)
    report_run = comparison_ready(stores, migrated_database, public_api_rows)

    with pytest.raises(LookupError, match="no succeeded economy comparison"):
        _read(
            migrated_database,
            report_run.id,
            EconomyResultLabels(model_version="1", data_version="1"),
        )


def test_reading_needs_a_label_for_every_geography(
    migrated_database, public_api_rows
) -> None:
    result = load_sample("uk_nation_scotland_2026")
    stores = result_stores(migrated_database)
    report_run = comparison_ready(stores, migrated_database, public_api_rows)
    _write(stores, report_run, result)
    labels = labels_for(result)
    missing = EconomyResultLabels(
        model_version=labels.model_version,
        data_version=labels.data_version,
        constituencies=dict(list(labels.constituencies.items())[1:]),
        local_authorities=labels.local_authorities,
    )

    with pytest.raises(LookupError, match="no label for S14"):
        _read(migrated_database, report_run.id, missing)


def _with(result, **sections):
    return result.model_copy(update=sections)


def test_results_the_tables_cannot_hold_exactly_are_refused() -> None:
    result = load_sample("uk_national_2026")
    reordered = result.decile.model_copy(
        update={"relative": dict(reversed(result.decile.relative.items()))}
    )
    other_categories = result.intra_decile.model_copy(
        update={"all": dict(reversed(result.intra_decile.all.items()))}
    )
    mixed = result.constituency_impact.model_copy(
        update={
            "root": [
                *result.constituency_impact.root,
                result.local_authority_impact.root[0],
            ]
        }
    )
    repeated_constituency = result.constituency_impact.model_copy(
        update={"root": [*result.constituency_impact.root[:2]] * 2}
    )
    districts = load_sample("us_state_ca_2026").congressional_district_impact
    repeated_district = districts.model_copy(
        update={"districts": [*districts.districts[:1]] * 2}
    )

    for refused, message in (
        (_with(result, decile=reordered), "decile"),
        (_with(result, intra_decile=other_categories), "intra_decile"),
        (_with(result, constituency_impact=mixed), "constituency_impact"),
        (_with(result, constituency_impact=repeated_constituency), "repeats"),
        (_with(result, congressional_district_impact=repeated_district), "repeats"),
    ):
        with pytest.raises(ValueError, match=message):
            check_storable(refused)


def test_the_recorder_refuses_output_that_is_not_a_result() -> None:
    with pytest.raises(ValidationError):
        RECORDER.validate({"budget": {"budgetary_impact": "much"}})


@pytest.mark.parametrize("table_name", ["budget_impacts", "poverty_rates"])
def test_the_runtime_role_cannot_change_stored_results(
    migrated_database, public_api_rows, table_name
) -> None:
    stores = result_stores(migrated_database)
    report_run = comparison_ready(stores, migrated_database, public_api_rows)
    _write(stores, report_run, load_sample("us_budget_window_year_2030"))
    engine = create_simulation_engine(migrated_database.runtime_url)
    try:
        for statement in (
            f"UPDATE simulation.{table_name} SET report_run_id = report_run_id",  # noqa: S608  # fixed table names
            f"DELETE FROM simulation.{table_name}",  # noqa: S608  # fixed table names
        ):
            with (
                pytest.raises(ProgrammingError, match="permission denied"),
                engine.begin() as connection,
            ):
                connection.execute(text(statement))
    finally:
        engine.dispose()
