"""A dataset-year error keeps its code wherever a worker failure is reported.

The run raises it while loading the dataset. A segmented national parent, the
executor's own HTTP route and the gateway must pass it on as a caller error
with its code and message, not retry it or redact it into a server failure.
"""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from dataset_year_errors import (
    DATASET_YEAR_MESSAGE,
    DATASET_YEAR_UNAVAILABLE,
    dataset_year_error,
)
from fastapi import FastAPI
from fastapi.testclient import TestClient
from policyengine_simulation_executor.simulation import create_router


def test_segmented_child_dataset_year_error_is_not_redacted_or_retried():
    from src.modal.segmented_national import (
        CHILD_DEADLINE_SECONDS,
        ChildExecution,
        SegmentedNationalRunner,
    )

    child = SimpleNamespace(get=Mock(side_effect=dataset_year_error()), cancel=Mock())
    child_execution = ChildExecution(
        group=["state/ca"],
        call=child,
        attempt=1,
        dispatched_at=0.0,
        deadline_seconds=CHILD_DEADLINE_SECONDS,
    )
    runner = object.__new__(SegmentedNationalRunner)
    runner.poll_interval_initial_seconds = 0.001
    runner.poll_interval_max_seconds = 0.001
    runner.superseded_children = []

    with pytest.raises(ValueError, match="results are available for") as raised:
        runner._collect([child_execution])

    assert raised.value.code == DATASET_YEAR_UNAVAILABLE
    child.get.assert_called_once()
    child.cancel.assert_called_once()


def test_sync_endpoint_returns_400_for_a_dataset_year_error(
    monkeypatch, observability_runtime
):
    from policyengine_simulation_executor import simulation

    def fail_with_dataset_year_error(_params, *, runtime):
        raise dataset_year_error()

    monkeypatch.setattr(simulation, "run_simulation_impl", fail_with_dataset_year_error)
    app = FastAPI()
    app.include_router(create_router(observability_runtime))

    response = TestClient(app).post(
        "/simulate/economy/comparison",
        json={"country": "us", "time_period": "2036"},
    )

    assert response.status_code == 400
    assert response.json() == {
        "status": "failed",
        "result": None,
        "error": DATASET_YEAR_MESSAGE,
        "errors": [{"code": DATASET_YEAR_UNAVAILABLE, "message": DATASET_YEAR_MESSAGE}],
    }
