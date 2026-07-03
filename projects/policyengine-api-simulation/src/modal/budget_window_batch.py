"""Thin entrypoint for budget-window batch execution."""

from __future__ import annotations

from typing import Any

import modal
from policyengine_observability import segment, set_attribute

from src.modal.budget_window_context import build_batch_context
from src.modal.budget_window_scheduler import BudgetWindowBatchRunner
from policyengine_api_simulation.observability import SegmentName


def run_budget_window_batch_impl(params: dict[str, Any]) -> dict[str, Any]:
    batch_job_id = modal.current_function_call_id()
    set_attribute("batch_job_id", batch_job_id)
    with segment(SegmentName.BUDGET_WINDOW_CONTEXT):
        context = build_batch_context(
            params,
            batch_job_id=batch_job_id,
        )
    runner = BudgetWindowBatchRunner(context)
    return runner.run()
