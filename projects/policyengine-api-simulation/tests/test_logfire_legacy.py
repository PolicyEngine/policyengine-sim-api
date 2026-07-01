"""Tests for legacy Logfire helper behavior."""

from __future__ import annotations

import sys
from contextlib import nullcontext

from src.modal import logfire_legacy


class _FakeLogfire:
    def __init__(self) -> None:
        self.configure_calls = []
        self.span_calls = []
        self.flush_calls = 0
        self.span_context = nullcontext("span")

    def configure(self, **kwargs):
        self.configure_calls.append(kwargs)

    def span(self, name, **kwargs):
        self.span_calls.append((name, kwargs))
        return self.span_context

    def force_flush(self):
        self.flush_calls += 1


def test_configure_logfire_skips_without_token(monkeypatch):
    fake_logfire = _FakeLogfire()
    monkeypatch.delenv("LOGFIRE_TOKEN", raising=False)
    monkeypatch.setitem(sys.modules, "logfire", fake_logfire)

    assert logfire_legacy.configure_logfire("policyengine-simulation") is False
    assert fake_logfire.configure_calls == []


def test_configure_logfire_uses_token_and_environment(monkeypatch):
    fake_logfire = _FakeLogfire()
    monkeypatch.setenv("LOGFIRE_TOKEN", "token-123")
    monkeypatch.setenv("LOGFIRE_ENVIRONMENT", "staging")
    monkeypatch.setenv("MODAL_ENVIRONMENT", "main")
    monkeypatch.setitem(sys.modules, "logfire", fake_logfire)

    assert logfire_legacy.configure_logfire("policyengine-simulation") is True
    assert fake_logfire.configure_calls == [
        {
            "service_name": "policyengine-simulation",
            "token": "token-123",
            "environment": "staging",
            "console": False,
        }
    ]


def test_configure_logfire_falls_back_to_default_environment(monkeypatch):
    fake_logfire = _FakeLogfire()
    monkeypatch.setenv("LOGFIRE_TOKEN", "token-123")
    monkeypatch.delenv("LOGFIRE_ENVIRONMENT", raising=False)
    monkeypatch.setenv("MODAL_ENVIRONMENT", "main")
    monkeypatch.setitem(sys.modules, "logfire", fake_logfire)

    logfire_legacy.configure_logfire(
        "policyengine-simulation",
        default_environment="development",
    )

    assert fake_logfire.configure_calls[0]["environment"] == "development"


def test_logfire_span_noops_when_disabled():
    with logfire_legacy.logfire_span(False, "run_simulation") as span:
        assert span is None


def test_logfire_span_delegates_when_enabled(monkeypatch):
    fake_logfire = _FakeLogfire()
    monkeypatch.setitem(sys.modules, "logfire", fake_logfire)

    with logfire_legacy.logfire_span(True, "run_simulation", country="us") as span:
        assert span == "span"

    assert fake_logfire.span_calls == [("run_simulation", {"country": "us"})]


def test_flush_logfire_only_flushes_when_enabled(monkeypatch):
    fake_logfire = _FakeLogfire()
    monkeypatch.setitem(sys.modules, "logfire", fake_logfire)

    logfire_legacy.flush_logfire(False)
    logfire_legacy.flush_logfire(True)

    assert fake_logfire.flush_calls == 1
