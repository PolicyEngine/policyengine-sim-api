"""Tests for the typed span scope of the observability runtime."""

from contextlib import nullcontext

import pytest
from policyengine_simulation_observability import scopes
from policyengine_simulation_observability.observability import build_runtime


class RecordingRuntime:
    """Records the spans it is asked to open, and enters each with a marker."""

    def __init__(self) -> None:
        self.opened_spans: list[tuple[str, object]] = []

    def span(self, name, *, attributes=None):
        self.opened_spans.append((name, attributes))
        return nullcontext("entered span state")


@pytest.fixture
def runtime(monkeypatch):
    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")
    real_runtime = build_runtime(
        service_name="policyengine-simulation-scopes-test",
        service_role="test",
        platform="local",
        environment="test",
    )
    yield real_runtime
    real_runtime.shutdown()


def test_span_opens_the_runtime_span_with_its_name_and_attributes():
    recording_runtime = RecordingRuntime()

    with scopes.span(
        recording_runtime, "simulation.parse", attributes={"country": "us"}
    ) as entered_state:
        assert recording_runtime.opened_spans == [
            ("simulation.parse", {"country": "us"})
        ]

    assert entered_state == "entered span state"


def test_span_defaults_to_no_attributes():
    recording_runtime = RecordingRuntime()

    with scopes.span(recording_runtime, "simulation.parse"):
        pass

    assert recording_runtime.opened_spans == [("simulation.parse", None)]


def test_span_lets_an_exception_raised_inside_it_propagate(runtime):
    message = "invalid simulation request"

    with (
        pytest.raises(ValueError, match=message),
        scopes.span(runtime, "simulation.parse"),
    ):
        raise ValueError(message)
