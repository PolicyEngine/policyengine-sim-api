"""Scheduler for budget-window child simulation batches."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

from policyengine_simulation_contract.budget_window_state import (
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
from policyengine_simulation_contract.spm import spm_error_detail
from policyengine_simulation_observability.errors import log_and_redact_exception
from policyengine_simulation_observability.stages import BUDGET_WINDOW_STAGES, Stage
from src.modal.budget_window_context import (
    BudgetWindowBatchContext,
    ChildSimulationHandle,
    build_child_simulation_request,
)
from src.modal.budget_window_results import (
    build_budget_window_result,
    extract_annual_impact,
)
from src.modal.fanout import PollBackoff

import modal

if TYPE_CHECKING:
    from types import ModuleType

    from policyengine_observability import ObservabilityRuntime
    from policyengine_simulation_contract.gateway_models import (
        BudgetWindowBatchState,
    )

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
DEFAULT_POLL_BACKOFF = PollBackoff(
    initial_seconds=POLL_INTERVAL_INITIAL_SECONDS,
    maximum_seconds=POLL_INTERVAL_MAX_SECONDS,
    factor=POLL_INTERVAL_BACKOFF_FACTOR,
)


def serialize_batch_status(
    state: BudgetWindowBatchState,
    *,
    runtime: ObservabilityRuntime,
) -> dict[str, Any]:
    """Render ``state`` as the JSON-ready batch status response."""
    with runtime.span(
        BUDGET_WINDOW_STAGES.name(Stage.BUDGET_WINDOW_STATUS_SERIALIZATION)
    ):
        return build_batch_status_response(state).model_dump(mode="json")


def load_or_create_batch_state(
    context: BudgetWindowBatchContext,
    *,
    runtime: ObservabilityRuntime,
) -> BudgetWindowBatchState:
    """Return the batch's submit-time seed state, creating and storing it if absent."""
    with runtime.span(BUDGET_WINDOW_STAGES.name(Stage.BUDGET_WINDOW_STATE_LOAD)):
        state = get_batch_job_seed(context.batch_job_id)
    if state is None:
        state = create_initial_batch_state(
            batch_job_id=context.batch_job_id,
            request=context.request,
            resolved_version=context.resolved_version,
            resolved_app_name=context.resolved_app_name,
            bundle=context.bundle,
        )
        with runtime.span(BUDGET_WINDOW_STAGES.name(Stage.BUDGET_WINDOW_STATE_WRITE)):
            put_batch_job_seed(state)
    return state


