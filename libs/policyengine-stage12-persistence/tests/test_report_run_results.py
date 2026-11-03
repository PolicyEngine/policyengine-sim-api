"""A report run's economy output, stored as rows with the run's success."""

from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

import pytest
from economy_output import (
    US_AT_LARGE_STATES,
    US_ECONOMY_OUTPUT,
    US_STATE_FIPS,
    uk_economy_output,
    uk_output_with_areas,
    us_output_with_every_part,
)
from policyengine_stage12_persistence import (
    DistrictCodes,
    ReportRunStore,
    RunStatus,
    RunWrite,
    report_run_result,
)
from policyengine_stage12_persistence.report_run_results import ResultRows
from report_run_database import NOW, SeededReportRun, worker_database_url
from sqlalchemy import insert
from sqlalchemy.exc import IntegrityError

LATER = NOW + timedelta(minutes=5)
INTRA_DECILE_LABELS = {
    "Lose more than 5%": "lose_more_than_5pct",
    "Lose less than 5%": "lose_less_than_5pct",
    "No change": "no_change",
    "Gain less than 5%": "gain_less_than_5pct",
    "Gain more than 5%": "gain_more_than_5pct",
}
AGE_GROUPS = ("child", "adult", "senior")


@pytest.fixture
def store():
    run_store = ReportRunStore(worker_database_url())
    try:
        yield run_store
    finally:
        run_store.engine.dispose()


DISTRICT_CODES = DistrictCodes(
    state_fips=US_STATE_FIPS, at_large_states=US_AT_LARGE_STATES
)
STATE_BY_FIPS = {fips: state for state, fips in US_STATE_FIPS.items()}
AREA_FIELDS = (
    "average_household_income_change",
    "relative_household_income_change",
    "population",
    "disclosure_tier",
    "average_household_income_change_lower",
    "average_household_income_change_upper",
    "relative_household_income_change_lower",
    "relative_household_income_change_upper",
)


def _complete(store, seeded_run, output=US_ECONOMY_OUTPUT, country="us"):
    store.claim_report_run(
        seeded_run.run_ids.report_run_id, modal_call_id="fc-coordinator", claimed_at=NOW
    )
    return store.complete_report_run(
        seeded_run.run_ids,
        modal_call_id="fc-coordinator",
        completed_at=LATER,
        result=report_run_result(
            output,
            run_ids=seeded_run.run_ids,
            country=country,
            district_codes=DISTRICT_CODES,
        ),
    )


def _rebuilt_district(row):
    """Rebuild today's district code as the output builder formats it."""
    state = STATE_BY_FIPS[row["state_fips"]]
    number = 1 if state in US_AT_LARGE_STATES else row["district_number"]
    return f"{state}-{number:02d}"


def _rebuilt_budget(seeder, report_run_id):
    rows = {
        row["variable_name"]: row
        for row in seeder.rows("budget_summary", report_run_id=report_run_id)
    }
    tax = rows["household_tax"]["change"]
    benefits = rows["household_benefits"]["change"]
    return {
        "budgetary_impact": tax - benefits,
        "tax_revenue_impact": tax,
        "state_tax_revenue_impact": rows["household_state_income_tax"]["change"],
        "benefit_spending_impact": benefits,
        "households": rows["household_weight"]["baseline_total"],
        "baseline_net_income": rows["household_net_income"]["baseline_total"],
    }


def _rebuilt_intra_decile(rows):
    """Rebuild today's shares; each overall one is the deciles' mean, as today."""
    by_decile = {row["decile"]: row for row in rows}
    deciles = sorted(by_decile)
    shares = {
        label: [by_decile[decile][column] for decile in deciles]
        for label, column in INTRA_DECILE_LABELS.items()
    }
    return {
        "all": {
            label: sum(values) / len(values) if values else 0.0
            for label, values in shares.items()
        },
        "deciles": shares,
    }


def _rebuilt_poverty(seeder, simulation_run_id, *, poverty_type, groups):
    rows = {
        row["filter_variable"]: row["rate"]
        for row in seeder.rows(
            "poverty", simulation_run_id=simulation_run_id, poverty_type=poverty_type
        )
    }
    return {group: rows[None if group == "all" else group] for group in groups}


