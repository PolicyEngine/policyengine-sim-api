"""The staging budget-window qualification's comparison with the old gateway."""

from __future__ import annotations

import importlib.util
from pathlib import Path

QUALIFICATION = (
    Path(__file__).resolve().parents[1]
    / "authenticated_tests"
    / "test_staging_budget_window_qualification.py"
)


def _qualification():
    spec = importlib.util.spec_from_file_location(
        "budget_window_qualification", QUALIFICATION
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _result(*budgetary_impacts: float) -> dict:
    def row(year: str, budgetary_impact: float) -> dict:
        return {
            "year": year,
            "taxRevenueImpact": 100.0,
            "federalTaxRevenueImpact": 80.0,
            "stateTaxRevenueImpact": 20.0,
            "benefitSpendingImpact": 50.0,
            "budgetaryImpact": budgetary_impact,
        }

    return {
        "annualImpacts": [
            row(str(2026 + offset), value)
            for offset, value in enumerate(budgetary_impacts)
        ],
        "totals": row("Total", sum(budgetary_impacts)),
    }


def test_windows_within_the_tolerance_have_no_differences() -> None:
    qualification = _qualification()

    assert (
        qualification.window_differences(
            _result(1_000_000.0, 2_000_000.0),
            _result(1_000_500.0, 1_999_000.0),
            relative=0.001,
            absolute=0.0,
        )
        == []
    )


def test_a_year_and_its_total_outside_the_tolerance_are_named() -> None:
    qualification = _qualification()

    differences = qualification.window_differences(
        _result(1_000_000.0, 2_000_000.0),
        _result(1_000_000.0, 2_100_000.0),
        relative=0.001,
        absolute=1000.0,
    )

    assert differences == [
        "2027 budgetaryImpact: v2 2000000.0 against the old gateway's 2100000.0",
        "Total budgetaryImpact: v2 3000000.0 against the old gateway's 3100000.0",
    ]


def test_a_year_only_one_path_has_is_a_difference() -> None:
    qualification = _qualification()

    assert qualification.window_differences(
        _result(1.0, 2.0), _result(1.0), relative=0.001, absolute=1000.0
    ) == ["2027: only one path has it"]


def test_the_qualification_runs_only_when_an_operator_asks(monkeypatch) -> None:
    monkeypatch.delenv("SIMULATION_STAGING_BUDGET_WINDOW_QUALIFICATION", raising=False)
    qualification = _qualification()

    marker = qualification.pytestmark.mark
    assert marker.name == "skipif"
    assert marker.args == (True,)


def test_a_failed_old_gateway_batch_is_read_as_its_status() -> None:
    """The old gateway answers 500 with the batch's status when it failed."""
    import httpx

    qualification = _qualification()
    request = httpx.Request("GET", "https://staging.example/budget-window-jobs/fc-1")
    failed = httpx.Response(
        500, json={"status": "failed", "error": "year 2027 failed"}, request=request
    )
    running = httpx.Response(200, json={"status": "running"}, request=request)

    assert qualification.old_gateway_status(failed) == {
        "status": "failed",
        "error": "year 2027 failed",
    }
    assert qualification.old_gateway_status(running) == {"status": "running"}
    for unreadable in (
        httpx.Response(502, text="Bad Gateway", request=request),
        httpx.Response(500, json={"detail": "Internal Server Error"}, request=request),
    ):
        try:
            qualification.old_gateway_status(unreadable)
        except httpx.HTTPStatusError:
            continue
        msg = f"{unreadable.status_code} was read as a status"
        raise AssertionError(msg)


def test_the_qualification_uses_the_cap_sim_uses() -> None:
    from policyengine_simulation_contract.economy_runs import (
        BUDGET_WINDOW_MAX_CONCURRENT_YEARS,
    )

    assert _qualification().MAX_CONCURRENT_YEARS is BUDGET_WINDOW_MAX_CONCURRENT_YEARS
