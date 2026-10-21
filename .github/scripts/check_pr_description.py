"""Check that a pull request's description names its tracking issue.

Rule sim.process.pr-tracking-issue: the first line of every pull request's
description is ``Fixes #ISSUE_NUMBER``.

How the first line is read:

- A line ends at a CRLF, a carriage return or a line feed, as in Markdown, so
  descriptions edited in the GitHub web interface (which uses CRLF) pass.
- Trailing spaces and tabs are ignored: they don't show, and GitHub still reads
  the closing keyword.
- Leading whitespace fails. Four leading spaces make the line a Markdown code
  block, where GitHub doesn't read closing keywords, and any indent is a sign
  the line isn't what the author meant.
- An empty or missing (``null``) description fails.
- The number is ASCII digits without a leading zero.
"""

from __future__ import annotations

import re

RULE_ID = "sim.process.pr-tracking-issue"
HOW_TO_FIX = (
    "Edit the description so its first line is exactly Fixes #ISSUE_NUMBER, "
    "where ISSUE_NUMBER is an open issue in this repository; editing the "
    "description runs this check again. See docs/engineering/skills/github-prs.md "
    f"(rule {RULE_ID})."
)
FIXES_LINE = re.compile(r"Fixes #([1-9][0-9]*)")
# Markdown line endings: CRLF, a carriage return alone, or a line feed.
LINE_ENDING = re.compile(r"\r\n|\r|\n")
# How much of a rejected first line a message quotes.
SHOWN_FIRST_LINE_LENGTH = 80


class DescriptionError(Exception):
    """The description, or the issue it names, does not satisfy the rule."""


def _shown(line: str) -> str:
    """Return ``line`` quoted for a message, shortened when it is long."""
    if len(line) > SHOWN_FIRST_LINE_LENGTH:
        line = line[:SHOWN_FIRST_LINE_LENGTH].rstrip() + "..."
    return f'"{line}"'


def issue_number_from_description(body: str | None) -> int:
    """Return the issue number on the description's first line.

    Raise ``DescriptionError``, with a message that says how to fix the
    description, unless the first line is ``Fixes #ISSUE_NUMBER``.
    """
    if body is None or not body.strip():
        msg = f"The description is empty. {HOW_TO_FIX}"
        raise DescriptionError(msg)

    lines = [line.rstrip(" \t") for line in LINE_ENDING.split(body)]
    first_line = lines[0]
    match = FIXES_LINE.fullmatch(first_line)
    if match:
        return int(match.group(1))

    if FIXES_LINE.fullmatch(first_line.lstrip()):
        msg = (
            f"The first line starts with whitespace: {_shown(first_line)}. {HOW_TO_FIX}"
        )
        raise DescriptionError(msg)
    for line_number, line in enumerate(lines[1:], start=2):
        if FIXES_LINE.fullmatch(line.strip()):
            msg = (
                f"{line.strip()} is on line {line_number}, but it must be the "
                f"first line. {HOW_TO_FIX}"
            )
            raise DescriptionError(msg)
    msg = f"The first line is {_shown(first_line)}. {HOW_TO_FIX}"
    raise DescriptionError(msg)
