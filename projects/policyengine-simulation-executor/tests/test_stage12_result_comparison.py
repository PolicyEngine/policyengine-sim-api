"""Tests for exact aggregate-result comparison receipts."""

from datetime import UTC, datetime
from uuid import UUID

from policyengine_simulation_executor.stage12_result_comparison import compare_results

RUN_ID = UUID("00000000-0000-0000-0000-000000000001")
COMPARED_AT = datetime(2026, 9, 18, tzinfo=UTC)


def test_equal_results_have_matching_digests_and_no_differences() -> None:
    result = {"budget": {"total": 10, "values": [1, 2]}}

    receipt = compare_results(
        evaluation_id=RUN_ID,
        production_job_id="production-job-1",
        production_result=result,
        stage12_result=result,
        compared_at=COMPARED_AT,
    )

    assert receipt.status == "matched"
    assert receipt.difference_count == 0
    assert receipt.differences == ()
    assert receipt.production_result_sha256 == receipt.stage12_result_sha256


def test_receipt_lists_every_differing_leaf_with_escaped_json_pointers() -> None:
    receipt = compare_results(
        evaluation_id=RUN_ID,
        production_job_id="production-job-1",
        production_result={"a/b": {"~value": 10.0}, "list": [1, 2]},
        stage12_result={"a/b": {"~value": 12.0}, "list": [1, 3, 4]},
        compared_at=COMPARED_AT,
    )

    assert receipt.status == "different"
    assert receipt.difference_count == 3
    differences = {difference.path: difference for difference in receipt.differences}
    numeric = differences["/a~1b/~0value"]
    assert numeric.production_value == 10.0
    assert numeric.stage12_value == 12.0
    assert numeric.absolute_delta == 2.0
    assert numeric.relative_delta == 0.2
    assert differences["/list/2"].production_present is False
    assert differences["/list/2"].stage12_value == 4


def test_numeric_zero_has_an_absolute_but_no_relative_delta() -> None:
    receipt = compare_results(
        evaluation_id=RUN_ID,
        production_job_id="production-job-1",
        production_result={"value": 0},
        stage12_result={"value": 5},
        compared_at=COMPARED_AT,
    )

    difference = receipt.differences[0]
    assert difference.absolute_delta == 5.0
    assert difference.relative_delta is None
