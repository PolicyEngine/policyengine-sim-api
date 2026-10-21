"""Tests for .github/scripts/check_pr_description.py.

Rule sim.process.pr-tracking-issue: the first line of every pull request's
description is ``Fixes #ISSUE_NUMBER``. These tests load the script in this
process to check how it reads a description.
"""

from __future__ import annotations

import importlib.util
from typing import TYPE_CHECKING

import pytest
from workflow_script_support import SCRIPTS_DIR

if TYPE_CHECKING:
    from types import ModuleType

CHECK_SCRIPT = SCRIPTS_DIR / "check_pr_description.py"


@pytest.fixture(scope="module")
def check_module() -> ModuleType:
    """Load the check script in this process, as the workflow runs it."""
    spec = importlib.util.spec_from_file_location("check_pr_description", CHECK_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _rejection(check_module: ModuleType, body: str | None) -> str:
    """Return the message the check gives for a description it rejects."""
    with pytest.raises(check_module.DescriptionError) as rejected:
        check_module.issue_number_from_description(body)
    return str(rejected.value)


# --- The first line -------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        "Fixes #861",
        "Fixes #861\n\n## Summary\n\n- Adds a check.\n",
    ],
)
def test_a_first_line_of_fixes_and_a_number_names_the_issue(check_module, body):
    assert check_module.issue_number_from_description(body) == 861


@pytest.mark.parametrize(
    "body",
    [
        # Descriptions edited in the GitHub web interface use CRLF.
        "Fixes #861\r\n\r\n## Summary\r\n",
        # A carriage return on its own also ends a line in Markdown.
        "Fixes #861\r## Summary",
    ],
)
def test_carriage_return_line_endings_end_the_first_line(check_module, body):
    assert check_module.issue_number_from_description(body) == 861


def test_trailing_whitespace_after_the_number_is_ignored(check_module):
    # From PR #405, edited in the web interface: a space, then CRLF.
    body = "Fixes #404 \r\n\r\n## Summary\r\n"

    assert check_module.issue_number_from_description(body) == 404


def test_a_description_without_a_fixes_line_is_rejected(check_module):
    message = _rejection(check_module, "## Summary\n\n- Adds a check.\n")

    assert 'The first line is "## Summary"' in message
    assert "first line is exactly Fixes #ISSUE_NUMBER" in message
    assert "sim.process.pr-tracking-issue" in message


def test_a_fixes_line_that_is_not_first_is_rejected(check_module):
    message = _rejection(check_module, "## Summary\n\nFixes #861\n")

    assert "Fixes #861 is on line 3" in message
    assert "must be the first line" in message


def test_a_blank_line_before_the_fixes_line_is_rejected(check_module):
    message = _rejection(check_module, "\r\nFixes #861\r\n")

    assert "Fixes #861 is on line 2" in message


def test_leading_whitespace_before_fixes_is_rejected(check_module):
    # Four leading spaces would make the line a Markdown code block, where
    # GitHub does not read closing keywords.
    message = _rejection(check_module, "    Fixes #861\n")

    assert "starts with whitespace" in message


@pytest.mark.parametrize("body", [None, "", " \r\n\t\n"])
def test_an_empty_or_missing_description_is_rejected(check_module, body):
    message = _rejection(check_module, body)

    assert "The description is empty." in message
    assert "first line is exactly Fixes #ISSUE_NUMBER" in message


@pytest.mark.parametrize(
    "first_line",
    [
        # From PR #616.
        "Fixes #615.",
        # From PR #543.
        "Refs #434",
        # From PR #750.
        "Fixes # (no tracking issue exists yet.)",
        "fixes #861",
        "Closes #861",
        "Fixes 861",
        "Fixes #",
        "Fixes #0",
        "Fixes #0861",
        "Fixes #861 and #862",
        "Fixes PolicyEngine/policyengine-api#3000",
        "Fixes https://github.com/PolicyEngine/policyengine-sim-api/issues/861",
        # Digits from another script are not issue numbers.
        "Fixes #٨٦١",
    ],
)
def test_other_first_lines_are_rejected(check_module, first_line):
    message = _rejection(check_module, f"{first_line}\n\n## Summary\n")

    assert f'The first line is "{first_line}"' in message


def test_a_long_first_line_is_shortened_in_the_message(check_module):
    first_line = "Draft, stacked on #743. " + "Gate releases on a sentinel. " * 10

    message = _rejection(check_module, first_line)

    assert 'The first line is "Draft, stacked on #743.' in message
    assert first_line not in message
