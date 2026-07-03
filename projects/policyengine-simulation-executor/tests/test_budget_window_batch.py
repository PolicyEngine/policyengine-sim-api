"""Tests for budget-window batch orchestration."""

from __future__ import annotations

from collections import deque

import pytest

import src.modal.budget_window_batch as batch_module
import src.modal.budget_window_scheduler as scheduler_module
import policyengine_simulation_contract.budget_window_state as state_module
from src.modal.budget_window_batch import run_budget_window_batch_impl
from policyengine_simulation_contract.gateway_models import (
    BudgetWindowBatchRequest,
    PolicyEngineBundle,
)


class SequencedCall:
    def __init__(self, object_id: str, events, tracker):
        self.object_id = object_id
        self._events = deque(events)
        self._tracker = tracker
        self._done = False
        self._tracker.started(self.object_id)

    def get(self, timeout: int = 0):
        event = self._events[0]
        if isinstance(event, TimeoutError):
            self._events.popleft()
            raise TimeoutError()

        self._events.popleft()
        if isinstance(event, Exception):
            self._finish()
            raise event

        self._finish()
        return event

    def _finish(self):
        if not self._done:
            self._done = True
            self._tracker.finished(self.object_id)


class SpawnTracker:
    def __init__(self):
        self.active = set()
        self.max_active = 0

    def started(self, object_id: str):
        self.active.add(object_id)
        self.max_active = max(self.max_active, len(self.active))

    def finished(self, object_id: str):
        self.active.discard(object_id)


class MockRunSimulationFunction:
    def __init__(
        self,
        *,
        tracker: SpawnTracker,
        results_by_year: dict[str, list[object]],
        call_registry: dict[str, SequencedCall],
    ):
        self.tracker = tracker
        self.results_by_year = results_by_year
        self.call_registry = call_registry
        self.spawned_years: list[str] = []

    def spawn(self, payload: dict) -> SequencedCall:
        year = payload["time_period"]
        self.spawned_years.append(year)
        call = SequencedCall(
            object_id=f"child-{year}",
            events=list(self.results_by_year[year]),
            tracker=self.tracker,
        )
        self.call_registry[call.object_id] = call
        return call


@pytest.fixture
def mock_batch_modal(monkeypatch):
    dicts: dict[str, dict] = {}
    call_registry: dict[str, SequencedCall] = {}
    functions: dict[tuple[str, str], object] = {}
    parent_call_id = "parent-123"

    class MockDict:
        def __init__(self, data: dict):
            self._data = data

        def __getitem__(self, key: str):
            return self._data[key]

        def __setitem__(self, key: str, value):
            self._data[key] = value

        def get(self, key: str, default=None):
            return self._data.get(key, default)

    class MockModalDict:
        @staticmethod
        def from_name(name: str, create_if_missing: bool = False):
            if create_if_missing and name not in dicts:
                dicts[name] = {}
            if name not in dicts:
                raise KeyError(name)
            return MockDict(dicts[name])

    class MockModalFunctionCall:
        @classmethod
        def from_id(cls, object_id: str):
            return call_registry[object_id]

    class MockModalFunction:
        @staticmethod
        def from_name(app_name: str, func_name: str):
            return functions[(app_name, func_name)]

    class MockModal:
        Dict = MockModalDict
        Function = MockModalFunction
        FunctionCall = MockModalFunctionCall

        @staticmethod
        def current_function_call_id():
            return parent_call_id

    monkeypatch.setattr(batch_module, "modal", MockModal)
    monkeypatch.setattr(scheduler_module, "modal", MockModal)
    monkeypatch.setattr(state_module, "modal", MockModal)
    monkeypatch.setattr(scheduler_module.time, "sleep", lambda _: None)

    return {
        "dicts": dicts,
        "call_registry": call_registry,
        "functions": functions,
        "parent_call_id": parent_call_id,
    }


def _build_parent_payload(*, window_size: int = 3):
    request = BudgetWindowBatchRequest(
        country="us",
        region="us",
        start_year="2026",
        window_size=window_size,
        max_parallel=2,
        scope="macro",
        reform={},
        _telemetry={
            "run_id": "batch-run-123",
            "process_id": "proc-123",
            "capture_mode": "disabled",
        },
    )
    payload = request.model_dump(mode="json", exclude={"telemetry"})
    payload["version"] = "1.500.0"
    payload["_telemetry"] = request.telemetry.model_dump(mode="json")
    payload["_metadata"] = {
        "resolved_version": "1.500.0",
        "resolved_app_name": "policyengine-simulation-py4-10-0",
        "policyengine_bundle": PolicyEngineBundle(model_version="1.500.0").model_dump(
            mode="json"
        ),
    }
    return request, payload


