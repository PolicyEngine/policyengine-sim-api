"""Unit coverage for the budget-window scheduler wiring.

Despite the previous filename (``test_budget_window_semi_integration.py``),
nothing in this file talks to a real Modal control plane: we monkey-patch
the ``modal`` module with in-memory fakes and run the scheduler against
those fakes. Renamed to reflect the actual scope (#457). Real Modal
integration tests live under ``tests/integration/`` and are skipped by
default."""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest
from fastapi.testclient import TestClient

import src.modal.budget_window_batch as batch_module
import src.modal.budget_window_scheduler as scheduler_module
import policyengine_simulation_contract.budget_window_state as state_module
from policyengine_simulation_gateway.testing import create_gateway_app
from policyengine_simulation_gateway import endpoints


@dataclass
class SemiIntegrationRuntime:
    dicts: dict[str, dict] = field(default_factory=dict)
    calls: dict[str, object] = field(default_factory=dict)
    child_payloads: list[dict] = field(default_factory=list)
    current_parent_call_id: str | None = None
    next_parent_call_id: str = "parent-batch-123"
    active_child_calls: set[str] = field(default_factory=set)
    max_active_child_calls: int = 0

    def child_result_for_year(self, simulation_year: str) -> dict:
        offset = int(simulation_year) - 2025
        return {
            "budget": {
                "tax_revenue_impact": offset * 100,
                "state_tax_revenue_impact": offset * 10,
                "benefit_spending_impact": offset + 4,
                "budgetary_impact": offset * 100 - (offset + 4),
            }
        }

    def child_started(self, object_id: str) -> None:
        self.active_child_calls.add(object_id)
        self.max_active_child_calls = max(
            self.max_active_child_calls,
            len(self.active_child_calls),
        )

    def child_finished(self, object_id: str) -> None:
        self.active_child_calls.discard(object_id)


class MockDict:
    def __init__(self, data: dict):
        self._data = data

    def __getitem__(self, key: str):
        return self._data[key]

    def __setitem__(self, key: str, value):
        self._data[key] = value

    def get(self, key: str, default=None):
        return self._data.get(key, default)


class MockChildCall:
    def __init__(
        self, runtime: SemiIntegrationRuntime, *, object_id: str, result: dict
    ):
        self.runtime = runtime
        self.object_id = object_id
        self.result = result
        self.runtime.child_started(object_id)

    def get(self, timeout: int = 0):
        self.runtime.child_finished(self.object_id)
        return self.result


class MockParentBatchCall:
    def __init__(self, runtime: SemiIntegrationRuntime, *, payload: dict):
        self.runtime = runtime
        self.object_id = runtime.next_parent_call_id
        self.payload = payload
        self._polls = 0
        self._result = None

    def get(self, timeout: int = 0):
        if self._result is not None:
            return self._result
        if self._polls == 0:
            self._polls += 1
            raise TimeoutError()

        previous = self.runtime.current_parent_call_id
        self.runtime.current_parent_call_id = self.object_id
        try:
            self._result = batch_module.run_budget_window_batch_impl(self.payload)
        finally:
            self.runtime.current_parent_call_id = previous
        return self._result


class MockFunction:
    def __init__(
        self, runtime: SemiIntegrationRuntime, *, app_name: str, func_name: str
    ):
        self.runtime = runtime
        self.app_name = app_name
        self.func_name = func_name

    def spawn(self, payload: dict):
        if self.func_name == "run_budget_window_batch":
            call = MockParentBatchCall(self.runtime, payload=payload)
            self.runtime.calls[call.object_id] = call
            return call

        simulation_year = payload["time_period"]
        self.runtime.child_payloads.append(payload)
        call = MockChildCall(
            self.runtime,
            object_id=f"child-{simulation_year}",
            result=self.runtime.child_result_for_year(simulation_year),
        )
        self.runtime.calls[call.object_id] = call
        return call


