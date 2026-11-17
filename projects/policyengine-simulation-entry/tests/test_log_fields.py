"""Every Simulation Entrypoint log call's fields reach the emitted log record.

The observability runtime's log handler keeps only a record's
``policyengine_attributes``, and drops each of those whose key contains
``household``, ``reform``, ``payload`` or ``person``, among others.
"""

from __future__ import annotations

import ast
from pathlib import Path

import policyengine_simulation_entry
from policyengine_observability import (
    DeploymentIdentity,
    ObservabilityConfig,
    ServiceIdentity,
)
from policyengine_observability.schema import normalize_attributes

PACKAGE_DIRECTORY = Path(policyengine_simulation_entry.__file__).parent
LOG_METHODS = frozenset(
    {"debug", "info", "warning", "error", "exception", "critical", "log"}
)


def _log_calls_with_extra() -> list[tuple[str, ast.Call, ast.expr]]:
    """Return each logging call in the package that passes ``extra=``."""
    calls = []
    for path in sorted(PACKAGE_DIRECTORY.rglob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in LOG_METHODS
            ):
                continue
            for keyword in node.keywords:
                if keyword.arg == "extra":
                    location = f"{path.relative_to(PACKAGE_DIRECTORY)}:{node.lineno}"
                    calls.append((location, node, keyword.value))
    return calls


def _is_log_extra_call(expression: ast.expr) -> bool:
    return (
        isinstance(expression, ast.Call)
        and isinstance(expression.func, ast.Name)
        and expression.func.id == "log_extra"
    )


def test_the_package_has_log_calls_with_fields() -> None:
    # Guards the next tests against finding nothing to check.
    assert len(_log_calls_with_extra()) >= 30


def test_every_log_call_passes_its_fields_through_log_extra() -> None:
    plain_extra = [
        location
        for location, _, extra in _log_calls_with_extra()
        if not _is_log_extra_call(extra)
    ]

    assert plain_extra == []


def test_no_log_field_is_named_with_a_word_the_package_drops() -> None:
    config = ObservabilityConfig(
        service=ServiceIdentity(
            name="policyengine-simulation-entry-test",
            namespace="policyengine.test",
            version="test",
            role="test",
        ),
        deployment=DeploymentIdentity(environment="test", platform="local"),
    )
    field_names = {
        (location, keyword.arg)
        for location, _, extra in _log_calls_with_extra()
        if isinstance(extra, ast.Call)
        for keyword in extra.keywords
        if keyword.arg is not None
    }
    assert field_names

    dropped = sorted(
        (location, name)
        for location, name in field_names
        if not normalize_attributes({name: "value"}, config)[0]
    )

    assert dropped == []
