"""Typed context and child request helpers for budget-window batches."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from src.modal.gateway.models import BudgetWindowBatchRequest, PolicyEngineBundle

BATCH_ONLY_FIELDS = {
    "version",
    "start_year",
    "window_size",
    "max_parallel",
    "target",
    "_metadata",
    "_telemetry",
}


@dataclass(frozen=True)
class BudgetWindowBatchContext:
    """Resolved parent batch execution context."""

    batch_job_id: str
    request: BudgetWindowBatchRequest
    resolved_version: str
    resolved_app_name: str
    bundle: PolicyEngineBundle
    raw_params: dict[str, Any]


@dataclass(frozen=True)
class ChildSimulationRequest:
    """Expanded single-year child simulation request."""

    simulation_year: str
    payload: dict[str, Any]


@dataclass
class ChildSimulationHandle:
    """Tracked child job handle for a single simulation year."""

    simulation_year: str
    job_id: str
    call: Any | None = None


def build_batch_context(
    params: dict[str, Any],
    *,
    batch_job_id: str,
) -> BudgetWindowBatchContext:
    request = BudgetWindowBatchRequest.model_validate(params)
    metadata = params.get("_metadata")
    if not isinstance(metadata, dict):
        raise ValueError("Missing internal batch metadata")

    resolved_app_name = metadata.get("resolved_app_name")
    resolved_version = metadata.get("resolved_version")
    bundle_payload = metadata.get("policyengine_bundle")

    if not isinstance(resolved_app_name, str) or not resolved_app_name:
        raise ValueError("Missing resolved_app_name in batch metadata")
    if not isinstance(resolved_version, str) or not resolved_version:
        raise ValueError("Missing resolved_version in batch metadata")
    if not isinstance(bundle_payload, dict):
        raise ValueError("Missing policyengine_bundle in batch metadata")

    return BudgetWindowBatchContext(
        batch_job_id=batch_job_id,
        request=request,
        resolved_version=resolved_version,
        resolved_app_name=resolved_app_name,
        bundle=PolicyEngineBundle.model_validate(bundle_payload),
        raw_params=params,
    )


def build_child_simulation_request(
    context: BudgetWindowBatchContext,
    *,
    simulation_year: str,
) -> ChildSimulationRequest:
    payload = {
        key: value
        for key, value in context.raw_params.items()
        if key not in BATCH_ONLY_FIELDS
    }
    payload["time_period"] = simulation_year

    telemetry = context.raw_params.get("_telemetry")
    if isinstance(telemetry, dict):
        payload["_telemetry"] = telemetry

    return ChildSimulationRequest(
        simulation_year=simulation_year,
        payload=payload,
    )
