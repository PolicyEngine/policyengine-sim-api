"""Tests for CHANGELOG.md and the history it took over from changelog_entry.yaml.

Before changelog fragments, every pull request added a line to one shared
changelog_entry.yaml, which nothing read. Its 63 entries moved, word for word
and in order, into CHANGELOG.md as the 0.1.0 section. Each of those changes
deployed when its pull request merged, so the section describes main as it
stood when fragments started, under the version every package already
declared. Towncrier writes each later release above it, after the start
marker towncrier.toml names, and the release commit raises VERSION to match.
"""

from __future__ import annotations

import hashlib
import re

import tomllib
from changelog_release_support import TOWNCRIER_CONFIG
from workflow_script_support import REPOSITORY_ROOT

CHANGELOG = REPOSITORY_ROOT / "CHANGELOG.md"
SECTION_HEADING = re.compile(
    r"^## \[(?P<version>[0-9]+\.[0-9]+\.[0-9]+)\] - "
    r"(?P<date>[0-9]{4}-[0-9]{2}-[0-9]{2})$",
    re.MULTILINE,
)
TYPE_HEADING = re.compile(r"^### (?P<name>.+)$", re.MULTILINE)
# SHA-256 of the 63 entries of changelog_entry.yaml, joined by newlines in
# file order (added, changed, fixed), as they were on main before the move.
HISTORY_SHA256 = "2a2b3336fd21a40a71bd83244e450c506a51e7a03193d0f88fa819833e3a7178"
# Two entries YAML read as mappings, because of an unquoted ": ". Copying the
# file's text, not what YAML parsed, keeps them whole.
ENTRIES_YAML_READ_AS_MAPPINGS = (
    "Added the rule sim.quality.python-file-length, checked by a new file-length "
    "quality guard with proof fixtures that its test writes to a temporary "
    "directory: a tracked Python file has at most 800 lines",
    "Enforced the file-length rule sim.quality.python-file-length, now that every "
    "tracked Python file is within its limit: its empty baseline and its downgrade "
    "section are gone",
)


def _towncrier() -> dict:
    return tomllib.loads(TOWNCRIER_CONFIG.read_text(encoding="utf-8"))["tool"][
        "towncrier"
    ]


def _sections() -> list[tuple[str, str, str]]:
    """Return each release section as (version, date, body), newest first."""
    text = CHANGELOG.read_text(encoding="utf-8")
    headings = list(SECTION_HEADING.finditer(text))
    return [
        (
            heading.group("version"),
            heading.group("date"),
            text[
                heading.end() : (
                    headings[index + 1].start()
                    if index + 1 < len(headings)
                    else len(text)
                )
            ],
        )
        for index, heading in enumerate(headings)
    ]


def _entries_by_type(body: str) -> dict[str, list[str]]:
    """Return the bulleted entries under each ``### Type`` heading of ``body``."""
    headings = list(TYPE_HEADING.finditer(body))
    entries = {}
    for index, heading in enumerate(headings):
        end = headings[index + 1].start() if index + 1 < len(headings) else len(body)
        entries[heading.group("name")] = [
            line.removeprefix("- ")
            for line in body[heading.end() : end].splitlines()
            if line.startswith("- ")
        ]
    return entries


def test_the_shared_changelog_entry_file_is_gone():
    assert not (REPOSITORY_ROOT / "changelog_entry.yaml").exists()


def test_towncrier_writes_releases_after_its_start_marker():
    text = CHANGELOG.read_text(encoding="utf-8")
    towncrier = _towncrier()
    start_marker = towncrier["start_string"]

    assert towncrier["filename"] == "CHANGELOG.md"
    assert text.count(start_marker) == 1
    assert text.index(start_marker) < SECTION_HEADING.search(text).start()
    assert towncrier["title_format"] == "## [{version}] - {project_date}"


def test_the_first_release_holds_the_history_from_changelog_entry_yaml():
    version, date, body = _sections()[-1]
    entries = _entries_by_type(body)

    assert (version, date) == ("0.1.0", "2026-10-28")
    assert list(entries) == ["Added", "Changed", "Fixed"]
    assert [len(listed) for listed in entries.values()] == [17, 28, 18]
    history = [entry for listed in entries.values() for entry in listed]
    assert hashlib.sha256("\n".join(history).encode()).hexdigest() == HISTORY_SHA256
    assert entries["Added"][0].startswith(
        "Recorded in the routing state when each policyengine.py route was first "
        "published"
    )
    assert entries["Fixed"][-1].startswith(
        "Gave Stage 12 baseline and reform simulations one shared wait window"
    )
    for prefix in ENTRIES_YAML_READ_AS_MAPPINGS:
        assert sum(entry.startswith(prefix) for entry in history) == 1, prefix


def test_version_names_the_newest_release():
    """The release commit raises VERSION and writes its section together."""
    newest_version = _sections()[0][0]

    assert (REPOSITORY_ROOT / "VERSION").read_text(encoding="utf-8") == (
        f"{newest_version}\n"
    )


def test_releases_are_listed_newest_first():
    versions = [
        tuple(int(part) for part in version.split("."))
        for version, _date, _body in _sections()
    ]

    assert versions == sorted(versions, reverse=True)
    assert len(set(versions)) == len(versions)
