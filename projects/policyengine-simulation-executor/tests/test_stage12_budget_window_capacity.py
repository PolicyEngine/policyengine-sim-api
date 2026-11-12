"""The v2 functions have room for budget windows running at their cap.

A budget window runs at most ``BUDGET_WINDOW_MAX_RUNNING_YEARS`` years at
once, each year one coordinator call that waits for up to two simulation
calls, and a US national simulation call waits for one segment call per
region group. A call queued behind a function's container limit counts
against its caller's wait, so each function has room for every call its
callers may make at once.
"""

from __future__ import annotations

from policyengine_simulation_executor.stage12_runtime.partition import (
    US_REGION_GROUPS,
)
from policyengine_stage12_persistence import BUDGET_WINDOW_MAX_RUNNING_YEARS
from test_stage12_modal_app import _load

# Budget windows the coordinator has room for at once, each at its cap,
# beside the coordinator calls it had for other reports.
WINDOWS_AT_ONCE = 3
OTHER_REPORTS = 10
SIMULATIONS_PER_REPORT = 2


def test_the_coordinator_holds_three_windows_at_their_cap_and_other_reports(
    monkeypatch,
) -> None:
    functions = dict(_load(monkeypatch).app.function_calls)

    assert (
        functions["coordinate_report"]["max_containers"]
        == WINDOWS_AT_ONCE * BUDGET_WINDOW_MAX_RUNNING_YEARS + OTHER_REPORTS
        == 25
    )


def test_each_simulation_worker_holds_both_simulations_of_every_coordinator(
    monkeypatch,
) -> None:
    functions = dict(_load(monkeypatch).app.function_calls)
    coordinators = functions["coordinate_report"]["max_containers"]

    for worker in ("run_single_simulation_us", "run_single_simulation_uk"):
        assert (
            functions[worker]["max_containers"]
            == SIMULATIONS_PER_REPORT * coordinators
            == 50
        )


def test_every_us_simulation_at_once_has_room_for_its_segments(monkeypatch) -> None:
    functions = dict(_load(monkeypatch).app.function_calls)

    assert (
        functions["run_single_simulation_segment_us"]["max_containers"]
        == functions["run_single_simulation_us"]["max_containers"]
        * len(US_REGION_GROUPS)
        == 1000
    )
