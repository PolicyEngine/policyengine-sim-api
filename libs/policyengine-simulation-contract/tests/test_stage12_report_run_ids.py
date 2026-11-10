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
        {"reform_simulation_run_id": BASELINE_RUN_ID},
    ],
)
def test_run_ids_must_name_three_different_runs(repeated_ids) -> None:
    fields = {**_run_ids().model_dump(), **repeated_ids}

    with pytest.raises(ValidationError, match="three different runs"):
        ReportRunIds(**fields)


def test_a_shadow_comparison_run_cannot_record_report_runs() -> None:
    with pytest.raises(ValidationError, match="production function call"):
        Stage12InvocationContext(
            **_context_fields(
                production_function_call_id="fc-production",
                report_run=_run_ids().model_dump(mode="json"),
            )
        )
