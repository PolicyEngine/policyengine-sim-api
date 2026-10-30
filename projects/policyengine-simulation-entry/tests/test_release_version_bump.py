"""Tests for the version bump and the changelog build that release a merge to main.

After a merge to main, .github/scripts/build-changelog.sh raises VERSION by
the largest bump the pending fragments' types call for (``changelog_fragments.py
bump``), then Towncrier, from the locked environment in .github/towncrier,
writes their CHANGELOG.md section. The bump reads fragments by the same
definition as the pull request check, so it refuses to run while changelog.d/
holds anything else. Tests of the bump run it in a throwaway repository;
tests of the build script put a fake ``uv`` first on PATH, so no test runs
Towncrier.
"""

from __future__ import annotations

import subprocess
import sys
from typing import TYPE_CHECKING

import pytest
import tomllib
from changelog_release_support import (
    FRAGMENT_TYPES,
    FRAGMENTS_MODULE,
    TOWNCRIER_CONFIG,
    load_fragments_module,
    make_repository,
)
from workflow_script_support import REPOSITORY_ROOT, FakeCommands, run_script

if TYPE_CHECKING:
    from pathlib import Path

BUILD_SCRIPT = "build-changelog.sh"
TOWNCRIER_PROJECT = REPOSITORY_ROOT / ".github" / "towncrier"
FRAGMENT_TEXT = "Changed something people notice.\n"
# Records each call's VERSION, so a test can tell the bump ran before Towncrier.
FAKE_UV = 'cat VERSION >> "$FAKE_UV_SAW_VERSION"\n'


def _repository(
    tmp_path: Path, fragments: dict[str, str], version: str = "0.1.0"
) -> Path:
    return make_repository(tmp_path, {"VERSION": f"{version}\n", **fragments})


def _run_fragments_command(
    root: Path, command: str
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(FRAGMENTS_MODULE), command],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )


def _version(root: Path) -> str:
    return (root / "VERSION").read_text(encoding="utf-8")


# --- The bump -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("version", "fragment_types", "expected"),
    [
        ("0.1.0", ["fixed"], "0.1.1"),
        ("0.1.0", ["changed"], "0.1.1"),
        ("0.1.0", ["added"], "0.2.0"),
        ("0.1.0", ["removed"], "0.2.0"),
        ("0.1.0", ["breaking"], "1.0.0"),
        ("0.1.0", ["fixed", "added", "changed"], "0.2.0"),
        ("0.1.0", ["added", "breaking", "fixed"], "1.0.0"),
        ("1.4.2", ["added"], "1.5.0"),
        ("1.4.2", ["breaking"], "2.0.0"),
        ("1.4.9", ["fixed"], "1.4.10"),
    ],
)
def test_the_bump_raises_the_largest_part_the_fragments_call_for(
    tmp_path, version, fragment_types, expected
):
    fragments = {
        f"changelog.d/98{index}.{fragment_type}.md": FRAGMENT_TEXT
        for index, fragment_type in enumerate(fragment_types)
    }
    root = _repository(tmp_path, fragments, version)

    result = _run_fragments_command(root, "bump")

    assert result.returncode == 0, result.stdout + result.stderr
    assert _version(root) == f"{expected}\n"
    assert f"VERSION: {version} -> {expected}" in result.stdout
    # Towncrier removes the fragments it builds; the bump leaves them.
    assert all((root / path).exists() for path in fragments)


def test_next_version_prints_the_bump_and_changes_nothing(tmp_path):
    root = _repository(tmp_path, {"changelog.d/982.added.md": FRAGMENT_TEXT})

    result = _run_fragments_command(root, "next-version")

    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout == "0.2.0\n"
    assert _version(root) == "0.1.0\n"


def test_the_bump_refuses_to_release_without_fragments(tmp_path):
    root = _repository(tmp_path, {})

    result = _run_fragments_command(root, "bump")

    assert result.returncode == 1
    assert result.stdout.startswith("::error::changelog.d/ holds no fragments")
    assert _version(root) == "0.1.0\n"


def test_the_bump_refuses_while_changelog_d_holds_a_file_it_cannot_read(tmp_path):
    """policyengine-api #4084's fragment would have stopped the release, saying why."""
    root = _repository(
        tmp_path,
        {
            "changelog.d/982.added.md": FRAGMENT_TEXT,
            "changelog.d/added/4083.md": FRAGMENT_TEXT,
        },
    )

    result = _run_fragments_command(root, "bump")

    assert result.returncode == 1
    assert "::error file=changelog.d/added/4083.md::" in result.stdout
    assert "Move it to changelog.d/4083.added.md" in result.stdout
    assert _version(root) == "0.1.0\n"


@pytest.mark.parametrize("version", ["0.1", "v0.1.0", "01.1.0", "0.1.0-rc1", ""])
def test_the_bump_refuses_a_malformed_version(tmp_path, version):
    root = _repository(tmp_path, {"changelog.d/982.fixed.md": FRAGMENT_TEXT}, version)

    result = _run_fragments_command(root, "bump")

    assert result.returncode == 1
    assert "VERSION must hold one version" in result.stdout
    assert _version(root) == f"{version}\n"


def test_the_bump_refuses_without_a_version_file(tmp_path):
    root = make_repository(tmp_path, {"changelog.d/982.fixed.md": FRAGMENT_TEXT})

    result = _run_fragments_command(root, "bump")

    assert result.returncode == 1
    assert "VERSION must hold one version" in result.stdout


