"""Tests for the typed span and operation scopes of the observability runtime."""

from contextlib import nullcontext

import pytest
from policyengine_simulation_observability import scopes
from policyengine_simulation_observability.observability import build_runtime


class RecordingRuntime:
    """Records the scopes it is asked to open, and enters each with a marker."""

    def __init__(self) -> None:
        self.opened_spans: list[tuple[str, object]] = []
        self.opened_operations: list[tuple[str, object, object]] = []

    def span(self, name, *, attributes=None):
        self.opened_spans.append((name, attributes))
        return nullcontext("entered span state")

    def operation(self, name, *, attributes=None, remote_context=None):
        self.opened_operations.append((name, attributes, remote_context))
        return nullcontext("entered operation state")


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


def test_operation_opens_the_runtime_operation_with_its_context():
    recording_runtime = RecordingRuntime()

    with scopes.operation(
        recording_runtime,
        "simulation.execution",
        attributes={"country": "us"},
        remote_context={"traceparent": "00-trace"},
    ) as entered_state:
        assert recording_runtime.opened_operations == [
            ("simulation.execution", {"country": "us"}, {"traceparent": "00-trace"})
        ]

    assert entered_state == "entered operation state"


def test_operation_defaults_to_no_attributes_or_remote_context():
    recording_runtime = RecordingRuntime()

    with scopes.operation(recording_runtime, "simulation.execution"):
        pass

    assert recording_runtime.opened_operations == [("simulation.execution", None, None)]


def test_operation_lets_an_exception_raised_inside_it_propagate(runtime):
    message = "simulation failed"

    with (
        pytest.raises(RuntimeError, match=message),
        scopes.operation(runtime, "simulation.execution"),
    ):
        raise RuntimeError(message)
