"""Helpers for budget-window batch job state."""

from __future__ import annotations

import logging
from datetime import UTC, datetime

import modal

from policyengine_simulation_contract.gateway_models import (
    BatchChildJobStatus,
    BudgetWindowAnnualImpact,
    BudgetWindowBatchRequest,
    BudgetWindowBatchState,
    BudgetWindowBatchStatusResponse,
    BudgetWindowResult,
    PolicyEngineBundle,
)

logger = logging.getLogger(__name__)

_UNKNOWN_CHILD_JOB_ID = "unknown"

BUDGET_WINDOW_JOB_DICT_NAME = "simulation-api-budget-window-jobs"
BUDGET_WINDOW_JOB_SEED_DICT_NAME = "simulation-api-budget-window-job-seeds"


def _budget_window_job_store() -> modal.Dict:
    return modal.Dict.from_name(BUDGET_WINDOW_JOB_DICT_NAME, create_if_missing=True)


def _budget_window_job_seed_store() -> modal.Dict:
    return modal.Dict.from_name(
        BUDGET_WINDOW_JOB_SEED_DICT_NAME, create_if_missing=True
    )


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _build_years(start_year: str, window_size: int) -> list[str]:
    base_year = int(start_year)
    return [str(base_year + offset) for offset in range(window_size)]


def _touch(state: BudgetWindowBatchState) -> BudgetWindowBatchState:
    state.updated_at = _utc_now_iso()
    return state


def create_initial_batch_state(  # noqa: PLR0913  # public contract API: the gateway and the executor's batch scheduler call it with these keywords
    *,
    batch_job_id: str,
    request: BudgetWindowBatchRequest,
    resolved_version: str,
    resolved_app_name: str,
    bundle: PolicyEngineBundle,
    observability_id: str | None = None,
) -> BudgetWindowBatchState:
    """Build the ``submitted`` state for a new batch, with every year queued.

    The stored request payload omits ``telemetry``, and all timestamps are set
    to the current UTC time.
    """
    years = _build_years(request.start_year, request.window_size)
    now = _utc_now_iso()

    return BudgetWindowBatchState(
        batch_job_id=batch_job_id,
        status="submitted",
        country=request.country,
        region=request.region,
        version=resolved_version,
        target=request.target,
        resolved_app_name=resolved_app_name,
        policyengine_bundle=bundle,
        start_year=request.start_year,
        window_size=request.window_size,
        max_parallel=request.max_parallel,
        request_payload=request.model_dump(exclude={"telemetry"}, mode="json"),
        years=years,
        queued_years=list(years),
        running_years=[],
        completed_years=[],
        failed_years=[],
        child_jobs={},
        partial_annual_impacts={},
        result=None,
        error=None,
        created_at=now,
        updated_at=now,
        observability_id=observability_id,
    )


def get_batch_job_seed(batch_job_id: str) -> BudgetWindowBatchState | None:
    """Load the submit-time seed state from its Modal Dict, or None if absent.

    The seed is written when the batch is submitted, before the batch runner
    writes live progress, so pollers fall back to it while no live state exists.
    """
    payload = _budget_window_job_seed_store().get(batch_job_id)
    if payload is None:
        return None
    return BudgetWindowBatchState.model_validate(payload)


def put_batch_job_seed(state: BudgetWindowBatchState) -> None:
    """Store ``state`` as the batch's seed in the seed Modal Dict."""
    _budget_window_job_seed_store()[state.batch_job_id] = state.model_dump(mode="json")


def get_batch_job_state(batch_job_id: str) -> BudgetWindowBatchState | None:
    """Load the live state the batch runner last wrote, or None if none yet."""
    payload = _budget_window_job_store().get(batch_job_id)
    if payload is None:
        return None
    return BudgetWindowBatchState.model_validate(payload)


def put_batch_job_state(state: BudgetWindowBatchState) -> None:
    """Store ``state`` as the batch's live state in the job Modal Dict."""
    serialized = state.model_dump(mode="json")
    _budget_window_job_store()[state.batch_job_id] = serialized


def mark_batch_running(state: BudgetWindowBatchState) -> BudgetWindowBatchState:
    """Set the batch status to ``running`` in place and return the same state."""
    state.status = "running"
    return _touch(state)


def mark_child_started(
    state: BudgetWindowBatchState,
    *,
    year: str,
    child_job_id: str,
) -> BudgetWindowBatchState:
    """Move ``year`` from queued to running and record its child job id.

    Mutates ``state`` in place, is safe to repeat, and returns the same object.
    """
    if year in state.queued_years:
        state.queued_years.remove(year)
    if year not in state.running_years:
        state.running_years.append(year)

    state.child_jobs[year] = BatchChildJobStatus(
        job_id=child_job_id,
        status="running",
    )
    return _touch(state)


