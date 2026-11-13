"""The output plan every run of a simulation stores, whatever report reads it.

Identical requests share simulation runs (C1 · 24, answer 4), so a run's file
holds what any report reading it needs from that side: every column that
doesn't depend on the other side, the area columns on the local-area dataset
and the US budget columns. It holds the labour-supply response columns only
when the simulation's own policy turns on a response; a report pairing it
with one that does reads that side's responses as zeros. It never holds the
cliff columns, so US national runs keep their region-group split. In the US
it always copies the two inputs .py's labour-supply output reads from either
side, ``self_employment_income`` and ``weekly_hours_worked``.
"""

from __future__ import annotations

from uuid import UUID

import pandas as pd
import pytest
from policyengine.outputs.labor_supply_response import (
    UK_ACTIVE_LSR_VARIABLES,
    US_ACTIVE_LSR_VARIABLES,
)
from policyengine_simulation_contract.stage12_execution import (
    BundleProvenance,
    DatasetArtifactMediaType,
    DatasetArtifactReference,
    DatasetPopulationInput,
    DatasetProvenance,
    GeographySelection,
    ReportAggregate,
    ReportExecutionInput,
    SimulationExecutionInput,
    SimulationRole,
)
from policyengine_simulation_executor.stage12_runtime.output_planning import (
    LABOR_SUPPLY_COPIED_INPUTS,
    labor_supply_response_columns,
    plan_simulation_input,
    resolve_report_output_plan,
    resolve_simulation_output_plan,
    side_read_plan,
    with_zero_labor_supply_responses,
)
from policyengine_simulation_executor.stage12_runtime.partition import (
    should_segment_simulation,
)

EVALUATION_ID = UUID("00000000-0000-0000-0000-000000000001")
LABOR_SUPPLY_POLICIES = {
    "us": {"gov.simulation.labor_supply_responses.elasticities.income": 0.1},
    "uk": {"gov.simulation.labour_supply_responses.income_elasticity": 0.1},
}
CLIFF_COLUMNS = {"cliff_gap", "is_on_cliff"}
PY_LABOR_SUPPLY_COLUMNS = {"us": US_ACTIVE_LSR_VARIABLES, "uk": UK_ACTIVE_LSR_VARIABLES}


def _simulation(country, role=SimulationRole.BASELINE, *, policy=None, options=None):
    dataset = "populace_us_2024" if country == "us" else "microcosm_uk_2024_25"
    bundle = BundleProvenance(
        policyengine_version="6.17.1",
        country_package_name=f"policyengine-{country}",
        country_package_version="1.0.0",
        dataset=DatasetProvenance(
            identity=dataset,
            uri=f"hf://policyengine/data/{dataset}.h5@revision",
            artifact_revision="revision",
            data_package_name="data",
            data_package_version="0.1.0",
        ),
        bundle_manifest_sha256="b" * 64,
    )
    return SimulationExecutionInput(
        evaluation_id=EVALUATION_ID,
        simulation_execution_id=UUID(
            "00000000-0000-0000-0000-000000000002"
            if role is SimulationRole.BASELINE
            else "00000000-0000-0000-0000-000000000003"
        ),
        role=role,
        policy=policy or {},
        population=DatasetPopulationInput(
            artifact=DatasetArtifactReference(
                uri=bundle.dataset.uri,
                media_type=DatasetArtifactMediaType.HDF5,
                content_sha256="a" * 64,
            )
        ),
        year=2026,
        geography=GeographySelection(country=country, region=country),
        options=options or {},
        bundle=bundle,
    )


def _report(country, *, reform_policy=None, options=None):
    return ReportExecutionInput(
        evaluation_id=EVALUATION_ID,
        baseline=_simulation(country, options=options),
        reform=_simulation(
            country, SimulationRole.REFORM, policy=reform_policy, options=options
        ),
        requested_aggregates=tuple(ReportAggregate),
    )


def _columns(plan, entity="person") -> set[str]:
    return set(
        next(
            item.materialized_variables
            for item in plan.entities
            if item.entity == entity
        )
    )


@pytest.mark.parametrize("country", ["us", "uk"])
def test_a_stored_plan_never_holds_the_cliff_columns(country) -> None:
    plan = resolve_simulation_output_plan(
        _simulation(country, options={"include_cliffs": True})
    )

    assert plan.requirements.include_cliff_impacts is False
    assert not CLIFF_COLUMNS & _columns(plan)


