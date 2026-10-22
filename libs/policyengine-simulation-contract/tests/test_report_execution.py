"""Tests for the typed outcome a report coordinator returns to its caller."""

from __future__ import annotations

import json
import pickle
from datetime import UTC, datetime
from hashlib import sha256
from typing import Any
from uuid import UUID

import pytest
from policyengine_simulation_contract.macro_output import SingleYearMacroOutput
from policyengine_simulation_contract.report_execution import (
    ReportExecutionError,
    ReportExecutionFailure,
    ReportExecutionProvenance,
    ReportExecutionResult,
    report_aggregate_sha256,
)
from policyengine_simulation_contract.spm import (
    SPMRuntimeVersions,
    SPMSelection,
    build_spm_comparison_provenance,
    build_spm_provenance,
)
from policyengine_simulation_contract.stage12_execution import (
    ArtifactMediaType,
    ArtifactReference,
    BundleProvenance,
    DatasetProvenance,
    EntityOutputPlan,
    ReportAggregate,
    ReportOutputRequirements,
    RowIdentity,
    SimulationArtifactDescriptor,
    SimulationRole,
    Stage12OutputPlan,
    stage12_output_plan_sha256,
)
from pydantic import ValidationError

COMPLETED_AT = datetime(2026, 10, 22, tzinfo=UTC)
EVALUATION_ID = UUID("00000000-0000-0000-0000-000000000001")
BASELINE_ID = UUID("00000000-0000-0000-0000-000000000002")
REFORM_ID = UUID("00000000-0000-0000-0000-000000000003")


def _bundle() -> BundleProvenance:
    return BundleProvenance(
        policyengine_version="6.7.5",
        country_package_name="policyengine-us",
        country_package_version="2.2.1",
        dataset=DatasetProvenance(
            identity="populace_us_2024",
            uri="hf://policyengine/data/populace_us_2024.h5@revision",
            artifact_revision="revision",
            data_package_name="populace-data",
            data_package_version="0.1.0",
        ),
        bundle_manifest_sha256="b" * 64,
    )


def _output_plan() -> Stage12OutputPlan:
    return Stage12OutputPlan(
        country="us",
        requirements=ReportOutputRequirements(
            aggregates=tuple(ReportAggregate),
            include_cliff_impacts=False,
            labor_supply_response_active=False,
        ),
        entities=(
            EntityOutputPlan(
                entity="household",
                materialized_variables=("household_id", "household_net_income"),
            ),
        ),
    )


def _descriptor(role: SimulationRole) -> SimulationArtifactDescriptor:
    is_baseline = role is SimulationRole.BASELINE
    return SimulationArtifactDescriptor(
        evaluation_id=EVALUATION_ID,
        simulation_execution_id=BASELINE_ID if is_baseline else REFORM_ID,
        role=role,
        artifact=ArtifactReference(
            uri=f"gs://private/stage-12-runs/staging/{role.value}.parquet",
            media_type=ArtifactMediaType.PARQUET,
            content_sha256=("1" if is_baseline else "2") * 64,
            size_bytes=2048,
        ),
        output_plan_sha256=stage12_output_plan_sha256(_output_plan()),
        row_identity=RowIdentity(
            identifier_columns=("household.household_id",),
            row_count=1,
            identity_sha256="c" * 64,
        ),
        bundle=_bundle(),
    )


def _baseline_and_reform(baseline: float, reform: float) -> dict[str, float]:
    return {"baseline": baseline, "reform": reform}


def _poverty_by_age() -> dict[str, dict[str, float]]:
    return {
        group: _baseline_and_reform(0.1, 0.09)
        for group in ("child", "adult", "senior", "all")
    }


