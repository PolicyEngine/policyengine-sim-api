"""The v2 app's economy functions: each reports that its call ended."""

from __future__ import annotations

from uuid import UUID

import pytest
from policyengine_simulation_contract.economy_runs import (
    ECONOMY_COMPARISON_FUNCTION,
    ECONOMY_SIMULATION_FUNCTIONS,
)
from policyengine_simulation_executor.economy_runs import (
    comparison_call,
    simulation_call,
)
from test_stage12_modal_app import RecordingRuntime, _load, _observability_context

RUN_ID = "00000000-0000-0000-0000-0000000000a1"
REPORT_RUN_ID = "00000000-0000-0000-0000-0000000000c1"
CALL_ID = "fc-01JB3W7X9Z8Q2R5T6Y4U1I0O9P"


@pytest.fixture
def v2_app(monkeypatch):
    module = _load(monkeypatch)
    runtime = RecordingRuntime()
    started: list[dict] = []
    monkeypatch.setattr(module, "init_process_observability", lambda **_: runtime)
    monkeypatch.setattr(
        module.modal, "current_function_call_id", lambda: CALL_ID, raising=False
    )
    monkeypatch.setattr(module, "start_call_report", started.append)
    module.started_reports = started
    module.recording_runtime = runtime
    return module


def test_the_v2_app_declares_each_economy_function_sim_starts(monkeypatch) -> None:
    module = _load(monkeypatch)
    functions = dict(module.app.function_calls)

    us = functions[ECONOMY_SIMULATION_FUNCTIONS["us"]]
    uk = functions[ECONOMY_SIMULATION_FUNCTIONS["uk"]]
    comparison = functions[ECONOMY_COMPARISON_FUNCTION]
    assert us["image"] is module.us_worker_image
    assert uk["image"] is module.uk_worker_image
    assert comparison["image"] is module.coordinator_image
    # The time limits of the Stage 12 workers, which a restart does not extend.
    assert us["timeout"] == functions["run_single_simulation_us"]["timeout"]
    assert uk["timeout"] == functions["run_single_simulation_uk"]["timeout"]
    for declaration in (us, uk, comparison):
        assert declaration["retries"] == 0
        assert declaration["secrets"] == module.worker_secrets


def test_a_simulation_call_reports_its_end_with_its_run(v2_app, monkeypatch) -> None:
    received = []

    def calculate(payload, **kwargs):
        received.append((payload, kwargs))
        return {"stored": True}

    monkeypatch.setattr(simulation_call, "run_economy_simulation", calculate)

    result = v2_app.run_economy_simulation_uk(
        {"simulation_run_id": RUN_ID}, observability_context=_observability_context()
    )

    assert result == {"stored": True}
    assert received[0][1]["required_country"] == "uk"
    assert (
        received[0][1]["invocation"].simulation_callable
        == (ECONOMY_SIMULATION_FUNCTIONS["uk"])
    )
    assert received[0][1]["invocation"].application_name == v2_app.APP_NAME
    assert v2_app.started_reports == [
        {
            "modal_call_id": CALL_ID,
            "simulation_run_ids": [RUN_ID],
            "report_run_id": None,
        }
    ]
    assert v2_app.recording_runtime.shutdown_calls == 1


def test_a_failed_simulation_call_reports_its_end_and_raises(
    v2_app, monkeypatch
) -> None:
    def fail(_payload, **_kwargs):
        msg = "calculation failed"
        raise LookupError(msg)

    monkeypatch.setattr(simulation_call, "run_economy_simulation", fail)

    with pytest.raises(LookupError, match="calculation failed"):
        v2_app.run_economy_simulation_us({"simulation_run_id": RUN_ID, "year": "x"})

    assert [report["simulation_run_ids"] for report in v2_app.started_reports] == [
        [RUN_ID]
    ]


def test_a_preempted_simulation_call_reports_nothing_until_its_rerun_ends(
    v2_app, monkeypatch
) -> None:
    attempts = []

    def preempted_once(_payload, **_kwargs):
        attempts.append("attempt")
        if len(attempts) == 1:
            # Modal delivers pre-emption to a synchronous input this way.
            raise KeyboardInterrupt
        return {"stored": True}

    monkeypatch.setattr(simulation_call, "run_economy_simulation", preempted_once)

    with pytest.raises(KeyboardInterrupt):
        v2_app.run_economy_simulation_us({"simulation_run_id": RUN_ID})
    assert v2_app.started_reports == []

    # Modal runs the same input again under the same call ID.
    assert v2_app.run_economy_simulation_us({"simulation_run_id": RUN_ID}) == {
        "stored": True
    }
    assert [report["modal_call_id"] for report in v2_app.started_reports] == [CALL_ID]


def test_a_call_without_a_run_it_can_name_reports_nothing(v2_app, monkeypatch) -> None:
    monkeypatch.setattr(
        simulation_call, "run_economy_simulation", lambda *_args, **_kwargs: {}
    )

    v2_app.run_economy_simulation_us({"simulation_run_id": "not-a-run"})

    assert v2_app.started_reports == []


def test_a_us_simulation_call_segments_in_this_application(v2_app, monkeypatch) -> None:
    from policyengine_simulation_executor import stage12_runtime

    segmented = []
    monkeypatch.setattr(
        stage12_runtime,
        "run_segmented_simulation",
        lambda simulation, **kwargs: segmented.append(kwargs) or "merged",
    )

    def calculate(_payload, *, dependencies, **_kwargs):
        calculator = dependencies.worker.segmented_calculator
        return {"calculated": calculator("simulation", lambda: False)}

    monkeypatch.setattr(simulation_call, "run_economy_simulation", calculate)

    assert v2_app.run_economy_simulation_us({"simulation_run_id": RUN_ID}) == {
        "calculated": "merged"
    }
    assert segmented[0]["app_name"] == v2_app.APP_NAME
    assert segmented[0]["runtime"] is v2_app.recording_runtime


def test_a_comparison_call_reports_its_end_with_its_report_run(
    v2_app, monkeypatch
) -> None:
    received = []

    def compare(payload, *, dependencies):
        received.append(dependencies)
        return {"budget": {}}

    monkeypatch.setattr(comparison_call, "compare_economy_simulations", compare)

    result = v2_app.compare_economy_simulations(
        {"report_run_id": REPORT_RUN_ID},
        observability_context=_observability_context(),
    )

    assert result == {"budget": {}}
    assert received[0].runtime is v2_app.recording_runtime
    assert v2_app.started_reports == [
        {
            "modal_call_id": CALL_ID,
            "simulation_run_ids": None,
            "report_run_id": REPORT_RUN_ID,
        }
    ]
    assert UUID(v2_app.started_reports[0]["report_run_id"])
