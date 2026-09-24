"""Shared HTTP responses for gateway endpoints."""

from __future__ import annotations

from fastapi.responses import JSONResponse

from policyengine_simulation_contract.gateway_models import (
    BudgetWindowBatchStatusResponse,
)


class ServerErrorResponse(JSONResponse):
    """Shared 500 JSON response."""

    def __init__(self, content: dict):
        super().__init__(status_code=500, content=content)


def batch_status_payload(response: BudgetWindowBatchStatusResponse) -> dict:
    payload = response.model_dump(mode="json")
    if payload.get("errors") is None:
        payload.pop("errors", None)
    for child in payload.get("child_jobs", {}).values():
        if child.get("errors") is None:
            child.pop("errors", None)
    if response.policyengine_bundle is not None:
        payload["policyengine_bundle"] = response.policyengine_bundle.model_dump(
            mode="json",
            exclude_none=True,
        )
    return payload


def batch_status_response(
    response: BudgetWindowBatchStatusResponse,
    *,
    headers: dict[str, str] | None = None,
):
    payload = batch_status_payload(response)
    if response.status in {"submitted", "running"}:
        return JSONResponse(status_code=202, content=payload, headers=headers)
    if response.status == "failed":
        return JSONResponse(
            status_code=400 if response.errors else 500,
            content=payload,
            headers=headers,
        )
    return JSONResponse(status_code=200, content=payload, headers=headers)


def running_job_response(
    job_metadata: dict | None = None,
    *,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    return JSONResponse(
        status_code=202,
        content={
            "status": "running",
            "result": None,
            "error": None,
            **(job_metadata or {}),
        },
        headers=headers,
    )


def failed_job_response(
    *,
    error: str,
    job_metadata: dict | None = None,
    errors: list[dict] | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    return JSONResponse(
        status_code=400 if errors else 500,
        content={
            **({"errors": errors} if errors else {}),
            "status": "failed",
            "result": None,
            "error": error,
            **(job_metadata or {}),
        },
        headers=headers,
    )
