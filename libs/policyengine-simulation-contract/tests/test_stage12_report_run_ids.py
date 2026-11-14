"""The report run and simulation runs a Stage 12 call records its outcome on."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
from policyengine_simulation_contract.stage12_execution import (
    ReportRunIds,
    SimulationRole,
    Stage12InvocationContext,
)
from pydantic import ValidationError

NOW = datetime(2026, 11, 3, tzinfo=UTC)
REPORT_RUN_ID = UUID("00000000-0000-0000-0000-0000000000a1")
BASELINE_RUN_ID = UUID("00000000-0000-0000-0000-0000000000b1")
REFORM_RUN_ID = UUID("00000000-0000-0000-0000-0000000000c1")


def _context_fields(**overrides: Any) -> dict[str, Any]:
    return {
        "request_id": "request-1",
        "environment": "staging",
        "modal_environment": "staging",
        "worker_version": "6.12.0",
        "modal_application": "policyengine-simulation-v2-py6-12-0",
        "simulation_callable": "run_single_simulation_us",
        "version_manifest_sha256": "a" * 64,
        "bundle_manifest_sha256": "b" * 64,
        "artifact_prefix": "stage-12-runs/staging/2026/11/report",
        "created_at": NOW,
        "retention_expires_at": NOW + timedelta(days=30),
        **overrides,
    }


def _run_ids() -> ReportRunIds:
    return ReportRunIds(
        report_run_id=REPORT_RUN_ID,
        baseline_simulation_run_id=BASELINE_RUN_ID,
        reform_simulation_run_id=REFORM_RUN_ID,
    )


def test_context_without_run_ids_keeps_its_comparison_fields() -> None:
    context = Stage12InvocationContext(**_context_fields())

    assert context.report_run is None
    assert "report_run" not in context.model_dump(mode="json", exclude_none=True)


def test_context_carries_the_report_run_and_both_simulation_runs() -> None:
    context = Stage12InvocationContext(
        **_context_fields(report_run=_run_ids().model_dump(mode="json"))
    )

    assert context.report_run == _run_ids()
    restored = Stage12InvocationContext.model_validate(context.model_dump(mode="json"))
    assert restored.report_run == _run_ids()


def test_each_role_names_its_own_simulation_run() -> None:
    run_ids = _run_ids()

    assert run_ids.simulation_run_id(SimulationRole.BASELINE) == BASELINE_RUN_ID
    assert run_ids.simulation_run_id(SimulationRole.REFORM) == REFORM_RUN_ID
    with pytest.raises(ValueError, match="standalone"):
        run_ids.simulation_run_id(SimulationRole.STANDALONE)


@pytest.mark.parametrize(
    "repeated_ids",
    [
        {"baseline_simulation_run_id": REPORT_RUN_ID},
        {"reform_simulation_run_id": REPORT_RUN_ID},
        {
            "baseline_simulation_run_id": REPORT_RUN_ID,
            "reform_simulation_run_id": REPORT_RUN_ID,
        },
    ],
)
def test_the_report_run_must_differ_from_its_simulation_runs(repeated_ids) -> None:
    fields = {**_run_ids().model_dump(), **repeated_ids}

    with pytest.raises(ValidationError, match="differ from its simulation runs"):
        ReportRunIds(**fields)


def test_one_simulation_run_may_be_both_the_baseline_and_the_reform() -> None:
    # A report comparing a simulation with itself names that simulation's run
    # on both sides, since a simulation has one unfailed run per version set.
    run_ids = ReportRunIds(
        report_run_id=REPORT_RUN_ID,
        baseline_simulation_run_id=BASELINE_RUN_ID,
        reform_simulation_run_id=BASELINE_RUN_ID,
    )

    assert run_ids.names_one_simulation_run
    assert run_ids.simulation_run_id(SimulationRole.REFORM) == BASELINE_RUN_ID
    assert not _run_ids().names_one_simulation_run


def test_a_shadow_comparison_run_cannot_record_report_runs() -> None:
    with pytest.raises(ValidationError, match="production function call"):
        Stage12InvocationContext(
            **_context_fields(
                production_function_call_id="fc-production",
                report_run=_run_ids().model_dump(mode="json"),
            )
        )


def test_a_workers_context_names_the_simulation_run_its_call_claims() -> None:
    context = Stage12InvocationContext(
        **_context_fields(simulation_run_id=str(BASELINE_RUN_ID))
    )

    assert context.simulation_run_id == BASELINE_RUN_ID
    assert context.report_run is None
    assert context.records_runs
    restored = Stage12InvocationContext.model_validate(context.model_dump(mode="json"))
    assert restored.simulation_run_id == BASELINE_RUN_ID


def test_only_a_detailed_save_call_records_runs() -> None:
    comparison = Stage12InvocationContext(**_context_fields())
    coordinator = Stage12InvocationContext(
        **_context_fields(report_run=_run_ids().model_dump(mode="json"))
    )

    assert comparison.simulation_run_id is None
    assert not comparison.records_runs
    assert coordinator.records_runs
    # A comparison context's JSON is unchanged, for the workers deployed now.
    assert "simulation_run_id" not in comparison.model_dump(
        mode="json", exclude_none=True
    )


def test_a_context_names_a_report_run_or_a_simulation_run_not_both() -> None:
    with pytest.raises(ValidationError, match="report run or a simulation run"):
        Stage12InvocationContext(
            **_context_fields(
                report_run=_run_ids().model_dump(mode="json"),
                simulation_run_id=str(BASELINE_RUN_ID),
            )
        )


def test_a_shadow_comparison_run_cannot_record_a_simulation_run() -> None:
    with pytest.raises(ValidationError, match="production function call"):
        Stage12InvocationContext(
            **_context_fields(
                production_function_call_id="fc-production",
                simulation_run_id=str(BASELINE_RUN_ID),
            )
        )
