"""A household function's last step reports that its call ended to sim."""

from __future__ import annotations

import sys
from uuid import uuid4

import pytest
from test_stage12_modal_app import (
    RecordingRuntime,
    _household_payload,
    _load,
)

HOUSEHOLD_CALL_ID = "fc-household"


class Outcome:
    """A household calculation outcome, as the calculation returns it."""

    def model_dump(self, *, mode):
        """Return the outcome as JSON."""
        assert mode == "json"
        return {"status": "succeeded"}


def _module_reporting_to(monkeypatch, reports: list, calculate):
    """Load the v2 app with a call ID, a report recorder and ``calculate``."""
    from policyengine_simulation_executor import household_calculation

    module = _load(monkeypatch)
    monkeypatch.setattr(
        module, "init_process_observability", lambda **_: RecordingRuntime()
    )
    monkeypatch.setattr(
        sys.modules["modal"],
        "current_function_call_id",
        lambda: HOUSEHOLD_CALL_ID,
        raising=False,
    )
    monkeypatch.setattr(
        module.report_call_ended, "spawn", reports.append, raising=False
    )
    monkeypatch.setattr(household_calculation, "calculate_household", calculate)
    return module


def test_a_household_call_reports_its_runs_when_it_ends(monkeypatch) -> None:
    reports: list = []
    module = _module_reporting_to(monkeypatch, reports, lambda *_, **__: Outcome())
    baseline_run, reform_run = str(uuid4()), str(uuid4())

    result = module.calculate_household_us(
        _household_payload("us"), [baseline_run, reform_run]
    )

    assert result == {"status": "succeeded"}
    assert reports == [
        {
            "modal_call_id": HOUSEHOLD_CALL_ID,
            "simulation_run_ids": [baseline_run, reform_run],
            "report_run_id": None,
        }
    ]


def test_a_household_call_that_raises_still_reports(monkeypatch) -> None:
    reports: list = []

    def fail(*_, **__):
        msg = "the country model could not load"
        raise RuntimeError(msg)

    module = _module_reporting_to(monkeypatch, reports, fail)
    run_id = str(uuid4())

    with pytest.raises(RuntimeError, match="could not load"):
        module.calculate_household_uk(_household_payload("uk"), [run_id])

    assert [report["simulation_run_ids"] for report in reports] == [[run_id]]


def test_a_pre_empted_household_call_reports_nothing(monkeypatch) -> None:
    """Modal re-runs a pre-empted call under the same ID; the re-run reports."""
    reports: list = []

    def pre_empt(*_, **__):
        raise KeyboardInterrupt

    module = _module_reporting_to(monkeypatch, reports, pre_empt)

    with pytest.raises(KeyboardInterrupt):
        module.calculate_household_us(_household_payload("us"), [str(uuid4())])

    assert reports == []


def test_a_household_call_without_runs_reports_nothing(monkeypatch) -> None:
    """Release validation and the image check call the functions without runs."""
    reports: list = []
    module = _module_reporting_to(monkeypatch, reports, lambda *_, **__: Outcome())

    assert module.calculate_household_us(_household_payload("us")) == {
        "status": "succeeded"
    }
    assert reports == []


def test_run_ids_that_are_not_uuids_report_nothing_but_still_calculate(
    monkeypatch,
) -> None:
    reports: list = []
    module = _module_reporting_to(monkeypatch, reports, lambda *_, **__: Outcome())

    result = module.calculate_household_us(_household_payload("us"), ["not-a-run"])

    assert result == {"status": "succeeded"}
    assert reports == []
