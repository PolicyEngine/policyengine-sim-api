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
from policyengine_simulation_contract.budget_window_state import (
    BUDGET_WINDOW_JOB_DICT_NAME,
    BUDGET_WINDOW_JOB_SEED_DICT_NAME,
)
from policyengine_simulation_contract.spm import SPMInputError, SPMSelection
from policyengine_simulation_gateway.testing import create_gateway_app
from policyengine_simulation_gateway import endpoints

SPM_SELECTION = SPMSelection(
    forecast_content_sha256="a" * 64,
    scenario="ce_trend",
    geography_kind="national",
    geography_id=None,
    county_vintage="2020",
    as_of=None,
).model_dump(mode="json")


def spm_child_result(runtime, simulation_year, *, receipt_year):
    """A child result carrying a canonical SPM receipt for ``receipt_year``."""
    receipt = {
        "forecast_id": "test-only",
        "forecast_sha256": SPM_SELECTION["forecast_content_sha256"],
        "scenario": SPM_SELECTION["scenario"],
        "geography_kind": SPM_SELECTION["geography_kind"],
        "runtime_versions": {"policyengine-us": "test-only"},
        "years": {receipt_year: {"status": "forecast"}},
        "geographies": [],
        "composition_method": "classified-inputs",
        "storage_method": "formula",
    }
    return {
        "spm_config": dict(SPM_SELECTION),
        "spm_provenance": {"baseline": [receipt], "reform": [dict(receipt)]},
        **runtime.child_result_for_year(simulation_year),
    }


@dataclass
class SemiIntegrationRuntime:
    dicts: dict[str, dict] = field(default_factory=dict)
    calls: dict[str, object] = field(default_factory=dict)
    child_payloads: list[dict] = field(default_factory=list)
    current_parent_call_id: str | None = None
    next_parent_call_id: str = "parent-batch-123"
    active_child_calls: set[str] = field(default_factory=set)
    max_active_child_calls: int = 0
    # Per-year injection seams: a child call raises ``child_errors[year]``
    # instead of returning, or returns ``child_results[year]`` verbatim.
    child_errors: dict[str, BaseException] = field(default_factory=dict)
    child_results: dict[str, dict] = field(default_factory=dict)
    observability: object | None = None

    def child_outcome_for_year(self, simulation_year: str) -> dict:
        return self.child_results.get(
            simulation_year, self.child_result_for_year(simulation_year)
        )

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
        self,
        runtime: SemiIntegrationRuntime,
        *,
        object_id: str,
        result: dict,
        error: BaseException | None = None,
    ):
        self.runtime = runtime
        self.object_id = object_id
        self.result = result
        self.error = error
        self.runtime.child_started(object_id)

    def get(self, timeout: int = 0):
        self.runtime.child_finished(self.object_id)
        if self.error is not None:
            raise self.error
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
            self._result = batch_module.run_budget_window_batch_impl(
                self.payload,
                runtime=self.runtime.observability,
            )
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
            result=self.runtime.child_outcome_for_year(simulation_year),
            error=self.runtime.child_errors.get(simulation_year),
        )
        self.runtime.calls[call.object_id] = call
        return call


@pytest.fixture
def budget_window_semi_integration_client(
    monkeypatch,
    observability_runtime,
) -> tuple[TestClient, SemiIntegrationRuntime]:
    runtime = SemiIntegrationRuntime()
    runtime.observability = observability_runtime
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
                "submission_claim_id": "proc-123",
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


