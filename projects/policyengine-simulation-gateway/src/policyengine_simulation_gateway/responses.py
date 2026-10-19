"""Shared HTTP responses for gateway endpoints."""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi.responses import JSONResponse

if TYPE_CHECKING:
    from policyengine_simulation_contract.gateway_models import (
        BudgetWindowBatchStatusResponse,
    )


class ServerErrorResponse(JSONResponse):
    """Shared 500 JSON response."""

    def __init__(self, content: dict) -> None:
        """Serialize ``content`` as the body of a 500 response."""
        super().__init__(status_code=500, content=content)


def batch_status_payload(response: BudgetWindowBatchStatusResponse) -> dict:
    """Return the batch status as JSON, omitting null ``errors`` fields.

    ``errors`` is dropped at the top level and on each child job when it is
    None, and ``policyengine_bundle`` omits its unset fields.
    """
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
) -> JSONResponse:
    """Return the batch status with the HTTP code the polling contract uses.

    Submitted or running batches get 202, complete ones 200, and failed ones
    400 when they carry structured caller errors, else 500.
    """
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
    """Return a 202 running-job body, merged with the job's public metadata."""
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
    """Return a failed-job body: 400 with structured ``errors``, otherwise 500.

    Pass structured errors only for caller-correctable failures such as an
    invalid SPM selection. ``error`` reaches the caller verbatim, so redact
    exception text first.
    """
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
