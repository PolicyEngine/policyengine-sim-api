"""The Modal images install pinned exports of uv.lock dependency groups.

These tests fail when the checked-in requirements files drift from
uv.lock — rerun scripts/export-modal-image-requirements.sh (make update
does it automatically). They also guard the regression from issue #602:
the exports must keep pinning logfire's undeclared importlib_metadata
runtime dependency.
"""

import subprocess
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
GROUPS = ("modal-simulation-image",)


def package_lines(text):
    return [
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


@pytest.mark.parametrize("group", GROUPS)
def test_checked_in_export_matches_lock(group):
    checked_in = PROJECT_ROOT / "requirements" / f"{group}.txt"
    assert checked_in.exists(), (
        f"{checked_in} is missing; run scripts/export-modal-image-requirements.sh"
    )
    exported = subprocess.run(
        [
            "uv",
            "export",
            "--only-group",
            group,
            "--frozen",
            "--no-hashes",
            "--no-annotate",
        ],
        cwd=PROJECT_ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert package_lines(checked_in.read_text()) == package_lines(exported), (
        f"requirements/{group}.txt is stale relative to uv.lock; "
        "run scripts/export-modal-image-requirements.sh"
    )


@pytest.mark.parametrize("group", GROUPS)
def test_export_is_fully_pinned(group):
    checked_in = PROJECT_ROOT / "requirements" / f"{group}.txt"
    for line in package_lines(checked_in.read_text()):
        requirement = line.split(";")[0].strip()
        assert "==" in requirement, f"unpinned requirement in {group}: {line}"


@pytest.mark.parametrize("group", GROUPS)
def test_export_pins_logfire_runtime_deps(group):
    checked_in = PROJECT_ROOT / "requirements" / f"{group}.txt"
    names = {
        line.split(";")[0].split("==")[0].strip()
        for line in package_lines(checked_in.read_text())
    }
    assert {"logfire", "importlib-metadata"} <= names