def test_a_report_run_stores_its_economy_output_as_rows(
    store, seeder, seeded_run: SeededReportRun
) -> None:
    run_ids = seeded_run.run_ids

    assert _complete(store, seeded_run) == RunWrite(
        changed=True, status=RunStatus.SUCCEEDED
    )

    output = US_ECONOMY_OUTPUT
    assert _rebuilt_budget(seeder, run_ids.report_run_id) == output["budget"]
    deciles = seeder.rows("decile_impacts", report_run_id=run_ids.report_run_id)
    assert {str(row["decile"]): row["absolute_change"] for row in deciles} == output[
        "decile"
    ]["average"]
    # .py's rows hold the relative change as a percentage; today's output
    # holds a fraction, so a reader divides by 100.
    assert {
        str(row["decile"]): row["relative_change"] / 100 for row in deciles
    } == pytest.approx(output["decile"]["relative"], rel=1e-12)
    assert sorted(row["relative_change"] for row in deciles) == pytest.approx(
        [1.0, 2.0]
    )
    assert {row["income_variable"] for row in deciles} == {"household_net_income"}
    assert all(
        row["baseline_simulation_run_id"] == run_ids.baseline_simulation_run_id
        and row["reform_simulation_run_id"] == run_ids.reform_simulation_run_id
        for row in deciles
    )
    intra = seeder.rows("intra_decile_impacts", report_run_id=run_ids.report_run_id)
    # Row 0 is .py's share across the whole population, which today's output
    # doesn't carry, so none is written.
    assert sorted(row["decile"] for row in intra) == [1, 2]
    assert _rebuilt_intra_decile(intra) == output["intra_decile"]
    for side, run_id in (
        ("baseline", run_ids.baseline_simulation_run_id),
        ("reform", run_ids.reform_simulation_run_id),
    ):
        for poverty_type, key in (("spm", "poverty"), ("spm_deep", "deep_poverty")):
            assert _rebuilt_poverty(
                seeder,
                run_id,
                poverty_type=poverty_type,
                groups=(*AGE_GROUPS, "all", "male", "female"),
            ) == {
                **{
                    group: output["poverty"][key][group][side]
                    for group in (*AGE_GROUPS, "all")
                },
                **{
                    group: output["poverty_by_gender"][key][group][side]
                    for group in ("male", "female")
                },
            }
        assert _rebuilt_poverty(
            seeder,
            run_id,
            poverty_type="spm",
            groups=("white", "black", "hispanic", "other"),
        ) == {
            group: value[side]
            for group, value in output["poverty_by_race"]["poverty"].items()
        }
        (inequality,) = seeder.rows("inequality", simulation_run_id=run_id)
        assert inequality["income_variable"] == "household_net_income"
        assert inequality["entity"] == "household"
        assert (
            inequality["gini"],
            inequality["top_10_share"],
            inequality["top_1_share"],
        ) == (
            output["inequality"]["gini"][side],
            output["inequality"]["top_10_pct_share"][side],
            output["inequality"]["top_1_pct_share"][side],
        )
    report_run = seeder.row("report_runs", run_ids.report_run_id)
    assert report_run["labor_supply_result"] == output["labor_supply_response"]
    assert report_run["cliff_impact_result"] == output["cliff_impact"]


def test_the_uk_output_uses_its_own_poverty_types_and_wealth_shares(
    store, seeder, seeded_run: SeededReportRun
) -> None:
    run_ids = seeded_run.run_ids

    _complete(store, seeded_run, output=uk_economy_output(), country="uk")

    poverty_types = {
        row["poverty_type"]
        for row in seeder.rows(
            "poverty", simulation_run_id=run_ids.baseline_simulation_run_id
        )
    }
    assert poverty_types == {"relative_bhc", "absolute_bhc"}
    (inequality,) = seeder.rows(
        "inequality", simulation_run_id=run_ids.reform_simulation_run_id
    )
    assert inequality["income_variable"] == "equiv_hbai_household_net_income"
    wealth = [
        row
        for row in seeder.rows(
            "intra_decile_impacts", report_run_id=run_ids.report_run_id
        )
        if row["decile_type"] == "wealth"
    ]
    assert _rebuilt_intra_decile(wealth) == uk_economy_output()["intra_wealth_decile"]


def test_a_repeated_completion_stores_no_second_result(
    store, seeder, seeded_run: SeededReportRun
) -> None:
    run_ids = seeded_run.run_ids
    _complete(store, seeded_run)

    repeated = store.complete_report_run(
        run_ids,
        modal_call_id="fc-coordinator",
        completed_at=LATER,
        result=report_run_result(US_ECONOMY_OUTPUT, run_ids=run_ids, country="us"),
    )

    assert repeated == RunWrite(changed=False, status=RunStatus.SUCCEEDED)
    assert len(seeder.rows("budget_summary", report_run_id=run_ids.report_run_id)) == 5
    assert len(seeder.rows("decile_impacts", report_run_id=run_ids.report_run_id)) == 2


