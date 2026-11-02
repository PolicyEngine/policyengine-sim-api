"""Thin entrypoint for budget-window batch execution."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from policyengine_simulation_observability.stages import BUDGET_WINDOW_STAGES, Stage
from src.modal.budget_window_context import build_batch_context
from src.modal.budget_window_scheduler import BudgetWindowBatchRunner

import modal

if TYPE_CHECKING:
    from policyengine_observability import ObservabilityRuntime


def run_budget_window_batch_impl(
    params: dict[str, Any],
    *,
    runtime: ObservabilityRuntime,
) -> dict[str, Any]:
    """Run a budget-window batch to completion and return its final status payload.

    Must run inside a Modal call: the current function call id becomes the
    batch job id that child jobs and stored batch state are keyed by.
    """
    from policyengine_simulation_executor.spm import (
        normalize_runtime_spm,
    )

    selection = normalize_runtime_spm(params)
    if selection is not None:
        params = {**params, "spm": selection}
    batch_job_id = modal.current_function_call_id()
    if batch_job_id is None:
        msg = "run_budget_window_batch must run inside a Modal function call"
        raise RuntimeError(msg)
    runtime.set_context(batch_job_id=batch_job_id)
    with runtime.span(BUDGET_WINDOW_STAGES.name(Stage.BUDGET_WINDOW_CONTEXT)):
        context = build_batch_context(
            params,
            batch_job_id=batch_job_id,
        )
    runner = BudgetWindowBatchRunner(context, runtime=runtime)
    return runner.run()
