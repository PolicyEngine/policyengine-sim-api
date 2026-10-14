"""Integration tests for Modal-based budget-window batches.

These tests run against the staging Modal deployment and verify that the
gateway can spawn the parent budget-window worker, the parent can spawn child
simulation workers, and the completed batch result has the public response
shape expected by API consumers.
"""

from http import HTTPStatus

import pytest
from policyengine_api_simulation_client.models import (
    BudgetWindowBatchSubmitResponse,
    BudgetWindowResult,
)
from policyengine_api_simulation_client.types import Unset


@pytest.mark.beta_only
def test_budget_window_multi_year_batch_completes(
    budget_window_smoke_case,
    decode_response_content,
    submit_budget_window_batch,
    poll_budget_window_batch,
):
    """A two-year US budget-window batch returns both annual impacts and totals.

    Given a two-year US budget-window request
    When the batch is submitted and polled to completion
    Then the response contains 2026 and 2027 annual impacts plus totals.
    """
    budget_window_years = budget_window_smoke_case.years
    submit_response = submit_budget_window_batch(budget_window_smoke_case.request)

    assert submit_response.status_code == HTTPStatus.OK, (
        "Unexpected submit status "
        f"{submit_response.status_code}: "
        f"{decode_response_content(submit_response.content)}"
    )
    assert isinstance(submit_response.parsed, BudgetWindowBatchSubmitResponse), (
        f"Unexpected response type: {type(submit_response.parsed)}"
    )
    assert submit_response.parsed.status == "submitted"
    assert submit_response.parsed.version == budget_window_smoke_case.model_version

    batch_job_id = submit_response.parsed.batch_job_id
    assert submit_response.parsed.poll_url == f"/budget-window-jobs/{batch_job_id}"

    completed = poll_budget_window_batch(batch_job_id)

    assert completed.status == "complete"
    assert completed.progress == 100
    assert completed.error is None or isinstance(completed.error, Unset)
    assert isinstance(completed.result, BudgetWindowResult)

    result = completed.result
    assert result.kind == "budgetWindow"
    assert result.start_year == budget_window_years[0]
    assert result.end_year == budget_window_years[-1]
    assert result.window_size == len(budget_window_years)
    annual_impacts = result.annual_impacts
    assert not isinstance(annual_impacts, Unset)
    assert [impact.year for impact in annual_impacts] == budget_window_years
    assert result.totals.year == "Total"
    assert all(
        isinstance(impact.budgetary_impact, int | float) for impact in annual_impacts
    )
