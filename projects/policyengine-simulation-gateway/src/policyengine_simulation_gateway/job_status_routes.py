"""The routes that report a simulation or budget-window job's status."""

import modal
from fastapi import Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from policyengine_observability import ObservabilityRuntime
from policyengine_simulation_contract.budget_window_state import (
    build_batch_status_response,
    get_batch_job_seed,
    get_batch_job_state,
    put_batch_job_seed,
    put_batch_job_state,
)
from policyengine_simulation_contract.gateway_models import (
    BudgetWindowBatchStatusResponse,
    JobStatusResponse,
)
from policyengine_simulation_contract.job_progress import (
    JOB_PROGRESS_CAPABILITY_KEY,
    JobStatus,
    job_progress_fields,
    supported_job_progress_capability,
)
from policyengine_simulation_contract.job_progress_state import get_job_progress
from policyengine_simulation_contract.spm import spm_error_detail
from policyengine_simulation_observability.errors import log_and_redact_exception
from policyengine_simulation_observability.identifiers import OBSERVABILITY_ID_HEADER
from policyengine_simulation_observability.stages import (
    ANNUAL_IMPACT_STAGES,
    BUDGET_WINDOW_STAGES,
    Stage,
)

from policyengine_simulation_gateway.auth import require_auth
from policyengine_simulation_gateway.modal_routing import (
    is_modal_job_not_found,
    job_metadata_store,
)
from policyengine_simulation_gateway.responses import (
    batch_status_response,
    failed_job_response,
    running_job_response,
)
from policyengine_simulation_gateway.routes_common import (
    bind_observability_id,
    logger,
    observability_headers,
    observability_runtime,
    record_not_found,
    router,
)

# Job metadata keys that the status response never echoes directly.
_INTERNAL_JOB_METADATA_KEYS = frozenset(
    {"observability_id", "submitted_at", JOB_PROGRESS_CAPABILITY_KEY}
)


def _public_job_metadata(job_metadata: dict | None) -> dict:
    if not isinstance(job_metadata, dict):
        return {}
    return {
        key: value
        for key, value in job_metadata.items()
        if key not in _INTERNAL_JOB_METADATA_KEYS
    }


def _job_progress_payload(
    runtime: ObservabilityRuntime,
    job_id: str,
    job_metadata: dict,
    status: JobStatus,
) -> dict:
    """Return the optional stage fields for a status response.

    Stage reporting never changes the status, the result or the HTTP code: a
    failed or malformed read drops the stage fields instead of guessing.
    """
    reports_stages = (
        supported_job_progress_capability(job_metadata.get(JOB_PROGRESS_CAPABILITY_KEY))
        is not None
    )
    record = None
    if reports_stages:
        try:
            with runtime.span(ANNUAL_IMPACT_STAGES.name(Stage.MODAL_JOB_PROGRESS_READ)):
                record = get_job_progress(job_id)
        except Exception as exc:  # noqa: BLE001  # stage reporting is optional
            logger.warning(
                "Job progress unavailable for %s: %s", job_id, type(exc).__name__
            )
            reports_stages = False
    return job_progress_fields(
        status=status,
        submitted_at=job_metadata.get("submitted_at"),
        record=record,
        reports_stages=reports_stages,
    )


