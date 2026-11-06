"""A report run's economy output, stored as rows with the run's success."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
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
US_POVERTY_TYPES = {"poverty": "spm", "deep_poverty": "spm_deep"}
UK_POVERTY_TYPES = {"poverty": "relative_bhc", "deep_poverty": "absolute_bhc"}
# The output part each of .py's poverty filter variables rebuilds; a row
# with none is everyone's rate, the part's "all".
POVERTY_PARTS = {
    None: "poverty",
    "age": "poverty",
    "is_male": "poverty_by_gender",
    "race": "poverty_by_race",
}
POVERTY_PART_NAMES = ("poverty", "poverty_by_gender", "poverty_by_race")
CLIFF_COLUMNS = (
    "baseline_cliff_gap",
    "baseline_cliff_share",
    "reform_cliff_gap",
    "reform_cliff_share",
)
LABOR_SUPPLY_TABLES = ("labor_supply_responses", "labor_supply_decile_responses")


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
    # Only the US models state income tax; today's output reports 0.0 elsewhere.
    state_tax = rows.get("household_state_income_tax", {"change": 0.0})["change"]
    return {
        "budgetary_impact": tax - benefits,
        "tax_revenue_impact": tax,
        "state_tax_revenue_impact": state_tax,
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


def _rebuilt_poverty(seeder, run_ids, poverty_types):
    """Rebuild today's three poverty parts from both simulation runs' rows."""
    measures = {
        poverty_type: measure for measure, poverty_type in poverty_types.items()
    }
    parts = {}
    for side, run_id in (
        ("baseline", run_ids.baseline_simulation_run_id),
        ("reform", run_ids.reform_simulation_run_id),
    ):
        for row in seeder.rows("poverty", simulation_run_id=run_id):
            assert row["entity"] == "person"
            # A group needs the variable that selects it, and everyone has neither.
            assert (row["filter_variable"] is None) == (row["filter_group"] is None)
            part = parts.setdefault(POVERTY_PARTS[row["filter_variable"]], {})
            groups = part.setdefault(measures[row["poverty_type"]], {})
            groups.setdefault(row["filter_group"] or "all", {})[side] = row["rate"]
    return {name: parts.get(name) for name in POVERTY_PART_NAMES}


def _rebuilt_cliff_impact(report_run):
    """Rebuild today's cliff impact from the report run's four columns."""
    if report_run["baseline_cliff_gap"] is None:
        return None
    return {
        side: {
            "cliff_gap": report_run[f"{side}_cliff_gap"],
            "cliff_share": report_run[f"{side}_cliff_share"],
        }
        for side in ("baseline", "reform")
    }


def _rebuilt_labor_supply(seeder, report_run_id):
    """Rebuild today's labour supply response from its two tables' rows.

    A decile .py reports no relative response for has null relative columns.
    """
    responses = seeder.rows("labor_supply_responses", report_run_id=report_run_id)
    if not responses:
        return None
    (response,) = responses
    deciles = seeder.rows("labor_supply_decile_responses", report_run_id=report_run_id)

    def by_decile(column):
        return {
            str(row["decile"]): row[column]
            for row in deciles
            if row[column] is not None
        }

    return {
        "substitution_lsr": response["substitution_effect_earnings_change"],
        "income_lsr": response["income_effect_earnings_change"],
        "relative_lsr": {
            "income": response["relative_income_effect"],
            "substitution": response["relative_substitution_effect"],
        },
        "total_change": response["total_earnings_change"],
        "revenue_change": response["revenue_change"],
        "decile": {
            "average": {
                "income": by_decile("average_income_effect"),
                "substitution": by_decile("average_substitution_effect"),
            },
            "relative": {
                "income": by_decile("relative_income_effect"),
                "substitution": by_decile("relative_substitution_effect"),
            },
        },
        "hours": {
            "baseline": response["baseline_weekly_hours"],
            "reform": response["reform_weekly_hours"],
            "change": response["weekly_hours_change"],
            "income_effect": response["weekly_hours_income_effect"],
            "substitution_effect": response["weekly_hours_substitution_effect"],
        },
    }


def _rebuilt_deciles(rows):
    """Rebuild today's decile part; .py's relative change is a percentage."""
    return {
        "average": {str(row["decile"]): row["absolute_change"] for row in rows},
        "relative": {str(row["decile"]): row["relative_change"] / 100 for row in rows},
    }


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
    # Income deciles name no separate variable to group households by.
    assert {row["decile_variable"] for row in deciles} == {None}
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
    # Everyone's rates, and those by age, gender and race, under .py's groups.
    assert _rebuilt_poverty(seeder, run_ids, US_POVERTY_TYPES) == {
        part: output[part] for part in POVERTY_PART_NAMES
    }
    for side, run_id in (
        ("baseline", run_ids.baseline_simulation_run_id),
        ("reform", run_ids.reform_simulation_run_id),
    ):
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
    # The cliff impact is four of the report run's own columns.
    report_run = seeder.row("report_runs", run_ids.report_run_id)
    assert _rebuilt_cliff_impact(report_run) == output["cliff_impact"]
    assert report_run["data_version"] == output["data_version"]
    assert (
        _rebuilt_labor_supply(seeder, run_ids.report_run_id)
        == (output["labor_supply_response"])
    )


def test_the_labour_supply_response_is_one_row_and_a_row_per_decile(
    store, seeder, seeded_run: SeededReportRun
) -> None:
    report_run_id = seeded_run.run_ids.report_run_id

    _complete(store, seeded_run)

    assert len(seeder.rows("labor_supply_responses", report_run_id=report_run_id)) == 1
    deciles = {
        row["decile"]: row
        for row in seeder.rows(
            "labor_supply_decile_responses", report_run_id=report_run_id
        )
    }
    assert sorted(deciles) == [0, 1, 2]
    # .py reports no relative response for decile 0.
    assert (
        deciles[0]["average_income_effect"],
        deciles[0]["average_substitution_effect"],
        deciles[0]["relative_income_effect"],
        deciles[0]["relative_substitution_effect"],
    ) == (-0.05, 0.1, None, None)


def test_an_output_without_labour_supply_or_cliff_impact_stores_neither(
    store, seeder, seeded_run: SeededReportRun
) -> None:
    report_run_id = seeded_run.run_ids.report_run_id
    output = deepcopy(US_ECONOMY_OUTPUT)
    output["labor_supply_response"] = None
    output["cliff_impact"] = None

    assert _complete(store, seeded_run, output=output) == RunWrite(
        changed=True, status=RunStatus.SUCCEEDED
    )

    for table_name in LABOR_SUPPLY_TABLES:
        assert seeder.rows(table_name, report_run_id=report_run_id) == []
    report_run = seeder.row("report_runs", report_run_id)
    assert {report_run[column] for column in CLIFF_COLUMNS} == {None}
    assert report_run["spm_provenance"] is None
    assert report_run["data_version"] == output["data_version"]


def test_the_uk_output_uses_its_own_poverty_types_and_wealth_deciles(
    store, seeder, seeded_run: SeededReportRun
) -> None:
    run_ids = seeded_run.run_ids
    output = uk_economy_output()

    _complete(store, seeded_run, output=output, country="uk")

    # No race breakdown outside the US.
    assert _rebuilt_poverty(seeder, run_ids, UK_POVERTY_TYPES) == {
        part: output[part] for part in POVERTY_PART_NAMES
    }
    deciles = seeder.rows("decile_impacts", report_run_id=run_ids.report_run_id)
    # .py groups wealth deciles' household net income by the wealth decile.
    wealth_deciles = [
        row for row in deciles if row["decile_variable"] == "household_wealth_decile"
    ]
    assert {row["income_variable"] for row in wealth_deciles} == {
        "household_net_income"
    }
    assert {row["entity"] for row in wealth_deciles} == {"household"}
    income_deciles = [row for row in deciles if row["decile_variable"] is None]
    for rows, part in ((wealth_deciles, "wealth_decile"), (income_deciles, "decile")):
        rebuilt = _rebuilt_deciles(rows)
        assert rebuilt["average"] == output[part]["average"]
        assert rebuilt["relative"] == pytest.approx(output[part]["relative"], rel=1e-12)
    assert "household_state_income_tax" not in {
        row["variable_name"]
        for row in seeder.rows("budget_summary", report_run_id=run_ids.report_run_id)
    }
    assert (
        _rebuilt_budget(seeder, run_ids.report_run_id)
        == (uk_economy_output()["budget"])
    )
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
    assert (
        len(seeder.rows("labor_supply_responses", report_run_id=run_ids.report_run_id))
        == 1
    )
    assert (
        len(
            seeder.rows(
                "labor_supply_decile_responses", report_run_id=run_ids.report_run_id
            )
        )
        == 3
    )


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
    broken = replace(
        result,
        rows=(*[rows for rows in result.rows if rows is not budget], duplicated),
    )

    with pytest.raises(IntegrityError):
        store.complete_report_run(
            run_ids, modal_call_id="fc-coordinator", completed_at=LATER, result=broken
        )

    report_run = seeder.row("report_runs", run_ids.report_run_id)
    assert report_run["status"] == "running"
    # The report run's own result columns roll back with its success.
    assert {report_run[column] for column in CLIFF_COLUMNS} == {None}
    assert report_run["data_version"] is None
    for table_name in (
        *LABOR_SUPPLY_TABLES,
        "decile_impacts",
        "budget_summary",
        "intra_decile_impacts",
    ):
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
                filter_group=None,
                rate=0.5,
            )
        )

    _complete(store, seeded_run)

    stored = seeder.rows(
        "poverty",
        simulation_run_id=run_ids.baseline_simulation_run_id,
        poverty_type="spm",
        filter_variable=None,
    )
    assert [row["rate"] for row in stored] == [0.5]
    # Everyone, three ages and two genders under both poverty types, and four
    # races under SPM poverty; the baseline gains all but the stored one.
    baseline = seeder.rows(
        "poverty", simulation_run_id=run_ids.baseline_simulation_run_id
    )
    reform = seeder.rows("poverty", simulation_run_id=run_ids.reform_simulation_run_id)
    assert len(reform) == len(baseline) == 16


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
    # The SPM receipts are the report run's own JSON column.
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