@pytest.mark.parametrize("country", ["us", "uk"])
@pytest.mark.parametrize(
    "turns_on_responses", [False, True], ids=["current-law", "responses"]
)
def test_a_stored_plan_holds_responses_only_when_its_own_policy_turns_them_on(
    country, *, turns_on_responses
) -> None:
    policy = LABOR_SUPPLY_POLICIES[country] if turns_on_responses else None

    plan = resolve_simulation_output_plan(_simulation(country, policy=policy))

    responses = set(labor_supply_response_columns(country)["person"])
    assert plan.requirements.labor_supply_response_active is turns_on_responses
    assert responses.issubset(_columns(plan)) is turns_on_responses
    assert bool(responses & _columns(plan)) is turns_on_responses


def test_a_us_stored_plan_copies_the_inputs_the_labour_supply_output_reads() -> None:
    plan = resolve_simulation_output_plan(_simulation("us"))

    assert {"self_employment_income", "weekly_hours_worked"}.issubset(_columns(plan))


def test_a_us_stored_plan_keeps_the_budget_columns() -> None:
    plan = resolve_simulation_output_plan(_simulation("us"))

    assert {"federal_benefit_cost", "state_benefit_cost"}.issubset(_columns(plan))


@pytest.mark.parametrize("country", ["us", "uk"])
def test_pys_labour_supply_columns_are_copied_inputs_or_responses(country) -> None:
    py_columns = {
        column
        for columns in PY_LABOR_SUPPLY_COLUMNS[country].values()
        for column in columns
    }
    copied = {
        column
        for columns in LABOR_SUPPLY_COPIED_INPUTS.get(country, {}).values()
        for column in columns
    }
    responses = {
        column
        for columns in labor_supply_response_columns(country).values()
        for column in columns
    }

    assert copied | responses == py_columns
    assert not copied & responses


def test_a_side_reads_the_reports_columns_but_responses_it_does_not_store() -> None:
    report = _report("us", reform_policy=LABOR_SUPPLY_POLICIES["us"])
    read_plan = resolve_report_output_plan(report)
    baseline_stored = resolve_simulation_output_plan(report.baseline)
    reform_stored = resolve_simulation_output_plan(report.reform)
    responses = set(labor_supply_response_columns("us")["person"])

    baseline_reads = side_read_plan(read_plan, baseline_stored)
    reform_reads = side_read_plan(read_plan, reform_stored)

    assert _columns(baseline_reads) == _columns(read_plan) - responses
    assert reform_reads == read_plan
    # Each side's stored file holds every column the report reads from it.
    for reads, stored in (
        (baseline_reads, baseline_stored),
        (reform_reads, reform_stored),
    ):
        for entity in reads.entities:
            assert set(entity.materialized_variables).issubset(
                _columns(stored, entity.entity)
            )


def test_responses_a_side_does_not_store_are_read_as_zeros() -> None:
    report = _report("us", reform_policy=LABOR_SUPPLY_POLICIES["us"])
    read_plan = resolve_report_output_plan(report)
    baseline_stored = resolve_simulation_output_plan(report.baseline)
    reform_stored = resolve_simulation_output_plan(report.reform)
    frames = {
        "person": pd.DataFrame({"person_id": [1, 2], "weekly_hours_worked": [40, 0]})
    }

    filled = with_zero_labor_supply_responses(frames, read_plan, baseline_stored)
    unchanged = with_zero_labor_supply_responses(frames, read_plan, reform_stored)

    for column in labor_supply_response_columns("us")["person"]:
        assert filled["person"][column].tolist() == [0.0, 0.0]
    # The copied inputs are the side's own values, never zeros.
    assert filled["person"]["weekly_hours_worked"].tolist() == [40, 0]
    assert unchanged is frames
    assert "income_elasticity_lsr" not in frames["person"]


def test_a_us_national_simulation_of_a_cliff_report_keeps_its_region_split() -> None:
    simulation = _simulation("us", options={"include_cliffs": True})

    planned = plan_simulation_input(
        simulation, resolve_simulation_output_plan(simulation)
    )

    assert should_segment_simulation(planned) is True