def _aggregate() -> dict[str, Any]:
    """Return a complete single-year macro result with the US-only sections set."""
    return {
        "spm_provenance": None,
        "model_version": "2.2.1",
        "data_version": "revision",
        "budget": {
            "tax_revenue_impact": 100.0,
            "state_tax_revenue_impact": 20.0,
            "benefit_spending_impact": 30.0,
            "budgetary_impact": 70.0,
            "households": 2.0,
            "baseline_net_income": 1000.0,
        },
        "detailed_budget": {
            "income_tax": {"baseline": 100.0, "reform": 125.0, "difference": 25.0}
        },
        "decile": {
            "average": {"1": 10.0, "10": 20.0, "2": 15.0},
            "relative": {"1": 0.01, "10": 0.02, "2": 0.015},
        },
        "inequality": {
            "gini": _baseline_and_reform(0.4, 0.39),
            "top_10_pct_share": _baseline_and_reform(0.3, 0.29),
            "top_1_pct_share": _baseline_and_reform(0.1, 0.09),
        },
        "poverty": {"poverty": _poverty_by_age(), "deep_poverty": _poverty_by_age()},
        "poverty_by_gender": {
            "poverty": {
                "male": _baseline_and_reform(0.08, 0.07),
                "female": _baseline_and_reform(0.09, 0.08),
            },
            "deep_poverty": {
                "male": _baseline_and_reform(0.02, 0.02),
                "female": _baseline_and_reform(0.03, 0.02),
            },
        },
        "poverty_by_race": {
            "poverty": {
                race: _baseline_and_reform(0.06, 0.05)
                for race in ("white", "black", "hispanic", "other")
            }
        },
        "intra_decile": {
            "deciles": {"Gain less than 5%": [0.4, 0.2], "No change": [0.6, 0.8]},
            "all": {"Gain less than 5%": 0.3, "No change": 0.7},
        },
        "wealth_decile": None,
        "intra_wealth_decile": None,
        "labor_supply_response": None,
        "constituency_impact": None,
        "local_authority_impact": None,
        "congressional_district_impact": {
            "districts": [
                {
                    "district": "AL-01",
                    "average_household_income_change": 10.0,
                    "relative_household_income_change": 0.01,
                    "winner_percentage": 0.6,
                    "loser_percentage": 0.3,
                    "no_change_percentage": 0.1,
                    "population": 1000.0,
                }
            ]
        },
        "cliff_impact": None,
    }


def _spm_comparison() -> dict[str, Any]:
    selection = SPMSelection(
        forecast_content_sha256="a" * 64,
        scenario="official",
        geography_kind="national",
        geography_id=None,
        county_vintage="2020",
        as_of=None,
    )
    receipt = build_spm_provenance(
        forecast_id="forecast-2026",
        forecast_sha256="a" * 64,
        selection=selection,
        years=("2026",),
        runtime_versions=SPMRuntimeVersions.model_validate(
            {
                "policyengine": "6.7.5",
                "policyengine-core": "3.32.10",
                "policyengine-us": "2.2.1",
                "spm-calculator": "1.0.0",
            }
        ),
    )
    return build_spm_comparison_provenance(
        baseline_receipts=(receipt,),
        reform_receipts=(receipt,),
    ).model_dump(mode="json", by_alias=True)


def _provenance() -> ReportExecutionProvenance:
    return ReportExecutionProvenance(
        bundle=_bundle(),
        worker_version="6.7.5",
        modal_environment="staging",
        modal_application="policyengine-simulation-v2-py6-7-5",
        simulation_callable="run_single_simulation_us",
        version_manifest_sha256="d" * 64,
        coordinator_invocation_id="fc-coordinator",
    )


def _result_payload(**overrides: Any) -> dict[str, Any]:
    aggregate = overrides.pop("aggregate", _aggregate())
    payload: dict[str, Any] = {
        "evaluation_id": str(EVALUATION_ID),
        "requested_aggregates": [
            aggregate_name.value for aggregate_name in ReportAggregate
        ],
        "aggregate": aggregate,
        "aggregate_sha256": report_aggregate_sha256(
            SingleYearMacroOutput.model_validate(aggregate)
        ),
        "output_plan": _output_plan().model_dump(mode="json"),
        "baseline": _descriptor(SimulationRole.BASELINE).model_dump(mode="json"),
        "reform": _descriptor(SimulationRole.REFORM).model_dump(mode="json"),
        "provenance": _provenance().model_dump(mode="json"),
        "completed_at": COMPLETED_AT.isoformat(),
    }
    payload.update(overrides)
    return payload


