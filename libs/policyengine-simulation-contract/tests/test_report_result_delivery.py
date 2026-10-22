"""Tests for how a report coordinator is told to deliver its outcome."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
from policyengine_simulation_contract.stage12_execution import (
    ReportResultDelivery,
    Stage12InvocationContext,
)
from pydantic import ValidationError

CREATED_AT = datetime(2026, 10, 22, tzinfo=UTC)
EVALUATION_ID = UUID("00000000-0000-0000-0000-000000000001")


def _entrypoint_context_payload(**overrides: Any) -> dict[str, Any]:
    """Return the dispatch context as the Simulation Entrypoint builds it today."""
    payload: dict[str, Any] = {
        "request_id": "request-1",
        "environment": "staging",
        "modal_environment": "staging",
        "worker_version": "6.7.5",
        "modal_application": "policyengine-simulation-v2-py6-7-5",
        "simulation_callable": "run_single_simulation_us",
        "version_manifest_sha256": "d" * 64,
        "bundle_manifest_sha256": "b" * 64,
        "artifact_prefix": f"stage-12-runs/staging/2026/10/{EVALUATION_ID}",
        "production_function_call_id": None,
        "created_at": CREATED_AT.isoformat(),
        "retention_expires_at": (CREATED_AT + timedelta(days=30)).isoformat(),
    }
    payload.update(overrides)
    return payload


def test_a_context_without_a_delivery_keeps_the_comparison_path_storage() -> None:
    context = Stage12InvocationContext.model_validate(_entrypoint_context_payload())

    assert context.result_delivery is ReportResultDelivery.STAGE12_DATABASE


def test_a_context_can_ask_for_the_report_to_return_to_its_caller() -> None:
    context = Stage12InvocationContext.model_validate(
        _entrypoint_context_payload(result_delivery="return_to_caller")
    )

    assert context.result_delivery is ReportResultDelivery.RETURN_TO_CALLER


def test_a_returned_report_cannot_request_a_production_comparison() -> None:
    # The comparison with a production result is recorded on the comparison
    # path's report row, which a returned report never writes.
    with pytest.raises(ValidationError, match="production comparison"):
        Stage12InvocationContext.model_validate(
            _entrypoint_context_payload(
                result_delivery="return_to_caller",
                production_function_call_id="fc-production",
            )
        )


def test_a_comparison_path_context_may_still_name_its_production_job() -> None:
    context = Stage12InvocationContext.model_validate(
        _entrypoint_context_payload(production_function_call_id="fc-production")
    )

    assert context.production_function_call_id == "fc-production"
    assert context.result_delivery is ReportResultDelivery.STAGE12_DATABASE


def test_a_context_rejects_an_unknown_delivery() -> None:
    with pytest.raises(ValidationError):
        Stage12InvocationContext.model_validate(
            _entrypoint_context_payload(result_delivery="object_storage")
        )
