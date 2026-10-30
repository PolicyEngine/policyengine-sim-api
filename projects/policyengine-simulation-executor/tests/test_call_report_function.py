"""The v2 app's report_call_ended function, which tells sim that a call ended."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from policyengine_simulation_contract.call_reports import CALL_REPORT_FUNCTION_NAME
from src.modal import call_reports
from test_stage12_modal_app import _load

ENTRY_URL = "https://policyengine-simulation-entry-abc.a.run.app"
PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_the_v2_app_declares_the_function_sim_accepts_reports_from(
    monkeypatch,
) -> None:
    monkeypatch.setenv("SIMULATION_ENTRY_URL", ENTRY_URL)
    module = _load(monkeypatch)

    declaration = dict(module.app.function_calls)[CALL_REPORT_FUNCTION_NAME]

    assert declaration["image"] is module.coordinator_image
    assert declaration["env"] == {"SIMULATION_ENTRY_URL": ENTRY_URL}
    assert declaration["retries"] == 0
    # Every attempt, each with its request timeout, and every wait between them.
    assert declaration["timeout"] >= call_reports.CALL_REPORT_ATTEMPTS * (
        call_reports.REQUEST_TIMEOUT_SECONDS + call_reports.LONGEST_WAIT_SECONDS
    )
    # It needs no secret: its own identity token proves who it is.
    assert "secrets" not in declaration


def test_the_function_sends_the_report_with_its_containers_settings(
    monkeypatch,
) -> None:
    module = _load(monkeypatch)
    sent = []
    monkeypatch.setattr(
        call_reports,
        "send_call_ended_report_from_container",
        lambda report: sent.append(report) or "delivered",
    )
    report = {"modal_call_id": "fc-ended", "report_run_id": "x"}

    assert module.report_call_ended(report) == "delivered"
    assert sent == [report]


@pytest.mark.parametrize("url", ["http://entry.example", "entry.example", " https://x"])
def test_a_deploy_with_a_url_that_is_not_https_stops(monkeypatch, url) -> None:
    monkeypatch.setenv("SIMULATION_ENTRY_URL", url)

    with pytest.raises(RuntimeError, match="SIMULATION_ENTRY_URL"):
        _load(monkeypatch)


def test_the_v2_app_imports_with_the_real_modal_package() -> None:
    """`modal deploy` imports the module with Modal's own decorators.

    There a declared function is a ``modal.Function``, not the decorated
    Python function, so nothing at import may rely on its Python attributes.
    """
    environment = {
        name: value
        for name, value in os.environ.items()
        if not name.startswith(("MODAL_", "STAGE12_", "SIMULATION_ENTRY_"))
    }

    result = subprocess.run(
        [sys.executable, "-c", "import src.modal.v2_app"],
        cwd=PROJECT_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
    )

    assert result.returncode == 0, result.stderr[-2000:]