def _failure() -> ReportExecutionFailure:
    return ReportExecutionFailure(
        evaluation_id=EVALUATION_ID,
        error_code="simulation_execution_failed",
        error_summary="ValueError in dataset_load (baseline): bad input",
    )


def test_result_round_trips_through_json() -> None:
    result = ReportExecutionResult.model_validate(_result_payload())

    restored = ReportExecutionResult.model_validate(
        json.loads(json.dumps(result.model_dump(mode="json")))
    )

    assert restored == result
    assert restored.aggregate.budget.budgetary_impact == 70.0
    assert restored.baseline.role is SimulationRole.BASELINE
    assert restored.provenance.bundle == _bundle()


def test_result_carries_the_totals_inline_as_a_single_year_macro_output() -> None:
    result = ReportExecutionResult.model_validate(_result_payload())

    assert isinstance(result.aggregate, SingleYearMacroOutput)
    assert result.aggregate.model_dump(mode="json") == _aggregate()


@pytest.mark.parametrize(
    "change",
    [
        lambda aggregate: aggregate.pop("budget"),
        lambda aggregate: aggregate.update({"unexpected_section": {}}),
        lambda aggregate: aggregate["budget"].update({"budgetary_impact": "large"}),
    ],
    ids=["missing-section", "unknown-section", "non-numeric-value"],
)
def test_result_rejects_an_aggregate_outside_the_macro_output_schema(change) -> None:
    aggregate = _aggregate()
    change(aggregate)
    payload = _result_payload()
    payload["aggregate"] = aggregate

    with pytest.raises(ValidationError):
        ReportExecutionResult.model_validate(payload)


def test_result_rejects_an_aggregate_digest_that_does_not_match() -> None:
    with pytest.raises(ValidationError, match="aggregate_sha256"):
        ReportExecutionResult.model_validate(_result_payload(aggregate_sha256="e" * 64))


def test_result_rejects_a_non_finite_aggregate_value() -> None:
    aggregate = _aggregate()
    aggregate["budget"]["budgetary_impact"] = float("nan")
    payload = _result_payload()
    payload["aggregate"] = aggregate

    with pytest.raises(ValidationError, match="non-finite"):
        ReportExecutionResult.model_validate(payload)