def _existing_child_or_sentinel(
    state: BudgetWindowBatchState, *, year: str
) -> BatchChildJobStatus:
    """Return the tracked child for ``year`` or synthesise a sentinel.

    Callers (``mark_child_completed`` / ``mark_child_failed``) used to index
    ``state.child_jobs[year]`` directly which would raise ``KeyError`` if
    transition helpers were invoked out of order (e.g., after recovery from
    a dropped ``mark_child_started`` due to a crash between spawn and seed
    persistence). In that unusual case we'd rather surface a redacted
    terminal state with a synthetic job id than abort the whole batch. The
    anomaly is logged at WARNING so operators can investigate separately.
    """
    child = state.child_jobs.get(year)
    if child is not None:
        return child

    logger.warning(
        "Transitioning child state for year %s with no prior child_jobs entry;"
        " synthesising a sentinel job id",
        year,
        extra={"year": year, "batch_job_id": state.batch_job_id},
    )
    sentinel = BatchChildJobStatus(job_id=_UNKNOWN_CHILD_JOB_ID, status="pending")
    state.child_jobs[year] = sentinel
    return sentinel


def mark_child_completed(
    state: BudgetWindowBatchState,
    *,
    year: str,
    annual_impact: BudgetWindowAnnualImpact,
) -> BudgetWindowBatchState:
    """Move ``year`` from running to completed and keep its annual impact.

    Mutates ``state`` in place and returns it. A year with no tracked child job
    gets a sentinel job id rather than raising.
    """
    if year in state.running_years:
        state.running_years.remove(year)
    if year not in state.completed_years:
        state.completed_years.append(year)

    child = _existing_child_or_sentinel(state, year=year)
    state.child_jobs[year] = BatchChildJobStatus(
        job_id=child.job_id,
        status="complete",
    )
    state.partial_annual_impacts[year] = annual_impact
    return _touch(state)


def mark_child_failed(
    state: BudgetWindowBatchState,
    *,
    year: str,
    error: str,
) -> BudgetWindowBatchState:
    """Move ``year`` from running to failed and record ``error`` on its child.

    Mutates ``state`` in place and returns it; the batch itself keeps its
    status. A year with no tracked child job gets a sentinel job id.
    """
    if year in state.running_years:
        state.running_years.remove(year)
    if year not in state.failed_years:
        state.failed_years.append(year)

    child = _existing_child_or_sentinel(state, year=year)
    state.child_jobs[year] = BatchChildJobStatus(
        job_id=child.job_id,
        status="failed",
        error=error,
    )
    return _touch(state)


def mark_batch_complete(
    state: BudgetWindowBatchState,
    *,
    result: BudgetWindowResult,
) -> BudgetWindowBatchState:
    """Mark the batch complete with ``result``, clearing any error and pending years."""
    state.status = "complete"
    state.result = result
    state.error = None
    state.running_years = []
    state.queued_years = []
    return _touch(state)


def mark_batch_failed(
    state: BudgetWindowBatchState,
    *,
    error: str,
) -> BudgetWindowBatchState:
    """Mark the batch failed and cancel every child that is still running.

    Cancelled children carry the batch ``error``; queued years are left as they
    are. Mutates ``state`` in place and returns it.
    """
    for year in list(state.running_years):
        child = state.child_jobs.get(year)
        if child is None:
            continue
        state.child_jobs[year] = BatchChildJobStatus(
            job_id=child.job_id,
            status="cancelled",
            error=error,
        )

    state.status = "failed"
    state.error = error
    state.running_years = []
    return _touch(state)


def build_batch_status_response(
    state: BudgetWindowBatchState,
) -> BudgetWindowBatchStatusResponse:
    """Project stored batch state onto the public polling response.

    ``progress`` is the whole-number percentage of years completed (0 for an
    empty window); failed years do not count towards it.
    """
    total_years = len(state.years)
    progress = (
        0 if total_years == 0 else round(len(state.completed_years) / total_years * 100)
    )

    return BudgetWindowBatchStatusResponse(
        status=state.status,
        progress=progress,
        completed_years=state.completed_years,
        running_years=state.running_years,
        queued_years=state.queued_years,
        failed_years=state.failed_years,
        child_jobs=state.child_jobs,
        result=state.result,
        error=state.error,
        errors=state.errors,
        resolved_app_name=state.resolved_app_name,
        policyengine_bundle=state.policyengine_bundle,
    )