class BudgetWindowBatchRunner:
    """Runs a parent budget-window batch job to completion."""

    def __init__(
        self,
        context: BudgetWindowBatchContext,
        *,
        runtime: ObservabilityRuntime,
        modal_module: ModuleType | None = None,
        poll_backoff: PollBackoff = DEFAULT_POLL_BACKOFF,
    ) -> None:
        """Load or seed the batch state and resolve a lazy child-function handle.

        Children run on the resolved app's ``run_simulation`` function.
        ``modal_module`` replaces the ``modal`` module in tests.
        """
        self.context = context
        self.runtime = runtime
        self.modal = modal if modal_module is None else modal_module
        self.poll_interval_initial_seconds = poll_backoff.initial_seconds
        self.poll_interval_max_seconds = poll_backoff.maximum_seconds
        self.poll_interval_backoff_factor = poll_backoff.factor
        # Kept for tests that still read this attribute.
        self.poll_interval_seconds = poll_backoff.initial_seconds
        self.runtime.set_context(
            batch_job_id=context.batch_job_id,
            resolved_app_name=context.resolved_app_name,
            resolved_version=context.resolved_version,
        )
        self.state = load_or_create_batch_state(context, runtime=runtime)
        # ``Function.from_name`` is a lazy handle (no RPC); hydration happens
        # on first spawn, which BUDGET_WINDOW_CHILD_SPAWN already times.
        self.child_func = self.modal.Function.from_name(
            context.resolved_app_name,
            "run_simulation",
        )
        self.child_handles: dict[str, ChildSimulationHandle] = {}
        # Poll/sleep totals are tracked as running aggregates instead of one
        # segment per loop iteration: a segment appends a node to the
        # operation's in-memory segment tree, and a near-timeout batch can
        # poll thousands of times — unbounded memory plus a final operation
        # log line large enough for Modal's log pipeline to truncate.
        # ``set_attribute`` overwrites, so publishing these stays bounded.
        self._child_poll_seconds = 0.0
        self._child_poll_count = 0
        self._backoff_sleep_seconds = 0.0
        self._backoff_sleep_count = 0

    def run(self) -> dict[str, Any]:
        """Spawn and poll children until all finish or one fails; return the status.

        At most ``max_parallel`` children run at once. Polling backs off
        exponentially while no child makes progress, and the first child
        failure fails the whole batch.
        """
        mark_batch_running(self.state)
        with self.runtime.span(
            BUDGET_WINDOW_STAGES.name(Stage.BUDGET_WINDOW_STATE_WRITE)
        ):
            put_batch_job_state(self.state)

        # Exponential backoff: reset on any progress, double on empty polls.
        current_sleep = self.poll_interval_initial_seconds

        while self.has_pending_work():
            self.spawn_until_capacity()
            progress_made = self.poll_running_children_once()
            if self.state.status == "failed":
                return serialize_batch_status(self.state, runtime=self.runtime)
            if self.state.running_years and not progress_made:
                time.sleep(current_sleep)
                self._backoff_sleep_seconds += current_sleep
                self._backoff_sleep_count += 1
                self._publish_poll_stats()
                current_sleep = min(
                    current_sleep * self.poll_interval_backoff_factor,
                    self.poll_interval_max_seconds,
                )
            elif progress_made:
                current_sleep = self.poll_interval_initial_seconds

        return self.complete_batch()

    def has_pending_work(self) -> bool:
        """Return True while any year is queued or running."""
        return bool(self.state.queued_years or self.state.running_years)

    def spawn_until_capacity(self) -> None:
        """Spawn queued years until ``max_parallel`` children are running.

        Each spawn is recorded in the stored batch state before the next one.
        """
        while (
            len(self.state.running_years) < self.state.max_parallel
            and self.state.queued_years
        ):
            simulation_year = self.state.queued_years[0]
            with self.runtime.span(
                BUDGET_WINDOW_STAGES.name(Stage.BUDGET_WINDOW_CHILD_REQUEST_BUILD),
                attributes={"simulation_year": simulation_year},
            ):
                child_request = build_child_simulation_request(
                    self.context,
                    simulation_year=simulation_year,
                )
            observability_context = self.runtime.capture_context()
            with self.runtime.span(
                BUDGET_WINDOW_STAGES.name(Stage.BUDGET_WINDOW_CHILD_SPAWN),
                attributes={"simulation_year": simulation_year},
            ):
                call = self.child_func.spawn(
                    child_request.payload,
                    observability_context=observability_context,
                )
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
            with self.runtime.span(
                BUDGET_WINDOW_STAGES.name(Stage.BUDGET_WINDOW_STATE_WRITE)
            ):
                put_batch_job_state(self.state)

    def poll_running_children_once(self) -> bool:
        """Probe each running child once without blocking; return True if any finished.

        A failed child, or a result that cannot be parsed, fails the batch and
        returns False immediately.
        """
        progress_made = False

        for simulation_year in list(self.state.running_years):
            handle = self.resolve_child_handle(simulation_year)

            poll_started = time.perf_counter()
            try:
                child_result = handle.call.get(timeout=0)
            except TimeoutError:
                continue
            except Exception as exc:  # noqa: BLE001  # call.get re-raises whatever the child worker raised; every failure fails the batch, logged with its traceback by log_and_redact_exception
                detail = spm_error_detail(exc)
                if detail:
                    self.state.errors = [detail]
                    self.fail_batch_for_child_error(
                        simulation_year=simulation_year, error=detail.message
                    )
                    self.state.child_jobs[simulation_year].errors = [detail]
                    put_batch_job_state(self.state)
                    return False
                redacted = log_and_redact_exception(
                    exc,
                    runtime=self.runtime,
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
            finally:
                self._child_poll_seconds += time.perf_counter() - poll_started
                self._child_poll_count += 1
                self._publish_poll_stats()

            try:
                with self.runtime.span(
                    BUDGET_WINDOW_STAGES.name(Stage.BUDGET_WINDOW_RESULT_PARSE),
                    attributes={"simulation_year": simulation_year},
                ):
                    annual_impact = extract_annual_impact(
                        simulation_year=simulation_year,
                        child_result=child_result,
                        spm=self.state.request_payload.get("spm"),
                    )
            except Exception as exc:  # noqa: BLE001  # any unparseable child result fails the batch, logged with its traceback by log_and_redact_exception
                detail = spm_error_detail(exc)
                if detail:
                    self.state.errors = [detail]
                    self.fail_batch_for_child_error(
                        simulation_year=simulation_year, error=detail.message
                    )
                    self.state.child_jobs[simulation_year].errors = [detail]
                    put_batch_job_state(self.state)
                    return False
                redacted = log_and_redact_exception(
                    exc,
                    runtime=self.runtime,
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
            with self.runtime.span(
                BUDGET_WINDOW_STAGES.name(Stage.BUDGET_WINDOW_STATE_WRITE)
            ):
                put_batch_job_state(self.state)
            progress_made = True

        return progress_made

    def resolve_child_handle(self, simulation_year: str) -> ChildSimulationHandle:
        """Return the year's call handle, rebuilt from the stored job id if needed."""
        handle = self.child_handles.get(simulation_year)
        if handle is not None and handle.call is not None:
            return handle

        job_id = self.state.child_jobs[simulation_year].job_id
        # ``FunctionCall.from_id`` is a lazy handle (no RPC), so there is
        # nothing meaningful to time here; the network cost lands in the
        # subsequent ``call.get`` poll.
        call = self.modal.FunctionCall.from_id(job_id)
        resolved_handle = ChildSimulationHandle(
            simulation_year=simulation_year,
            job_id=job_id,
            call=call,
        )
        self.child_handles[simulation_year] = resolved_handle
        return resolved_handle

    def _publish_poll_stats(self) -> None:
        self.runtime.set_context(
            child_poll_count=self._child_poll_count,
            child_poll_ms_total=round(self._child_poll_seconds * 1000, 1),
            backoff_sleep_count=self._backoff_sleep_count,
            backoff_sleep_ms_total=round(self._backoff_sleep_seconds * 1000, 1),
        )

    def fail_batch_for_child_error(
        self,
        *,
        simulation_year: str,
        error: str,
    ) -> None:
        """Mark the year's child and the whole batch failed and store the state."""
        mark_child_failed(self.state, year=simulation_year, error=error)
        mark_batch_failed(self.state, error=error)
        with self.runtime.span(
            BUDGET_WINDOW_STAGES.name(Stage.BUDGET_WINDOW_STATE_WRITE)
        ):
            put_batch_job_state(self.state)

    def complete_batch(self) -> dict[str, Any]:
        """Aggregate annual impacts, mark the batch complete, and return its status."""
        with self.runtime.span(
            BUDGET_WINDOW_STAGES.name(Stage.BUDGET_WINDOW_AGGREGATION)
        ):
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
        with self.runtime.span(
            BUDGET_WINDOW_STAGES.name(Stage.BUDGET_WINDOW_STATE_WRITE)
        ):
            put_batch_job_state(self.state)
        return serialize_batch_status(self.state, runtime=self.runtime)
