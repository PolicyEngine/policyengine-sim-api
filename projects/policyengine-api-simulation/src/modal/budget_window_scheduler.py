"""Scheduler for budget-window child simulation batches."""

from __future__ import annotations

import time
from typing import Any

import modal
from policyengine_observability import segment, set_attribute

from src.modal.budget_window_context import (
    BudgetWindowBatchContext,
    ChildSimulationHandle,
    build_child_simulation_request,
)
from src.modal.budget_window_results import (
    build_budget_window_result,
    extract_annual_impact,
)
from src.modal.budget_window_state import (
    build_batch_status_response,
    create_initial_batch_state,
    get_batch_job_seed,
    mark_batch_complete,
    mark_batch_failed,
    mark_batch_running,
    mark_child_completed,
    mark_child_failed,
    mark_child_started,
    put_batch_job_seed,
    put_batch_job_state,
)
from src.modal.gateway.errors import log_and_redact_exception
from policyengine_api_simulation.observability import SegmentName

# Polling tuning. The runner busy-loops across child FunctionCall.get(timeout=0)
# probes; when no child resolved we sleep before the next probe to stop the
# Modal control-plane from getting hammered. We start aggressive (0.5s) so
# fast child runs don't inflate end-to-end latency, then double up to 30s so a
# sluggish child doesn't keep the parent container hot polling. A blocking
# FunctionCall.get(timeout=...) would be even better, but its interaction with
# max_parallel means we'd have to juggle per-year deadlines and give up early
# termination on child failure; the exponential walk keeps the control flow
# simple while matching Modal's recommended polling cadence.
POLL_INTERVAL_INITIAL_SECONDS = 0.5
POLL_INTERVAL_MAX_SECONDS = 30.0
POLL_INTERVAL_BACKOFF_FACTOR = 2.0
# Retained for backward compatibility with callers that imported the original
# constant; new code should use the initial/max pair above.
POLL_INTERVAL_SECONDS = POLL_INTERVAL_INITIAL_SECONDS


def serialize_batch_status(state) -> dict[str, Any]:
    with segment(SegmentName.BUDGET_WINDOW_STATUS_SERIALIZATION):
        return build_batch_status_response(state).model_dump(mode="json")


def load_or_create_batch_state(context: BudgetWindowBatchContext):
    with segment(SegmentName.BUDGET_WINDOW_STATE_LOAD):
        state = get_batch_job_seed(context.batch_job_id)
    if state is None:
        state = create_initial_batch_state(
            batch_job_id=context.batch_job_id,
            request=context.request,
            resolved_version=context.resolved_version,
            resolved_app_name=context.resolved_app_name,
            bundle=context.bundle,
        )
        with segment(SegmentName.BUDGET_WINDOW_STATE_WRITE):
            put_batch_job_seed(state)
    return state