def test_every_fragment_type_has_a_bump():
    module = load_fragments_module()

    assert tuple(module.BUMP_BY_TYPE) == FRAGMENT_TYPES
    assert module.BUMP_BY_TYPE == {
        "breaking": "major",
        "added": "minor",
        "changed": "patch",
        "fixed": "patch",
        "removed": "minor",
    }


def test_a_type_without_a_bump_stops_the_check_and_the_bump(tmp_path):
    """Both read towncrier.toml through one definition, so neither can drift."""
    root = _repository(tmp_path, {"changelog.d/982.security.md": FRAGMENT_TEXT})
    config = (root / "towncrier.toml").read_text(encoding="utf-8")
    (root / "towncrier.toml").write_text(
        config
        + '\n[[tool.towncrier.type]]\ndirectory = "security"\nname = "Security"\n'
        "showcontent = true\n",
        encoding="utf-8",
    )
    module = load_fragments_module()

    with pytest.raises(module.ConfigurationError, match='"security"'):
        module.FragmentDefinition.load(root)
    result = _run_fragments_command(root, "bump")
    assert result.returncode == 1
    assert '"security"' in result.stdout


# --- build-changelog.sh -------------------------------------------------------


@pytest.fixture
def fakes(tmp_path: Path) -> FakeCommands:
    return FakeCommands.create(tmp_path)


def _build(fakes: FakeCommands, root: Path, *arguments: str):
    fakes.add("uv", FAKE_UV)
    seen_versions = root.parent / "uv-saw-version"
    result = run_script(
        BUILD_SCRIPT,
        *arguments,
        env=fakes.env(FAKE_UV_SAW_VERSION=str(seen_versions)),
        cwd=root,
    )
    seen = seen_versions.read_text(encoding="utf-8") if seen_versions.exists() else ""
    return result, seen


def test_the_build_bumps_the_version_then_runs_towncrier_from_the_lock(fakes, tmp_path):
    root = _repository(tmp_path, {"changelog.d/982.added.md": FRAGMENT_TEXT})

    result, version_towncrier_saw = _build(fakes, root)

    assert result.returncode == 0, result.stdout + result.stderr
    assert _version(root) == "0.2.0\n"
    assert version_towncrier_saw == "0.2.0\n"
    assert fakes.calls() == [
        [
            "uv",
            "run",
            "--frozen",
            "--project",
            ".github/towncrier",
            "towncrier",
            "build",
            "--yes",
            "--version",
            "0.2.0",
        ]
    ]


def test_the_draft_shows_the_next_release_and_changes_nothing(fakes, tmp_path):
    root = _repository(tmp_path, {"changelog.d/982.fixed.md": FRAGMENT_TEXT})

    result, _ = _build(fakes, root, "--draft")

    assert result.returncode == 0, result.stdout + result.stderr
    assert _version(root) == "0.1.0\n"
    assert fakes.calls() == [
        [
            "uv",
            "run",
            "--frozen",
            "--project",
            ".github/towncrier",
            "towncrier",
            "build",
            "--draft",
            "--version",
            "0.1.1",
        ]
    ]


def test_the_build_runs_no_towncrier_when_the_bump_refuses(fakes, tmp_path):
    root = _repository(tmp_path, {"changelog.d/added/4083.md": FRAGMENT_TEXT})

    result, _ = _build(fakes, root)

    assert result.returncode == 1
    assert "changelog.d/added/4083.md" in result.stdout
    assert fakes.calls() == []
    assert _version(root) == "0.1.0\n"


def test_the_build_rejects_an_unknown_argument(fakes, tmp_path):
    root = _repository(tmp_path, {"changelog.d/982.fixed.md": FRAGMENT_TEXT})

    result, _ = _build(fakes, root, "--publish")

    assert result.returncode == 2
    assert "Usage: build-changelog.sh [--draft]" in result.stderr
    assert fakes.calls() == []


# --- Where Towncrier and the version come from ----------------------------------


def test_towncrier_runs_from_a_locked_project_of_its_own():
    project = tomllib.loads(
        (TOWNCRIER_PROJECT / "pyproject.toml").read_text(encoding="utf-8")
    )
    lock = tomllib.loads((TOWNCRIER_PROJECT / "uv.lock").read_text(encoding="utf-8"))

    assert project["tool"]["uv"]["package"] is False
    assert [
        requirement.split(">", 1)[0].split("=", 1)[0].strip()
        for requirement in project["project"]["dependencies"]
    ] == ["towncrier"]
    assert "towncrier" in {package["name"] for package in lock["package"]}


def test_the_repository_has_one_release_version():
    module = load_fragments_module()
    version = (REPOSITORY_ROOT / "VERSION").read_text(encoding="utf-8")

    assert module.VERSION_PATTERN.fullmatch(version.removesuffix("\n"))
    assert (
        "version"
        not in tomllib.loads(TOWNCRIER_CONFIG.read_text(encoding="utf-8"))["tool"][
            "towncrier"
        ]
    )


def test_make_changelog_runs_the_release_build():
    makefile = (REPOSITORY_ROOT / "Makefile").read_text(encoding="utf-8")

    assert "\nchangelog:\n\t.github/scripts/build-changelog.sh\n" in makefile
    assert "\nchangelog-draft:\n\t.github/scripts/build-changelog.sh --draft\n" in (
        makefile
    )