def test_aggregate_digest_is_the_digest_of_its_canonical_json() -> None:
    aggregate = SingleYearMacroOutput.model_validate(_aggregate())
    reordered = SingleYearMacroOutput.model_validate(
        dict(reversed(list(_aggregate().items())))
    )
    expected = sha256(
        json.dumps(
            _aggregate(),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()

    assert report_aggregate_sha256(aggregate) == expected
    assert report_aggregate_sha256(reordered) == expected


def test_aggregate_digest_survives_a_json_round_trip_with_spm_provenance() -> None:
    aggregate_payload = {**_aggregate(), "spm_provenance": _spm_comparison()}
    aggregate = SingleYearMacroOutput.model_validate(aggregate_payload)
    restored = SingleYearMacroOutput.model_validate(
        json.loads(json.dumps(aggregate.model_dump(mode="json")))
    )

    assert aggregate.model_dump(mode="json") == aggregate_payload
    assert report_aggregate_sha256(restored) == report_aggregate_sha256(aggregate)
    ReportExecutionResult.model_validate(_result_payload(aggregate=aggregate_payload))


@pytest.mark.parametrize(
    ("field", "replacement", "message"),
    [
        (
            "baseline",
            lambda: _descriptor(SimulationRole.REFORM).model_copy(
                update={"simulation_execution_id": BASELINE_ID}
            ),
            "baseline artifact must use the baseline role",
        ),
        (
            "reform",
            lambda: _descriptor(SimulationRole.BASELINE).model_copy(
                update={"simulation_execution_id": REFORM_ID}
            ),
            "reform artifact must use the reform role",
        ),
        (
            "reform",
            lambda: _descriptor(SimulationRole.REFORM).model_copy(
                update={"evaluation_id": UUID(int=99)}
            ),
            "must belong to this report",
        ),
        (
            "reform",
            lambda: _descriptor(SimulationRole.REFORM).model_copy(
                update={"simulation_execution_id": BASELINE_ID}
            ),
            "different simulation identifiers",
        ),
        (
            "reform",
            lambda: _descriptor(SimulationRole.REFORM).model_copy(
                update={
                    "bundle": _bundle().model_copy(
                        update={"country_package_version": "2.2.2"}
                    )
                }
            ),
            "must use the report's bundle",
        ),
        (
            "baseline",
            lambda: _descriptor(SimulationRole.BASELINE).model_copy(
                update={"output_plan_sha256": "f" * 64}
            ),
            "must follow the report's output plan",
        ),
        (
            "reform",
            lambda: _descriptor(SimulationRole.REFORM).model_copy(
                update={
                    "row_identity": RowIdentity(
                        identifier_columns=("household.household_id",),
                        row_count=2,
                        identity_sha256="c" * 64,
                    )
                }
            ),
            "must cover the same rows",
        ),
    ],
    ids=[
        "baseline-role",
        "reform-role",
        "other-report",
        "same-simulation",
        "other-bundle",
        "other-output-plan",
        "other-rows",
    ],
)
def test_result_rejects_artifacts_that_do_not_form_this_report(
    field, replacement, message
) -> None:
    payload = _result_payload(**{field: replacement().model_dump(mode="json")})

    with pytest.raises(ValidationError, match=message):
        ReportExecutionResult.model_validate(payload)


def test_result_requires_a_timezone_aware_completion_time() -> None:
    with pytest.raises(ValidationError, match="timezone"):
        ReportExecutionResult.model_validate(
            _result_payload(completed_at="2026-10-22T00:00:00")
        )


def test_result_rejects_repeated_requested_aggregates() -> None:
    with pytest.raises(ValidationError, match="unique"):
        ReportExecutionResult.model_validate(
            _result_payload(requested_aggregates=["budget", "budget"])
        )


def test_result_rejects_requested_aggregates_that_differ_from_the_output_plan() -> None:
    # The coordinator builds the output plan from the aggregates the report
    # requested, so a result naming other aggregates contradicts its own plan.
    with pytest.raises(ValidationError, match="requested aggregates must match"):
        ReportExecutionResult.model_validate(
            _result_payload(requested_aggregates=["budget"])
        )


def test_result_rejects_requested_aggregates_in_another_order_than_the_plan() -> None:
    reordered = [aggregate_name.value for aggregate_name in reversed(ReportAggregate)]

    with pytest.raises(ValidationError, match="requested aggregates must match"):
        ReportExecutionResult.model_validate(
            _result_payload(requested_aggregates=reordered)
        )


def test_result_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        ReportExecutionResult.model_validate(
            _result_payload(aggregate_output_uri="gs://private/aggregate.json")
        )


def test_failure_bounds_its_code_and_summary() -> None:
    with pytest.raises(ValidationError):
        ReportExecutionFailure(
            evaluation_id=EVALUATION_ID,
            error_code="c" * 65,
            error_summary="Simulation failed",
        )
    with pytest.raises(ValidationError):
        ReportExecutionFailure(
            evaluation_id=EVALUATION_ID,
            error_code="simulation_execution_failed",
            error_summary="s" * 513,
        )


def test_error_pickles_its_failure_as_plain_json_values() -> None:
    # The error crosses Modal as a pickle. Plain JSON values in it mean the
    # container's and the caller's pydantic versions need not agree.
    pickled = pickle.dumps(ReportExecutionError(_failure()))

    assert b"ReportExecutionFailure" not in pickled
    assert b"pydantic" not in pickled


def test_error_keeps_a_fixed_message_and_its_failure_through_pickle() -> None:
    restored = pickle.loads(pickle.dumps(ReportExecutionError(_failure())))

    assert isinstance(restored, ReportExecutionError)
    assert restored.failure == _failure()
    assert str(restored) == "Report execution failed"