def test_a_result_that_cannot_be_stored_leaves_the_report_run_running(
    store, seeder, seeded_run: SeededReportRun
) -> None:
    run_ids = seeded_run.run_ids
    store.claim_report_run(
        run_ids.report_run_id, modal_call_id="fc-coordinator", claimed_at=NOW
    )
    result = report_run_result(US_ECONOMY_OUTPUT, run_ids=run_ids, country="us")
    budget = next(rows for rows in result.rows if rows.table.name == "budget_summary")
    # The same row twice breaks the table's unique key in the last insert.
    duplicated = ResultRows(
        table=budget.table,
        rows=(*budget.rows, {**budget.rows[0], "id": uuid4()}),
        shared_by_reports=False,
    )
    broken = result.__class__(
        rows=(*[rows for rows in result.rows if rows is not budget], duplicated),
        labor_supply_result=result.labor_supply_result,
        cliff_impact_result=result.cliff_impact_result,
    )

    with pytest.raises(IntegrityError):
        store.complete_report_run(
            run_ids, modal_call_id="fc-coordinator", completed_at=LATER, result=broken
        )

    report_run = seeder.row("report_runs", run_ids.report_run_id)
    assert report_run["status"] == "running"
    assert report_run["labor_supply_result"] is None
    for table_name in ("decile_impacts", "budget_summary", "intra_decile_impacts"):
        assert seeder.rows(table_name, report_run_id=run_ids.report_run_id) == []
    assert (
        seeder.rows("poverty", simulation_run_id=run_ids.baseline_simulation_run_id)
        == []
    )


def test_a_shared_simulation_run_keeps_the_results_first_stored_for_it(
    store, seeder, seeded_run: SeededReportRun
) -> None:
    run_ids = seeded_run.run_ids
    # An earlier report run that shares this baseline stored its poverty first.
    with seeder.engine.begin() as connection:
        connection.execute(
            insert(seeder.table("poverty")).values(
                id=uuid4(),
                simulation_run_id=run_ids.baseline_simulation_run_id,
                poverty_type="spm",
                entity="person",
                filter_variable=None,
                rate=0.5,
            )
        )

    _complete(store, seeded_run)

    stored = seeder.rows(
        "poverty",
        simulation_run_id=run_ids.baseline_simulation_run_id,
        poverty_type="spm",
    )
    assert next(row for row in stored if row["filter_variable"] is None)["rate"] == 0.5
    assert len(stored) == len({row["filter_variable"] for row in stored})
    reform = seeder.rows("poverty", simulation_run_id=run_ids.reform_simulation_run_id)
    assert len(reform) == 2 * 6 + 4


def test_the_detailed_budget_districts_and_receipts_are_stored_losslessly(
    store, seeder, seeded_run: SeededReportRun
) -> None:
    report_run_id = seeded_run.run_ids.report_run_id
    output = us_output_with_every_part()

    _complete(store, seeded_run, output=output)

    programs = seeder.rows("program_statistics", report_run_id=report_run_id)
    assert {
        row["program_name"]: {
            "baseline": row["baseline_total"],
            "reform": row["reform_total"],
            "difference": row["change"],
            "is_tax": row["is_tax"],
        }
        for row in programs
    } == output["detailed_budget"]
    assert {row["entity"] for row in programs} == {None}
    districts = seeder.rows(
        "congressional_district_impacts", report_run_id=report_run_id
    )
    assert sorted(
        (
            {
                "district": _rebuilt_district(row),
                **{
                    field: row[field]
                    for field in (
                        "average_household_income_change",
                        "relative_household_income_change",
                        "winner_percentage",
                        "loser_percentage",
                        "no_change_percentage",
                        "population",
                    )
                },
            }
            for row in districts
        ),
        key=lambda district: district["district"],
    ) == sorted(
        output["congressional_district_impact"]["districts"],
        key=lambda district: district["district"],
    )
    # Census GEOIDs: an at-large district is numbered 00.
    assert {row["district_geoid"] for row in districts} == {101, 107, 200, 612}
    report_run = seeder.row("report_runs", report_run_id)
    assert report_run["spm_provenance"] == output["spm_provenance"]
    assert report_run["data_version"] == output["data_version"]


def test_the_uk_area_impacts_are_stored_with_their_disclosure_fields(
    store, seeder, seeded_run: SeededReportRun
) -> None:
    report_run_id = seeded_run.run_ids.report_run_id
    output = uk_output_with_areas()

    _complete(store, seeded_run, output=output, country="uk")

    for table_name, kind in (
        ("constituency_impacts", "constituency"),
        ("local_authority_impacts", "local_authority"),
    ):
        stored = {
            row[f"{kind}_code"]: {
                f"{kind}_code": row[f"{kind}_code"],
                f"{kind}_name": row[f"{kind}_name"],
                **{field: row[field] for field in AREA_FIELDS},
            }
            for row in seeder.rows(table_name, report_run_id=report_run_id)
        }
        expected = {
            record[f"{kind}_code"]: dict.fromkeys(AREA_FIELDS) | record
            for record in output[f"{kind}_impact"]
        }
        assert stored == expected


def test_a_district_in_an_unknown_state_is_refused(seeded_run: SeededReportRun) -> None:
    output = us_output_with_every_part()
    output["congressional_district_impact"]["districts"][0]["district"] = "ZZ-01"

    with pytest.raises(ValueError, match="ZZ"):
        report_run_result(
            output,
            run_ids=seeded_run.run_ids,
            country="us",
            district_codes=DISTRICT_CODES,
        )