@pytest.fixture
def budget_window_semi_integration_client(
    monkeypatch,
) -> tuple[TestClient, SemiIntegrationRuntime]:
    runtime = SemiIntegrationRuntime()
    runtime.dicts["simulation-api-us-versions"] = {
        "latest": "1.500.0",
        "1.500.0": "policyengine-simulation-py4-10-0",
    }

    class MockModalDict:
        @staticmethod
        def from_name(name: str, create_if_missing: bool = False):
            if create_if_missing and name not in runtime.dicts:
                runtime.dicts[name] = {}
            if name not in runtime.dicts:
                raise KeyError(name)
            return MockDict(runtime.dicts[name])

    class MockModalFunction:
        @staticmethod
        def from_name(app_name: str, func_name: str):
            return MockFunction(runtime, app_name=app_name, func_name=func_name)

    class MockModalFunctionCall:
        @classmethod
        def from_id(cls, object_id: str):
            return runtime.calls[object_id]

    class MockModal:
        Dict = MockModalDict
        Function = MockModalFunction
        FunctionCall = MockModalFunctionCall

        @staticmethod
        def current_function_call_id():
            if runtime.current_parent_call_id is None:
                raise RuntimeError("No active parent batch call")
            return runtime.current_parent_call_id

    monkeypatch.setattr(endpoints, "modal", MockModal)
    monkeypatch.setattr(state_module, "modal", MockModal)
    monkeypatch.setattr(scheduler_module, "modal", MockModal)
    monkeypatch.setattr(batch_module, "modal", MockModal)
    monkeypatch.setattr(scheduler_module.time, "sleep", lambda _: None)

    return TestClient(create_gateway_app()), runtime


def test_budget_window_submit_and_poll_exercise_gateway_worker_seams(
    budget_window_semi_integration_client,
):
    client, runtime = budget_window_semi_integration_client

    submit_response = client.post(
        "/simulate/economy/budget-window",
        json={
            "country": "us",
            "region": "us",
            "scope": "macro",
            "reform": {},
            "start_year": "2026",
            "window_size": 3,
            "max_parallel": 2,
            "_telemetry": {
                "run_id": "batch-run-123",
                "process_id": "proc-123",
                "capture_mode": "disabled",
            },
        },
    )

    assert submit_response.status_code == 200
    assert submit_response.json()["batch_job_id"] == "parent-batch-123"

    first_poll = client.get("/budget-window-jobs/parent-batch-123")
    assert first_poll.status_code == 202
    assert first_poll.json()["status"] == "submitted"
    assert first_poll.json()["queued_years"] == ["2026", "2027", "2028"]

    second_poll = client.get("/budget-window-jobs/parent-batch-123")
    assert second_poll.status_code == 200
    body = second_poll.json()

    assert body["status"] == "complete"
    assert body["progress"] == 100
    assert body["completed_years"] == ["2026", "2027", "2028"]
    assert body["result"]["kind"] == "budgetWindow"
    assert body["result"]["startYear"] == "2026"
    assert body["result"]["endYear"] == "2028"
    assert [row["year"] for row in body["result"]["annualImpacts"]] == [
        "2026",
        "2027",
        "2028",
    ]
    assert body["result"]["totals"] == {
        "year": "Total",
        "taxRevenueImpact": 600.0,
        "federalTaxRevenueImpact": 540.0,
        "stateTaxRevenueImpact": 60.0,
        "benefitSpendingImpact": 18.0,
        "budgetaryImpact": 582.0,
    }

    assert runtime.max_active_child_calls == 2
    assert len(runtime.child_payloads) == 3
    assert [payload["time_period"] for payload in runtime.child_payloads] == [
        "2026",
        "2027",
        "2028",
    ]
    assert all("target" not in payload for payload in runtime.child_payloads)
    assert all("start_year" not in payload for payload in runtime.child_payloads)
    assert all("window_size" not in payload for payload in runtime.child_payloads)
    assert all("max_parallel" not in payload for payload in runtime.child_payloads)
    assert all("_metadata" not in payload for payload in runtime.child_payloads)