class BudgetWindowBatchRunner:
    """Runs a parent budget-window batch job to completion."""

    def __init__(
        self,
        context: BudgetWindowBatchContext,
        *,
        modal_module=None,
        poll_interval_seconds: float = POLL_INTERVAL_INITIAL_SECONDS,
        poll_interval_max_seconds: float = POLL_INTERVAL_MAX_SECONDS,
        poll_interval_backoff_factor: float = POLL_INTERVAL_BACKOFF_FACTOR,
    ):
        self.context = context
        self.modal = modal if modal_module is None else modal_module
        self.poll_interval_initial_seconds = poll_interval_seconds
        self.poll_interval_max_seconds = poll_interval_max_seconds
        self.poll_interval_backoff_factor = poll_interval_backoff_factor
        # Kept for tests that still read this attribute.
        self.poll_interval_seconds = poll_interval_seconds
        set_attribute("batch_job_id", context.batch_job_id)
        set_attribute("resolved_app_name", context.resolved_app_name)
        set_attribute("resolved_version", context.resolved_version)
        self.state = load_or_create_batch_state(context)
        with segment(SegmentName.MODAL_FUNCTION_LOOKUP):
            self.child_func = self.modal.Function.from_name(
                context.resolved_app_name,
                "run_simulation",
            )
        self.child_handles: dict[str, ChildSimulationHandle] = {}

    def run(self) -> dict[str, Any]:
        mark_batch_running(self.state)
        with segment(SegmentName.BUDGET_WINDOW_STATE_WRITE):
            put_batch_job_state(self.state)

        # Exponential backoff: reset on any progress, double on empty polls.
        current_sleep = self.poll_interval_initial_seconds

        while self.has_pending_work():
            self.spawn_until_capacity()
            progress_made = self.poll_running_children_once()
            if self.state.status == "failed":
                return serialize_batch_status(self.state)
            if self.state.running_years and not progress_made:
                with segment(
                    SegmentName.BUDGET_WINDOW_BACKOFF_SLEEP,
                    sleep_seconds=current_sleep,
                ):
                    time.sleep(current_sleep)
                current_sleep = min(
                    current_sleep * self.poll_interval_backoff_factor,
                    self.poll_interval_max_seconds,
                )
            elif progress_made:
                current_sleep = self.poll_interval_initial_seconds

        return self.complete_batch()

    def has_pending_work(self) -> bool:
        return bool(self.state.queued_years or self.state.running_years)

    def spawn_until_capacity(self) -> None:
        while (
            len(self.state.running_years) < self.state.max_parallel
            and self.state.queued_years
        ):
            simulation_year = self.state.queued_years[0]
            with segment(
                SegmentName.BUDGET_WINDOW_CHILD_REQUEST_BUILD,
                simulation_year=simulation_year,
            ):
                child_request = build_child_simulation_request(
                    self.context,
                    simulation_year=simulation_year,
                )
            with segment(
                SegmentName.BUDGET_WINDOW_CHILD_SPAWN,
                simulation_year=simulation_year,
            ):
                call = self.child_func.spawn(child_request.payload)
            self.child_handles[simulation_year] = ChildSimulationHandle(
                simulation_year=simulation_year,
                job_id=call.object_id,
                call=call,
            )
            mark_child_started(
                self.state,
                year=simulation_year,
                child_job_id=call.object_id,
            )
            with segment(SegmentName.BUDGET_WINDOW_STATE_WRITE):
                put_batch_job_state(self.state)

    def poll_running_children_once(self) -> bool:
        progress_made = False

        for simulation_year in list(self.state.running_years):
            handle = self.resolve_child_handle(simulation_year)

            try:
                with segment(
                    SegmentName.BUDGET_WINDOW_CHILD_POLL,
                    simulation_year=simulation_year,
                ):
                    child_result = handle.call.get(timeout=0)
            except TimeoutError:
                continue
            except Exception as exc:
                redacted = log_and_redact_exception(
                    exc,
                    scope="budget_window_child_call",
                    context={
                        "batch_job_id": self.context.batch_job_id,
                        "simulation_year": simulation_year,
                    },
                )
                self.fail_batch_for_child_error(
                    simulation_year=simulation_year,
                    error=redacted,
                )
                return False

            try:
                with segment(
                    SegmentName.BUDGET_WINDOW_RESULT_PARSE,
                    simulation_year=simulation_year,
                ):
                    annual_impact = extract_annual_impact(
                        simulation_year=simulation_year,
                        child_result=child_result,
                    )
            except Exception as exc:
                redacted = log_and_redact_exception(
                    exc,
                    scope="budget_window_child_result_parsing",
                    context={
                        "batch_job_id": self.context.batch_job_id,
                        "simulation_year": simulation_year,
                    },
                )
                self.fail_batch_for_child_error(
                    simulation_year=simulation_year,
                    error=redacted,
                )
                return False

            mark_child_completed(
                self.state,
                year=simulation_year,
                annual_impact=annual_impact,
            )
            with segment(SegmentName.BUDGET_WINDOW_STATE_WRITE):
                put_batch_job_state(self.state)
            progress_made = True

        return progress_made

    def resolve_child_handle(self, simulation_year: str) -> ChildSimulationHandle:
        handle = self.child_handles.get(simulation_year)
        if handle is not None and handle.call is not None:
            return handle

        job_id = self.state.child_jobs[simulation_year].job_id
        with segment(
            SegmentName.BUDGET_WINDOW_CHILD_POLL,
            simulation_year=simulation_year,
        ):
            call = self.modal.FunctionCall.from_id(job_id)
        resolved_handle = ChildSimulationHandle(
            simulation_year=simulation_year,
            job_id=job_id,
            call=call,
        )
        self.child_handles[simulation_year] = resolved_handle
        return resolved_handle

    def fail_batch_for_child_error(
        self,
        *,
        simulation_year: str,
        error: str,
    ) -> None:
        mark_child_failed(self.state, year=simulation_year, error=error)
        mark_batch_failed(self.state, error=error)
        with segment(SegmentName.BUDGET_WINDOW_STATE_WRITE):
            put_batch_job_state(self.state)

    def complete_batch(self) -> dict[str, Any]:
        with segment(SegmentName.BUDGET_WINDOW_AGGREGATION):
            annual_impacts = [
                self.state.partial_annual_impacts[simulation_year]
                for simulation_year in self.state.years
                if simulation_year in self.state.partial_annual_impacts
            ]
            result = build_budget_window_result(
                start_year=self.state.start_year,
                window_size=self.state.window_size,
                annual_impacts=annual_impacts,
            )
        mark_batch_complete(self.state, result=result)
        with segment(SegmentName.BUDGET_WINDOW_STATE_WRITE):
            put_batch_job_state(self.state)
        return serialize_batch_status(self.state)
