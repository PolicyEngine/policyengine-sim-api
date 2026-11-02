"""Tests for the shared wait window of a Stage 12 report's child simulations."""

from __future__ import annotations

import importlib
import sys

import pytest
from fixtures.fake_modal import install_fake_modal
from policyengine_simulation_executor.stage12_runtime import (
    coordination as coordination_module,
)
from policyengine_simulation_executor.stage12_runtime.comparison import (
    PRODUCTION_RESULT_WAIT_TIMEOUT_SECONDS,
)
from stage12_child_call_support import (
    ScriptedChildInvoker,
    assert_cancelled,
    coordinate_with,
    report_with_segmentation,
    role_simulation_id,
)
from stage12_fake_stores import FakeStore
from stage12_runtime_support import report_execution_input, shared_output_plan


class SteppedClock:
    def __init__(self, start_seconds=10_000.0):
        self.current_seconds = start_seconds

    def monotonic(self):
        return self.current_seconds

    def advance(self, elapsed_seconds):
        self.current_seconds += elapsed_seconds


def _declared_modal_timeouts(monkeypatch):
    install_fake_modal(monkeypatch)
    sys.modules.pop("src.modal.v2_app", None)
    modal_app = importlib.import_module("src.modal.v2_app")
    return {
        name: options.get("timeout") for name, options in modal_app.app.function_calls
    }


# After the child window closes and the production-result wait ends, the
# coordinator still has to read both simulation artifacts, aggregate, write its
# artifacts, and record the outcome before Modal stops it.
MINIMUM_FINISHING_SECONDS = 600


def test_declared_modal_timeouts_fit_the_shared_wait_window(monkeypatch) -> None:
    declared_timeouts = _declared_modal_timeouts(monkeypatch)
    window_seconds = coordination_module.SIMULATION_WAIT_TIMEOUT_SECONDS
    child_worker_timeouts = {
        name: timeout
        for name, timeout in declared_timeouts.items()
        if name.startswith("run_single_simulation")
    }

    assert set(child_worker_timeouts) == {
        "run_single_simulation_us",
        "run_single_simulation_uk",
        "run_single_simulation_segment_us",
    }
    # The window covers every worker's own Modal timeout.
    assert all(window_seconds >= timeout for timeout in child_worker_timeouts.values())
    assert (
        declared_timeouts["coordinate_report"]
        >= window_seconds
        + PRODUCTION_RESULT_WAIT_TIMEOUT_SECONDS
        + MINIMUM_FINISHING_SECONDS
    )


@pytest.mark.parametrize("setup_seconds", [0.0, 400.0, 2_000.0])
def test_child_wait_window_opens_when_the_coordinator_starts(
    monkeypatch,
    setup_seconds,
) -> None:
    declared_timeouts = _declared_modal_timeouts(monkeypatch)
    window_seconds = coordination_module.SIMULATION_WAIT_TIMEOUT_SECONDS
    clock = SteppedClock()
    coordinator_started_at = clock.monotonic()

    def slow_output_planning(_report):
        # Loading the country model is coordinator setup, not child time.
        clock.advance(setup_seconds)
        return shared_output_plan()

    store = FakeStore()
    invoker = ScriptedChildInvoker(clock=clock)

    with pytest.raises(RuntimeError, match="Stage 12 report coordination failed"):
        coordinate_with(
            report_execution_input(),
            store,
            invoker,
            output_plan_resolver=slow_output_planning,
            monotonic=clock.monotonic,
            sleep=clock.advance,
        )

    probed_at = invoker.calls["baseline"].probed_at
    child_waits_began_after = probed_at[0] - coordinator_started_at
    window_closed_after = probed_at[-1] - coordinator_started_at
    assert child_waits_began_after == setup_seconds
    # Setup shortens the children's window rather than the time after it.
    assert window_closed_after == pytest.approx(window_seconds)
    assert probed_at[-1] - probed_at[0] == pytest.approx(window_seconds - setup_seconds)
    assert (
        declared_timeouts["coordinate_report"]
        - window_closed_after
        - PRODUCTION_RESULT_WAIT_TIMEOUT_SECONDS
        >= MINIMUM_FINISHING_SECONDS
    )
    assert store.parent.error_code == "simulation_wait_timeout"
    for role in ("baseline", "reform"):
        assert_cancelled(
            store.children[role_simulation_id(role)],
            after=f"the {window_seconds:g}-second wait window closed",
        )


def test_exhausted_wait_window_gives_the_children_no_extra_time() -> None:
    window_seconds = coordination_module.SIMULATION_WAIT_TIMEOUT_SECONDS
    clock = SteppedClock()

    def setup_that_uses_the_whole_window(_report):
        clock.advance(window_seconds + 60.0)
        return shared_output_plan()

    store = FakeStore()
    invoker = ScriptedChildInvoker(clock=clock)

    with pytest.raises(RuntimeError, match="Stage 12 report coordination failed"):
        coordinate_with(
            report_with_segmentation(segmented=False),
            store,
            invoker,
            output_plan_resolver=setup_that_uses_the_whole_window,
            monotonic=clock.monotonic,
            sleep=lambda _: pytest.fail("an exhausted window must not wait"),
        )

    assert [len(call.probed_at) for call in invoker.calls.values()] == [1, 1]
    assert [call.cancel_count for call in invoker.calls.values()] == [1, 1]
    assert store.parent.error_code == "simulation_wait_timeout"