def submit_budget_window(client, *, window_size=3, max_parallel=1):
    response = client.post(
        "/simulate/economy/budget-window",
        json={
            "country": "us",
            "region": "us",
            "scope": "macro",
            "reform": {},
            "start_year": "2026",
            "window_size": window_size,
            "max_parallel": max_parallel,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()["batch_job_id"]


def test_typed_child_call_failure_persists_errors_and_stops_the_batch(
    budget_window_semi_integration_client,
):
    """A child that raises ``SPMInputError`` is transported, not redacted."""
    client, runtime = budget_window_semi_integration_client
    runtime.child_errors["2026"] = SPMInputError(
        "SPM_GEOGRAPHY_REQUIRED", "County required"
    )

    batch_job_id = submit_budget_window(client)
    assert client.get(f"/budget-window-jobs/{batch_job_id}").status_code == 202

    failure = client.get(f"/budget-window-jobs/{batch_job_id}")
    assert failure.status_code == 400
    body = failure.json()
    expected = [{"code": "SPM_GEOGRAPHY_REQUIRED", "message": "County required"}]
    assert body["status"] == "failed"
    assert body["errors"] == expected
    assert body["error"] == "County required"
    # ``mark_child_failed`` replaces the child entry, so the scheduler has to
    # re-attach the typed payload after it.
    assert body["child_jobs"]["2026"]["errors"] == expected
    assert body["child_jobs"]["2026"]["status"] == "failed"
    assert body["failed_years"] == ["2026"]
    # The scheduler returns early: the remaining years are never spawned.
    assert body["queued_years"] == ["2027", "2028"]
    assert [payload["time_period"] for payload in runtime.child_payloads] == ["2026"]


def test_typed_result_validation_failure_persists_errors_and_stops_the_batch(
    budget_window_semi_integration_client,
):
    """A mismatched SPM receipt fails the batch with the typed code."""
    client, runtime = budget_window_semi_integration_client
    batch_job_id = submit_budget_window(client)

    # The gateway records the resolved selection on the seed whenever the
    # route advertises a canonical SPM capability; the parent reads it back
    # from the seed to validate each child receipt. Seeding it here keeps the
    # test on the scheduler seam instead of the registry's.
    seed = runtime.dicts[BUDGET_WINDOW_JOB_SEED_DICT_NAME][batch_job_id]
    seed["request_payload"]["spm"] = dict(SPM_SELECTION)
    runtime.child_results["2026"] = spm_child_result(
        runtime, "2026", receipt_year="1999"
    )

    assert client.get(f"/budget-window-jobs/{batch_job_id}").status_code == 202

    failure = client.get(f"/budget-window-jobs/{batch_job_id}")
    assert failure.status_code == 400
    body = failure.json()
    expected = [
        {
            "code": "SPM_CONFIGURATION_UNAVAILABLE",
            "message": "Result SPM provenance does not cover the requested year",
        }
    ]
    assert body["status"] == "failed"
    assert body["errors"] == expected
    assert body["child_jobs"]["2026"]["errors"] == expected
    assert body["failed_years"] == ["2026"]
    assert body["queued_years"] == ["2027", "2028"]
    assert [payload["time_period"] for payload in runtime.child_payloads] == ["2026"]


def test_a_typed_child_failure_cancels_the_siblings_already_running(
    budget_window_semi_integration_client,
):
    """The early return, where a single-parallel batch cannot show it.

    ``poll_running_children_once`` returns False on a typed failure instead
    of continuing round the loop. With ``max_parallel=1`` there is never a
    second running child, so replacing that return with ``continue`` changed
    nothing observable. At two, the sibling that was already running must
    be cancelled rather than harvested: the batch has failed, and a
    completed 2027 alongside a failed 2026 would be a partial window nobody
    asked for.
    """
    client, runtime = budget_window_semi_integration_client
    runtime.child_errors["2026"] = SPMInputError(
        "SPM_GEOGRAPHY_REQUIRED", "County required"
    )

    batch_job_id = submit_budget_window(client, max_parallel=2)
    assert client.get(f"/budget-window-jobs/{batch_job_id}").status_code == 202

    body = client.get(f"/budget-window-jobs/{batch_job_id}").json()

    assert [payload["time_period"] for payload in runtime.child_payloads] == [
        "2026",
        "2027",
    ]
    assert body["completed_years"] == []
    assert body["child_jobs"]["2027"]["status"] == "cancelled"
    assert body["failed_years"] == ["2026"]
    assert body["queued_years"] == ["2028"]


def test_a_typed_child_failure_is_persisted_not_only_returned(
    budget_window_semi_integration_client,
):
    """The poll body is serialized from the state run() returns in memory.

    Both typed branches write the re-attached errors back through
    ``put_batch_job_state`` before returning, and nothing else re-reads the
    batch-state dict, so deleting those writes left the suite green. A
    later poll -- a different container, or the same one after a restart --
    reads the dict, so what is in it is what the client eventually sees.
    """
    client, runtime = budget_window_semi_integration_client
    runtime.child_errors["2026"] = SPMInputError(
        "SPM_GEOGRAPHY_REQUIRED", "County required"
    )

    batch_job_id = submit_budget_window(client)
    assert client.get(f"/budget-window-jobs/{batch_job_id}").status_code == 202
    assert client.get(f"/budget-window-jobs/{batch_job_id}").status_code == 400

    persisted = runtime.dicts[BUDGET_WINDOW_JOB_DICT_NAME][batch_job_id]
    expected = [{"code": "SPM_GEOGRAPHY_REQUIRED", "message": "County required"}]
    assert persisted["status"] == "failed"
    assert persisted["errors"] == expected
    assert persisted["child_jobs"]["2026"]["errors"] == expected


def test_untyped_child_failure_is_redacted_and_carries_no_typed_errors(
    budget_window_semi_integration_client,
):
    """Only public typed errors escape redaction; others stay 500 with no code."""
    client, runtime = budget_window_semi_integration_client
    runtime.child_errors["2026"] = RuntimeError("internal detail")

    batch_job_id = submit_budget_window(client)
    assert client.get(f"/budget-window-jobs/{batch_job_id}").status_code == 202

    failure = client.get(f"/budget-window-jobs/{batch_job_id}")
    assert failure.status_code == 500
    body = failure.json()
    assert "errors" not in body
    assert "internal detail" not in body["error"]
    assert "errors" not in body["child_jobs"]["2026"]


def test_untyped_result_parsing_failure_is_redacted_and_stops_the_batch(
    budget_window_semi_integration_client,
):
    """A malformed child result fails the batch without leaking the reason."""
    client, runtime = budget_window_semi_integration_client
    runtime.child_results["2026"] = {"budget": "not-an-object"}

    batch_job_id = submit_budget_window(client)
    assert client.get(f"/budget-window-jobs/{batch_job_id}").status_code == 202

    failure = client.get(f"/budget-window-jobs/{batch_job_id}")
    assert failure.status_code == 500
    body = failure.json()
    assert "errors" not in body
    assert "missing budget object" not in body["error"]
    assert body["failed_years"] == ["2026"]
    assert body["queued_years"] == ["2027", "2028"]
    assert [payload["time_period"] for payload in runtime.child_payloads] == ["2026"]