def _seed_parent_batch(request: BudgetWindowBatchRequest, batch_job_id: str):
    seed = state_module.create_initial_batch_state(
        batch_job_id=batch_job_id,
        request=request,
        resolved_version="1.500.0",
        resolved_app_name="policyengine-simulation-py4-10-0",
        bundle=PolicyEngineBundle(model_version="1.500.0"),
    )
    state_module.put_batch_job_seed(seed)


def test_run_budget_window_batch_impl_completes_and_respects_max_parallel(
    mock_batch_modal,
):
    request, payload = _build_parent_payload()
    _seed_parent_batch(request, mock_batch_modal["parent_call_id"])

    tracker = SpawnTracker()
    run_simulation = MockRunSimulationFunction(
        tracker=tracker,
        results_by_year={
            "2026": [
                TimeoutError(),
                {
                    "budget": {
                        "tax_revenue_impact": 10,
                        "state_tax_revenue_impact": 3,
                        "benefit_spending_impact": 5,
                        "budgetary_impact": 15,
                    }
                },
            ],
            "2027": [
                {
                    "budget": {
                        "tax_revenue_impact": 11,
                        "state_tax_revenue_impact": 3,
                        "benefit_spending_impact": 6,
                        "budgetary_impact": 17,
                    }
                }
            ],
            "2028": [
                {
                    "budget": {
                        "tax_revenue_impact": 12,
                        "state_tax_revenue_impact": 4,
                        "benefit_spending_impact": 7,
                        "budgetary_impact": 19,
                    }
                }
            ],
        },
        call_registry=mock_batch_modal["call_registry"],
    )
    mock_batch_modal["functions"][
        ("policyengine-simulation-py4-10-0", "run_simulation")
    ] = run_simulation

    result = run_budget_window_batch_impl(payload)
    state = state_module.get_batch_job_state(mock_batch_modal["parent_call_id"])

    assert tracker.max_active == 2
    assert run_simulation.spawned_years == ["2026", "2027", "2028"]
    assert result["status"] == "complete"
    assert result["progress"] == 100
    assert result["completed_years"] == ["2027", "2026", "2028"]
    assert result["result"]["annualImpacts"][0]["year"] == "2026"
    assert result["result"]["totals"]["budgetaryImpact"] == 51
    assert state is not None
    assert state.status == "complete"
    assert state.result is not None
    assert state.result.totals.budgetaryImpact == 51


def test_run_budget_window_batch_impl_marks_failure(mock_batch_modal):
    request, payload = _build_parent_payload(window_size=2)
    _seed_parent_batch(request, mock_batch_modal["parent_call_id"])

    tracker = SpawnTracker()
    run_simulation = MockRunSimulationFunction(
        tracker=tracker,
        results_by_year={
            "2026": [RuntimeError("child failed")],
            "2027": [
                {
                    "budget": {
                        "tax_revenue_impact": 11,
                        "state_tax_revenue_impact": 3,
                        "benefit_spending_impact": 6,
                        "budgetary_impact": 17,
                    }
                }
            ],
        },
        call_registry=mock_batch_modal["call_registry"],
    )
    mock_batch_modal["functions"][
        ("policyengine-simulation-py4-10-0", "run_simulation")
    ] = run_simulation

    result = run_budget_window_batch_impl(payload)
    state = state_module.get_batch_job_state(mock_batch_modal["parent_call_id"])

    assert result["status"] == "failed"
    assert result["failed_years"] == ["2026"]
    # Error body is redacted (#453); message must not leak the raw exception.
    assert result["error"].startswith("Simulation failed")
    assert "correlation_id=" in result["error"]
    assert "child failed" not in result["error"]
    assert result["running_years"] == []
    assert result["child_jobs"]["2027"]["status"] == "cancelled"
    assert result["child_jobs"]["2027"]["error"].startswith("Simulation failed")
    assert state is not None
    assert state.status == "failed"
    assert state.error.startswith("Simulation failed")
    assert state.running_years == []
    assert state.child_jobs["2027"].status == "cancelled"


