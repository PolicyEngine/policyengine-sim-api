"""Every Simulation Entrypoint log call's fields reach the emitted log record.

The observability runtime's log handler keeps only a record's
``policyengine_attributes``, and drops each of those whose key contains
``household``, ``reform``, ``payload`` or ``person``, among others.
"""

from __future__ import annotations

import ast
import logging
from pathlib import Path

import policyengine_simulation_entry
from conftest import FakeBackend, make_settings
from policyengine_observability import (
    DeploymentIdentity,
    ObservabilityConfig,
    ServiceIdentity,
)
from policyengine_observability.schema import normalize_attributes
from policyengine_simulation_entry.app import create_app

PACKAGE_DIRECTORY = Path(policyengine_simulation_entry.__file__).parent
PROOF_FIXTURES = (
    Path(__file__).resolve().parents[3]
    / "rules"
    / "fixtures"
    / "sim.observability.log-fields-through-log-extra"
)
LOG_METHODS = frozenset(
    {"debug", "info", "warning", "error", "exception", "critical", "log"}
)
FILTER_CONFIG = ObservabilityConfig(
    service=ServiceIdentity(
        name="policyengine-simulation-entry-test",
        namespace="policyengine.test",
        version="test",
        role="test",
    ),
    deployment=DeploymentIdentity(environment="test", platform="local"),
)


def _log_calls_with_extra(paths: list[Path]) -> list[tuple[str, ast.expr]]:
    """Return each logging call in ``paths`` that passes ``extra=``, with its value.

    Each call is named ``file:line``.
    """
    calls = []
    for path in sorted(paths):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in LOG_METHODS
            ):
                continue
            calls.extend(
                (f"{path.name}:{node.lineno}", keyword.value)
                for keyword in node.keywords
                if keyword.arg == "extra"
            )
    return calls


def _is_log_extra_call(expression: ast.expr) -> bool:
    return (
        isinstance(expression, ast.Call)
        and isinstance(expression.func, ast.Name)
        and expression.func.id == "log_extra"
    )


def _plain_extra_calls(calls: list[tuple[str, ast.expr]]) -> list[str]:
    """Return the calls whose fields don't go through ``log_extra``."""
    return [location for location, extra in calls if not _is_log_extra_call(extra)]


def _dropped_field_names(calls: list[tuple[str, ast.expr]]) -> list[tuple[str, str]]:
    """Return each field named in a ``log_extra`` call that the package drops.

    Only names written as keywords are seen, not keys unpacked with ``**``.
    """
    field_names = {
        (location, keyword.arg)
        for location, extra in calls
        if _is_log_extra_call(extra) and isinstance(extra, ast.Call)
        for keyword in extra.keywords
        if keyword.arg is not None
    }
    return sorted(
        (location, name)
        for location, name in field_names
        if not normalize_attributes({name: "value"}, FILTER_CONFIG)[0]
    )


def _package_log_calls() -> list[tuple[str, ast.expr]]:
    return _log_calls_with_extra(list(PACKAGE_DIRECTORY.rglob("*.py")))


def test_the_package_has_log_calls_with_fields() -> None:
    # Guards the next tests against finding nothing to check.
    assert len(_package_log_calls()) >= 30


def test_every_log_call_passes_its_fields_through_log_extra() -> None:
    assert _plain_extra_calls(_package_log_calls()) == []


def test_no_log_field_is_named_with_a_word_the_package_drops() -> None:
    assert _dropped_field_names(_package_log_calls()) == []


def test_rule_proof_log_fields_through_log_extra() -> None:
    """Proof for rule sim.observability.log-fields-through-log-extra."""
    bad_calls = _log_calls_with_extra([PROOF_FIXTURES / "bad_entry_logs.py"])
    good_calls = _log_calls_with_extra([PROOF_FIXTURES / "good_entry_logs.py"])

    assert _plain_extra_calls(bad_calls) == ["bad_entry_logs.py:22"]
    assert _dropped_field_names(bad_calls) == [
        ("bad_entry_logs.py:30", "household_job_id")
    ]
    assert _plain_extra_calls(good_calls) == []
    assert _dropped_field_names(good_calls) == []
    assert good_calls


def test_the_entrypoint_emits_its_info_level_log_records() -> None:
    # The root logger stays at WARNING, so without a level of its own an INFO
    # record, such as ``budget_window_year_deferred``, is discarded before the
    # observability runtime's handler sees it.
    package_logger = logging.getLogger("policyengine_simulation_entry")
    previous_level = package_logger.level
    # ``setLevel`` also clears the loggers' cached answers to ``isEnabledFor``.
    package_logger.setLevel(logging.NOTSET)
    try:
        create_app(
            settings=make_settings(),
            backend=FakeBackend(),
            auth_dependency=lambda: None,
        )

        assert logging.getLogger(
            "policyengine_simulation_entry.budget_window_submissions"
        ).isEnabledFor(logging.INFO)
    finally:
        package_logger.setLevel(previous_level)