@router.get(
    "/jobs/{job_id}",
    response_model=JobStatusResponse,
    response_model_exclude_none=True,
    dependencies=[Depends(require_auth)],
)
async def get_job_status(
    job_id: str, request: Request, response: Response
) -> JobStatusResponse | JSONResponse:
    """Poll for job status.

    Returns:
        - 200 with status="complete" and result when done
        - 202 with status="running" while in progress
        - 500 with status="failed" and error on failure
        - 404 if job_id not found

    """
    runtime = observability_runtime(request)
    runtime.set_context(job_id=job_id)
    with runtime.span(ANNUAL_IMPACT_STAGES.name(Stage.MODAL_JOB_METADATA_READ)):
        job_metadata = job_metadata_store().get(job_id)
    if job_metadata is None:
        record_not_found(runtime, f"Job not found: {job_id}")
        raise HTTPException(status_code=404, detail=f"Job not found: {job_id}")
    observability_id = bind_observability_id(
        request,
        runtime,
        job_metadata.get("observability_id"),
    )
    if observability_id is not None:
        response.headers[OBSERVABILITY_ID_HEADER] = observability_id
    public_metadata = _public_job_metadata(job_metadata)
    response_headers = observability_headers(observability_id)

    try:
        with runtime.span(ANNUAL_IMPACT_STAGES.name(Stage.MODAL_JOB_STATUS_POLL)):
            call = modal.FunctionCall.from_id(job_id)
    except Exception as exc:
        if is_modal_job_not_found(exc):
            runtime.record_exception(exc, handled=True, status_code=404)
            raise HTTPException(
                status_code=404, detail=f"Job not found: {job_id}"
            ) from exc
        runtime.record_exception(exc, handled=False, status_code=500)
        raise

    def with_progress(status: JobStatus) -> dict:
        return {
            **public_metadata,
            **_job_progress_payload(runtime, job_id, job_metadata, status),
        }

    try:
        with runtime.span(ANNUAL_IMPACT_STAGES.name(Stage.MODAL_JOB_STATUS_POLL)):
            result = call.get(timeout=0)
        complete_metadata = with_progress("complete")
        with runtime.span(ANNUAL_IMPACT_STAGES.name(Stage.RESPONSE_SERIALIZATION)):
            return JobStatusResponse(
                status="complete", result=result, **complete_metadata
            )
    except TimeoutError:
        running_metadata = with_progress("running")
        with runtime.span(ANNUAL_IMPACT_STAGES.name(Stage.RESPONSE_SERIALIZATION)):
            return running_job_response(running_metadata, headers=response_headers)
    # call.get re-raises whatever the worker raised; every failure becomes a
    # failed-job response, logged with its traceback by log_and_redact_exception.
    except Exception as exc:
        if is_modal_job_not_found(exc):
            runtime.record_exception(exc, handled=True, status_code=404)
            raise HTTPException(
                status_code=404, detail=f"Job not found: {job_id}"
            ) from exc
        detail = spm_error_detail(exc)
        if detail:
            return failed_job_response(
                error=detail.message,
                errors=[detail.model_dump()],
                job_metadata=with_progress("failed"),
                headers=response_headers,
            )
        redacted = log_and_redact_exception(
            exc,
            runtime=runtime,
            scope="simulation_job_status",
            context={"job_id": job_id},
        )
        failed_metadata = with_progress("failed")
        with runtime.span(ANNUAL_IMPACT_STAGES.name(Stage.RESPONSE_SERIALIZATION)):
            return failed_job_response(
                error=redacted,
                job_metadata=failed_metadata,
                headers=response_headers,
            )


