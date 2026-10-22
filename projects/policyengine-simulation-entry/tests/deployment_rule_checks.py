"""Checks behind two deployment rules, runnable on the repository or a fixture.

- ``combined_secret_resolutions`` (sim.deploy.workflow-shell-safety): Bash
  gives an array assignment the exit status of its last command substitution
  only, so an assignment that resolves several secrets lets an earlier
  failure pass unnoticed.
- ``libraries_missing_from_context`` (sim.deploy.container-context-minimal):
  every local library the entrypoint installs must be in its build context.
"""

from __future__ import annotations

import re
from pathlib import Path

import tomllib

ARRAY_ASSIGNMENT = re.compile(r"^\s*\w+\+?=\(", re.MULTILINE)
# The line that closes a multi-line array assignment: only its parenthesis.
ARRAY_END = re.compile(r"\n[ \t]*\)")
SECRET_RESOLUTION = "$(resolve_secret_reference"


def combined_secret_resolutions(script_text: str) -> list[str]:
    """Return array assignments that resolve more than one secret."""
    violations = []
    for match in ARRAY_ASSIGNMENT.finditer(script_text):
        line_end = script_text.find("\n", match.end())
        first_line = script_text[match.start() : line_end if line_end >= 0 else None]
        if ")" in first_line:
            assignment = first_line
        else:
            closing = ARRAY_END.search(script_text, match.end())
            assignment = script_text[
                match.start() : closing.start() if closing is not None else None
            ]
        if assignment.count(SECRET_RESOLUTION) > 1:
            violations.append(assignment.strip().splitlines()[0])
    return violations


def libraries_missing_from_context(pyproject_text: str, ignore_text: str) -> list[str]:
    """Return the local libraries the project installs but its context leaves out."""
    rules = ignore_text.splitlines()
    sources = tomllib.loads(pyproject_text).get("tool", {}).get("uv", {}).get(
        "sources", {}
    )
    libraries = [
        Path(source["path"]).name for source in sources.values() if "path" in source
    ]
    return [
        library
        for library in libraries
        if f"!libs/{library}/" not in rules or f"!libs/{library}/**" not in rules
    ]