def test_scheduler_sleep_exponentially_backs_off_then_resets_on_progress(
    monkeypatch, mock_batch_modal
):
    """When every poll sees nothing resolve the runner should sleep with
    increasing intervals, capped at the configured max. Any progress in a
    subsequent poll should reset the cadence to the initial interval.
    """

    request, payload = _build_parent_payload(window_size=1)
    _seed_parent_batch(request, mock_batch_modal["parent_call_id"])

    tracker = SpawnTracker()
    run_simulation = MockRunSimulationFunction(
        tracker=tracker,
        results_by_year={
            "2026": [
                TimeoutError(),
                TimeoutError(),
                TimeoutError(),
                TimeoutError(),
                {
                    "budget": {
                        "tax_revenue_impact": 10,
                        "state_tax_revenue_impact": 3,
                        "benefit_spending_impact": 5,
                        "budgetary_impact": 15,
                    }
                },
            ],
        },
        call_registry=mock_batch_modal["call_registry"],
    )
    mock_batch_modal["functions"][
        ("policyengine-simulation-py4-10-0", "run_simulation")
    ] = run_simulation

    sleeps: list[float] = []
    monkeypatch.setattr(scheduler_module.time, "sleep", sleeps.append)

    runner = scheduler_module.BudgetWindowBatchRunner(
        context=scheduler_module.BudgetWindowBatchContext(
            batch_job_id=mock_batch_modal["parent_call_id"],
            request=request,
            resolved_version="1.500.0",
            resolved_app_name="policyengine-simulation-py4-10-0",
            bundle=PolicyEngineBundle(model_version="1.500.0"),
            raw_params=payload,
        ),
        poll_interval_seconds=0.5,
        poll_interval_max_seconds=4.0,
        poll_interval_backoff_factor=2.0,
    )

    runner.run()

    # We expect 4 sleeps from the 4 TimeoutError probes: 0.5, 1.0, 2.0, 4.0.
    assert sleeps == [0.5, 1.0, 2.0, 4.0]


def test_scheduler_publishes_bounded_poll_aggregates(monkeypatch, mock_batch_modal):
    """Poll/sleep telemetry must be published as overwriting aggregate
    attributes, not one segment per probe: segments append nodes to the
    operation's in-memory segment tree, so a near-timeout batch polling for
    an hour would grow memory without bound and emit a final operation log
    line large enough for Modal's log pipeline to truncate.
    """

    request, payload = _build_parent_payload(window_size=1)
    _seed_parent_batch(request, mock_batch_modal["parent_call_id"])

    tracker = SpawnTracker()
    run_simulation = MockRunSimulationFunction(
        tracker=tracker,
        results_by_year={
            "2026": [
                TimeoutError(),
                TimeoutError(),
                {
                    "budget": {
                        "tax_revenue_impact": 10,
                        "state_tax_revenue_impact": 3,
                        "benefit_spending_impact": 5,
                        "budgetary_impact": 15,
                    }
                },
            ],
        },
        call_registry=mock_batch_modal["call_registry"],
    )
    mock_batch_modal["functions"][
        ("policyengine-simulation-py4-10-0", "run_simulation")
    ] = run_simulation

    monkeypatch.setattr(scheduler_module.time, "sleep", lambda _: None)
    attributes: dict[str, object] = {}
    monkeypatch.setattr(
        scheduler_module,
        "set_attribute",
        lambda key, value: attributes.__setitem__(key, value),
    )

    runner = scheduler_module.BudgetWindowBatchRunner(
        context=scheduler_module.BudgetWindowBatchContext(
            batch_job_id=mock_batch_modal["parent_call_id"],
            request=request,
            resolved_version="1.500.0",
            resolved_app_name="policyengine-simulation-py4-10-0",
            bundle=PolicyEngineBundle(model_version="1.500.0"),
            raw_params=payload,
        ),
        poll_interval_seconds=0.5,
        poll_interval_max_seconds=4.0,
        poll_interval_backoff_factor=2.0,
    )

    runner.run()

    # Two TimeoutError probes plus the resolving poll.
    assert attributes["child_poll_count"] == 3
    assert attributes["child_poll_ms_total"] >= 0
    # Two empty polls: 0.5s + 1.0s of backoff sleep.
    assert attributes["backoff_sleep_count"] == 2
    assert attributes["backoff_sleep_ms_total"] == 1500.0


def test_run_budget_window_batch_impl_fails_on_malformed_child_result(
    mock_batch_modal,
):
    request, payload = _build_parent_payload(window_size=1)
    _seed_parent_batch(request, mock_batch_modal["parent_call_id"])

    tracker = SpawnTracker()
    run_simulation = MockRunSimulationFunction(
        tracker=tracker,
        results_by_year={
            "2026": [
                {
                    "budget": {
                        "state_tax_revenue_impact": 3,
                        "benefit_spending_impact": 6,
                        "budgetary_impact": 17,
                    }
                }
            ],
        },
        call_registry=mock_batch_modal["call_registry"],
    )
    mock_batch_modal["functions"][
        ("policyengine-simulation-py4-10-0", "run_simulation")
    ] = run_simulation

    result = run_budget_window_batch_impl(payload)
    state = state_module.get_batch_job_state(mock_batch_modal["parent_call_id"])

    assert result["status"] == "failed"
    assert result["failed_years"] == ["2026"]
    # The raw "Malformed ..." message is logged server-side but only a
    # redacted correlated message reaches the caller (#453).
    assert result["error"].startswith("Simulation failed")
    assert "correlation_id=" in result["error"]
    assert "Malformed" not in result["error"]
    assert state is not None
    assert state.status == "failed"
    assert state.child_jobs["2026"].status == "failed"