@router.get(
    "/budget-window-jobs/{batch_job_id}",
    response_model=BudgetWindowBatchStatusResponse,
    response_model_exclude_none=True,
    dependencies=[Depends(require_auth)],
)
async def get_budget_window_job_status(
    batch_job_id: str,
    request: Request,
    response: Response,
) -> JSONResponse:
    """Poll for budget-window batch status."""
    runtime = observability_runtime(request)
    runtime.set_context(batch_job_id=batch_job_id)
    with runtime.span(BUDGET_WINDOW_STAGES.name(Stage.BUDGET_WINDOW_STATE_LOAD)):
        state = get_batch_job_state(batch_job_id)
    if state is not None:
        bind_observability_id(request, runtime, state.observability_id)
        response_headers = observability_headers(state.observability_id)
        if response_headers is not None:
            response.headers.update(response_headers)
        with runtime.span(
            BUDGET_WINDOW_STAGES.name(Stage.BUDGET_WINDOW_STATUS_SERIALIZATION)
        ):
            return batch_status_response(
                build_batch_status_response(state), headers=response_headers
            )

    with runtime.span(BUDGET_WINDOW_STAGES.name(Stage.BUDGET_WINDOW_STATE_LOAD)):
        seed_state = get_batch_job_seed(batch_job_id)
    if seed_state is None:
        record_not_found(runtime, f"Budget-window job not found: {batch_job_id}")
        raise HTTPException(
            status_code=404, detail=f"Budget-window job not found: {batch_job_id}"
        )
    bind_observability_id(request, runtime, seed_state.observability_id)
    response_headers = observability_headers(seed_state.observability_id)
    if response_headers is not None:
        response.headers.update(response_headers)

    try:
        with runtime.span(BUDGET_WINDOW_STAGES.name(Stage.MODAL_JOB_STATUS_POLL)):
            call = modal.FunctionCall.from_id(batch_job_id)
    except Exception as exc:  # noqa: BLE001  # any lookup failure degrades to the seed state; logged below with its traceback at warning level
        # The endpoint degrades gracefully to the seed state (a successful
        # 202/200 response), so this must NOT be recorded as a request
        # error — recording the exception would emit a request failure and
        # an error metric contradicting the status the client actually saw.
        runtime.event(
            "budget_window_parent_lookup_degraded",
            attributes={
                "batch_job_id": batch_job_id,
                "error_type": type(exc).__name__,
                "parent_call_not_found": is_modal_job_not_found(exc),
            },
        )
        logger.warning(
            "Budget-window parent FunctionCall lookup failed; "
            "serving seed state (batch_job_id=%s)",
            batch_job_id,
            exc_info=exc,
        )
        with runtime.span(
            BUDGET_WINDOW_STAGES.name(Stage.BUDGET_WINDOW_STATUS_SERIALIZATION)
        ):
            return batch_status_response(
                build_batch_status_response(seed_state), headers=response_headers
            )

    try:
        with runtime.span(BUDGET_WINDOW_STAGES.name(Stage.MODAL_JOB_STATUS_POLL)):
            result = call.get(timeout=0)
    except TimeoutError:
        with runtime.span(
            BUDGET_WINDOW_STAGES.name(Stage.BUDGET_WINDOW_STATUS_SERIALIZATION)
        ):
            return batch_status_response(
                build_batch_status_response(seed_state), headers=response_headers
            )
    except Exception as exc:  # noqa: BLE001  # call.get re-raises whatever the batch worker raised; every failure is persisted as a failed batch, logged with its traceback by log_and_redact_exception
        # Persist the failure so subsequent polls don't resurrect the
        # "submitted" status from the seed store (#448). We deliberately
        # overwrite the main job store entry as well as the seed so either
        # lookup path observes the terminal failed state.
        detail = spm_error_detail(exc)
        message = (
            detail.message
            if detail
            else log_and_redact_exception(
                exc,
                runtime=runtime,
                scope="budget_window_parent_call",
                context={"batch_job_id": batch_job_id},
            )
        )
        seed_state.status = "failed"
        seed_state.errors = [detail] if detail else None
        seed_state.error = message
        with runtime.span(BUDGET_WINDOW_STAGES.name(Stage.BUDGET_WINDOW_STATE_WRITE)):
            put_batch_job_state(seed_state)
            put_batch_job_seed(seed_state)
        with runtime.span(
            BUDGET_WINDOW_STAGES.name(Stage.BUDGET_WINDOW_STATUS_SERIALIZATION)
        ):
            return batch_status_response(
                build_batch_status_response(seed_state), headers=response_headers
            )

    with runtime.span(BUDGET_WINDOW_STAGES.name(Stage.BUDGET_WINDOW_RESULT_PARSE)):
        status_response = BudgetWindowBatchStatusResponse.model_validate(result)
    with runtime.span(
        BUDGET_WINDOW_STAGES.name(Stage.BUDGET_WINDOW_STATUS_SERIALIZATION)
    ):
        return batch_status_response(status_response, headers=response_headers)
