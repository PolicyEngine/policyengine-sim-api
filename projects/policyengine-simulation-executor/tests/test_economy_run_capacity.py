"""The v2 economy functions have room for budget windows calculated at their cap.

The Simulation Entrypoint calculates at most
``BUDGET_WINDOW_MAX_CONCURRENT_YEARS`` years of a budget window at once, each
year up to two simulation calls and one comparison call. The functions keep
the room they had for other reports, and a US national simulation call runs
one segment call per region group.
"""

from __future__ import annotations

from policyengine_simulation_contract.economy_runs import (
    BUDGET_WINDOW_MAX_CONCURRENT_YEARS,
    ECONOMY_COMPARISON_FUNCTION,
    ECONOMY_SIMULATION_FUNCTIONS,
)
from policyengine_simulation_executor.national_partition import (
    US_NATIONAL_REGION_GROUPS,
)
from test_stage12_modal_app import _load

# Budget windows each function has room for at once, every year at the cap,
# beside the containers the functions had for other reports.
WINDOWS_AT_ONCE = 3
OTHER_REPORTS = 10
SIMULATIONS_PER_YEAR = 2


def test_a_budget_window_calculates_a_few_years_at_once() -> None:
    assert BUDGET_WINDOW_MAX_CONCURRENT_YEARS == 5


def test_the_simulation_and_comparison_functions_hold_three_windows_and_more(
    monkeypatch,
) -> None:
    functions = dict(_load(monkeypatch).app.function_calls)
    years = WINDOWS_AT_ONCE * BUDGET_WINDOW_MAX_CONCURRENT_YEARS

    for country in ("us", "uk"):
        simulation = functions[ECONOMY_SIMULATION_FUNCTIONS[country]]
        assert (
            simulation["max_containers"]
            == (SIMULATIONS_PER_YEAR * years + OTHER_REPORTS)
            == 40
        )
    assert (
        functions[ECONOMY_COMPARISON_FUNCTION]["max_containers"]
        == (years + OTHER_REPORTS)
        == 25
    )


def test_every_us_simulation_call_at_once_has_room_for_its_segments(
    monkeypatch,
) -> None:
    """A segment queued behind the limit counts against its caller's time limit."""
    functions = dict(_load(monkeypatch).app.function_calls)
    us_simulation_calls = (
        functions[ECONOMY_SIMULATION_FUNCTIONS["us"]]["max_containers"]
        + functions["run_single_simulation_us"]["max_containers"]
    )

    assert (
        functions["run_single_simulation_segment_us"]["max_containers"]
        == (us_simulation_calls * len(US_NATIONAL_REGION_GROUPS))
        == 1000
    )
