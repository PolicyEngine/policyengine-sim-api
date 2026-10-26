"""Tests that Modal raises ``ReportExecutionError`` in the caller as the same type.

They call Modal's own exception transport, which is private to the Modal
client, so they live apart from the other report execution tests: a Modal
upgrade that moves these functions fails only this module.
"""

from __future__ import annotations

import asyncio
from uuid import UUID

import pytest
from modal._serialization import pickle_exception
from modal._utils.function_utils import _process_result
from modal_proto import api_pb2
from policyengine_simulation_contract.report_execution import (
    ReportExecutionError,
    ReportExecutionFailure,
)

EVALUATION_ID = UUID("00000000-0000-0000-0000-000000000001")


def _failure() -> ReportExecutionFailure:
    return ReportExecutionFailure(
        evaluation_id=EVALUATION_ID,
        error_code="simulation_execution_failed",
        error_summary="ValueError in dataset_load (baseline): bad input",
    )


def test_modal_raises_the_error_in_the_caller_as_the_same_type() -> None:
    """Send the error through Modal's own exception transport.

    A function container pickles the exception it raised with
    ``pickle_exception``, and ``FunctionCall.get`` in the caller unpickles and
    raises it in ``_process_result``. Both are Modal's functions, so this
    checks the installed Modal client rather than a copy of its behavior.
    """
    error = ReportExecutionError(_failure())
    remote_result = api_pb2.GenericResult(
        status=api_pb2.GenericResult.GENERIC_STATUS_FAILURE,
        data=pickle_exception(error),
        exception=repr(error),
    )

    with pytest.raises(ReportExecutionError) as raised:
        asyncio.run(
            _process_result(remote_result, api_pb2.DATA_FORMAT_PICKLE, None, None)
        )

    assert type(raised.value) is ReportExecutionError
    assert raised.value.failure == _failure()
    assert str(raised.value) == "Report execution failed"
