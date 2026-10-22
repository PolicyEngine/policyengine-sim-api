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


def test_a_reused_baseline_keeps_its_figures_once(
    migrated_database, public_api_rows
) -> None:
    first = load_sample("us_state_ca_2026")
    second = first.model_copy(
        update={"budget": first.budget.model_copy(update={"budgetary_impact": 1.5})}
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
    no_shares = result.intra_decile.model_copy(
        update={"deciles": {**result.intra_decile.deciles, "No change": []}}
    )
    mixed = result.constituency_impact.model_copy(
        update={
            "root": [
                *result.constituency_impact.root,
                result.local_authority_impact.root[0],
            ]
        }
    )

    for refused, message in (
        (_with(result, decile=reordered), "decile"),
        (_with(result, intra_decile=no_shares), "intra_decile"),
        (_with(result, constituency_impact=mixed), "constituency_impact"),
    ):
        with pytest.raises(ValueError, match=message):
            check_storable(refused)


def test_the_recorder_refuses_output_that_is_not_a_result() -> None:
    with pytest.raises(ValidationError):
        RECORDER.validate({"budget": {"budgetary_impact": "much"}})
