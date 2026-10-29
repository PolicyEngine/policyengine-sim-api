"""The v2 economy functions have room for budget windows calculated at their cap.

The Simulation Entrypoint calculates at most
``BUDGET_WINDOW_MAX_CONCURRENT_YEARS`` years of a budget window at once, each
year up to two simulation calls and one comparison call, and a US national
simulation call runs one segment call per region group.
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

# Budget windows each function has room for at once, every year at the cap.
WINDOWS_AT_ONCE = 4
US_NATIONAL_WINDOWS_AT_ONCE = 3
SIMULATIONS_PER_YEAR = 2


def test_a_budget_window_calculates_a_few_years_at_once() -> None:
    assert BUDGET_WINDOW_MAX_CONCURRENT_YEARS == 5


def test_the_simulation_and_comparison_functions_hold_four_windows(
    monkeypatch,
) -> None:
    functions = dict(_load(monkeypatch).app.function_calls)
    years = WINDOWS_AT_ONCE * BUDGET_WINDOW_MAX_CONCURRENT_YEARS

    for country in ("us", "uk"):
        simulation = functions[ECONOMY_SIMULATION_FUNCTIONS[country]]
        assert simulation["max_containers"] == SIMULATIONS_PER_YEAR * years == 40
    assert functions[ECONOMY_COMPARISON_FUNCTION]["max_containers"] == 2 * years


def test_the_us_segment_function_holds_three_national_windows(monkeypatch) -> None:
    functions = dict(_load(monkeypatch).app.function_calls)

    assert (
        functions["run_single_simulation_segment_us"]["max_containers"]
        == (
            US_NATIONAL_WINDOWS_AT_ONCE
            * BUDGET_WINDOW_MAX_CONCURRENT_YEARS
            * SIMULATIONS_PER_YEAR
            * len(US_NATIONAL_REGION_GROUPS)
        )
        == 600
    )
