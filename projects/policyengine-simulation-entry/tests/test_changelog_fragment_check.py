"""Tests for the changelog fragment check that every pull request runs.

Rule sim.process.changelog-fragment: a pull request adds at least one
non-empty file directly in changelog.d/, named ``<name>.<type>.md`` with a type
from towncrier.toml, and every file in changelog.d/ is such a fragment, because
those are the only files the release reads. Each test builds a repository with
a base commit and a pull request commit, and runs
.github/scripts/check-changelog-fragment.sh there, as the workflow does.
"""

from __future__ import annotations

import shutil
from typing import TYPE_CHECKING

import pytest
from changelog_release_support import (
    FRAGMENT_TYPES,
    GIT_ENVIRONMENT,
    commit_all,
    git,
    load_fragments_module,
    make_repository,
    write_files,
)
from workflow_script_support import REPOSITORY_ROOT, run_script

if TYPE_CHECKING:
    import subprocess
    from pathlib import Path

CHECK_SCRIPT = "check-changelog-fragment.sh"
PROOF_FIXTURES = (
    REPOSITORY_ROOT / "rules" / "fixtures" / "sim.process.changelog-fragment"
)
FRAGMENT_TEXT = "Required a changelog fragment on every pull request.\n"
NO_FRAGMENT_ADDED = "::error::This pull request adds no changelog fragment."


def _open_pull_request(
    root: Path, files: dict[str, str], removed: tuple[str, ...] = ()
) -> str:
    """Commit ``files`` and the removal of ``removed`` on a pull request branch."""
    git(root, "checkout", "--quiet", "-b", "pull-request")
    write_files(root, files)
    for relative_path in removed:
        (root / relative_path).unlink()
    return commit_all(root, "Pull request")


def _check(root: Path, base: str = "main") -> subprocess.CompletedProcess[str]:
    return run_script(CHECK_SCRIPT, env={**GIT_ENVIRONMENT, "BASE_SHA": base}, cwd=root)


def _errors(result: subprocess.CompletedProcess[str]) -> list[str]:
    return [line for line in result.stdout.splitlines() if line.startswith("::error")]


# --- What the check reads from towncrier.toml ------------------------------


def test_the_definition_reads_the_directory_and_types_from_towncrier_toml():
    module = load_fragments_module()

    definition = module.FragmentDefinition.load(REPOSITORY_ROOT)

    assert definition.directory == "changelog.d"
    assert definition.types == FRAGMENT_TYPES


# --- Pull requests the check accepts -----------------------------------------


@pytest.mark.parametrize(
    "fragment",
    [
        "changelog.d/982.added.md",
        "changelog.d/982.breaking.md",
        "changelog.d/982.changed.md",
        "changelog.d/982.fixed.md",
        "changelog.d/982.removed.md",
        "changelog.d/update-policyengine-6.11.1.changed.md",
        "changelog.d/towncrier_changelog.added.md",
    ],
)
def test_accepts_a_pull_request_that_adds_a_fragment(tmp_path, fragment):
    root = make_repository(tmp_path)
    _open_pull_request(root, {fragment: FRAGMENT_TEXT, "src/app.py": "pass\n"})

    result = _check(root)

    assert result.returncode == 0, result.stdout + result.stderr
    assert _errors(result) == []
    assert f"{fragment} (" in result.stdout


def test_accepts_several_fragments_of_different_types(tmp_path):
    root = make_repository(tmp_path)
    _open_pull_request(
        root,
        {
            "changelog.d/982.added.md": FRAGMENT_TEXT,
            "changelog.d/982.fixed.md": "Fixed the check.\n",
        },
    )

    result = _check(root)

    assert result.returncode == 0, result.stdout
    assert "adds 2 changelog fragments" in result.stdout


def test_counts_a_misplaced_file_renamed_into_place(tmp_path):
    """A rename is a removal and an addition, so moving a file into place counts."""
    root = make_repository(tmp_path, {"changelog.d/added/4083.md": FRAGMENT_TEXT})
    git(root, "checkout", "--quiet", "-b", "pull-request")
    git(root, "mv", "changelog.d/added/4083.md", "changelog.d/4083.added.md")
    commit_all(root, "Move the fragment into place")

    result = _check(root)

    assert result.returncode == 0, result.stdout


# --- Pull requests the check rejects ---------------------------------------


def test_rejects_the_subfolder_fragment_of_policyengine_api_4084(tmp_path):
    """policyengine-api #4084's check passed this file; its release read nothing."""
    root = make_repository(tmp_path)
    _open_pull_request(root, {"changelog.d/added/4083.md": FRAGMENT_TEXT})

    result = _check(root)

    assert result.returncode == 1
    errors = _errors(result)
    assert errors[0].startswith("::error file=changelog.d/added/4083.md::")
    assert "is in a folder inside changelog.d/" in errors[0]
    assert "Move it to changelog.d/4083.added.md" in errors[0]
    assert errors[1].startswith(NO_FRAGMENT_ADDED)


def test_rejects_a_pull_request_that_adds_no_fragment(tmp_path):
    root = make_repository(tmp_path)
    _open_pull_request(root, {"src/app.py": "pass\n"})

    result = _check(root)

    assert result.returncode == 1
    errors = _errors(result)
    assert len(errors) == 1
    assert errors[0].startswith(NO_FRAGMENT_ADDED)
    assert "changelog.d/<name>.<type>.md" in errors[0]
    assert "breaking, added, changed, fixed or removed" in errors[0]


def test_fragments_already_on_main_do_not_count(tmp_path):
    """policyengine-api's check passed any pull request while main had fragments."""
    root = make_repository(tmp_path, {"changelog.d/981.fixed.md": FRAGMENT_TEXT})
    _open_pull_request(root, {"src/app.py": "pass\n"})

    result = _check(root)

    assert result.returncode == 1
    assert _errors(result)[0].startswith(NO_FRAGMENT_ADDED)


def test_editing_a_fragment_already_on_main_does_not_count(tmp_path):
    root = make_repository(tmp_path, {"changelog.d/981.fixed.md": FRAGMENT_TEXT})
    _open_pull_request(root, {"changelog.d/981.fixed.md": "Fixed it better.\n"})

    result = _check(root)

    assert result.returncode == 1
    assert _errors(result)[0].startswith(NO_FRAGMENT_ADDED)


def test_a_fragment_name_outside_changelog_d_does_not_count(tmp_path):
    root = make_repository(tmp_path)
    _open_pull_request(root, {"docs/changelog.d/982.added.md": FRAGMENT_TEXT})

    result = _check(root)

    assert result.returncode == 1
    assert _errors(result)[0].startswith(NO_FRAGMENT_ADDED)


@pytest.mark.parametrize("fragment_type", ["feature", "Added", "bugfix", "doc"])
def test_rejects_an_unknown_fragment_type(tmp_path, fragment_type):
    fragment = f"changelog.d/982.{fragment_type}.md"
    root = make_repository(tmp_path)
    _open_pull_request(root, {fragment: FRAGMENT_TEXT})

    result = _check(root)

    assert result.returncode == 1
    errors = _errors(result)
    assert errors[0].startswith(f"::error file={fragment}::")
    assert f'has the type "{fragment_type}"' in errors[0]
    assert "breaking, added, changed, fixed or removed" in errors[0]
    assert errors[1].startswith(NO_FRAGMENT_ADDED)


@pytest.mark.parametrize(
    "text", ["", "\n", "  \n\t\n"], ids=["empty", "newline", "blank"]
)
def test_rejects_an_empty_fragment(tmp_path, text):
    root = make_repository(tmp_path)
    _open_pull_request(root, {"changelog.d/982.added.md": text})

    result = _check(root)

    assert result.returncode == 1
    errors = _errors(result)
    assert errors[0].startswith("::error file=changelog.d/982.added.md::")
    assert "is empty" in errors[0]


@pytest.mark.parametrize(
    "fragment",
    [
        "changelog.d/982.added",
        "changelog.d/982.added.txt",
        "changelog.d/982.added.1.md",
        "changelog.d/982.md",
        "changelog.d/.added.md",
        "changelog.d/982..added.md",
        "changelog.d/982 work.added.md",
        "changelog.d/README.md",
    ],
)
def test_rejects_other_names(tmp_path, fragment):
    root = make_repository(tmp_path)
    _open_pull_request(root, {fragment: FRAGMENT_TEXT})

    result = _check(root)

    assert result.returncode == 1
    errors = _errors(result)
    assert errors[0].startswith(f"::error file={fragment}::")
    assert errors[-1].startswith(NO_FRAGMENT_ADDED)


def test_rejects_an_invalid_file_next_to_a_valid_fragment(tmp_path):
    """The release reads every file in changelog.d/, so every one must be valid."""
    root = make_repository(tmp_path)
    _open_pull_request(
        root,
        {
            "changelog.d/982.added.md": FRAGMENT_TEXT,
            "changelog.d/982.feature.md": FRAGMENT_TEXT,
        },
    )

    result = _check(root)

    assert result.returncode == 1
    assert [error.split("::")[1] for error in _errors(result)] == [
        "error file=changelog.d/982.feature.md"
    ]


def test_rejects_an_invalid_file_already_on_main(tmp_path):
    """The next release would fail on it, so the merged result must not hold it."""
    root = make_repository(tmp_path, {"changelog.d/added/4083.md": FRAGMENT_TEXT})
    _open_pull_request(root, {"changelog.d/982.added.md": FRAGMENT_TEXT})

    result = _check(root)

    assert result.returncode == 1
    errors = _errors(result)
    assert len(errors) == 1
    assert errors[0].startswith("::error file=changelog.d/added/4083.md::")
    assert "already on the base branch" in errors[0]


# --- The merged result GitHub checks out --------------------------------------


def _merge_into_advanced_main(root: Path, pull_request_files: dict[str, str]) -> str:
    """Merge a pull request into main after another one merged a fragment first.

    Return the base commit: main's head before the merge, which the merge
    commit has as its first parent, like GitHub's pull request merge ref.
    """
    _open_pull_request(root, pull_request_files)
    git(root, "checkout", "--quiet", "main")
    write_files(root, {"changelog.d/981.fixed.md": "Fixed something else.\n"})
    base = commit_all(root, "Merge pull request #981")
    git(root, "merge", "--quiet", "--no-ff", "--message", "Merge", "pull-request")
    return base


def test_counts_the_fragment_the_pull_request_adds_to_the_merged_result(tmp_path):
    root = make_repository(tmp_path)
    base = _merge_into_advanced_main(root, {"changelog.d/982.added.md": FRAGMENT_TEXT})

    result = _check(root, base)

    assert result.returncode == 0, result.stdout
    assert "changelog.d/982.added.md (added)" in result.stdout
    assert "981" not in result.stdout


def test_a_fragment_another_pull_request_merged_first_does_not_count(tmp_path):
    root = make_repository(tmp_path)
    base = _merge_into_advanced_main(root, {"src/app.py": "pass\n"})

    result = _check(root, base)

    assert result.returncode == 1
    assert _errors(result)[0].startswith(NO_FRAGMENT_ADDED)


def test_requires_the_base_revision(tmp_path):
    root = make_repository(tmp_path)
    environment = dict(GIT_ENVIRONMENT)
    environment.pop("BASE_SHA", None)

    result = run_script(CHECK_SCRIPT, env=environment, cwd=root)

    assert result.returncode != 0
    assert "BASE_SHA is required" in result.stderr


def test_fails_when_the_base_revision_is_not_in_the_checkout(tmp_path):
    root = make_repository(tmp_path)
    _open_pull_request(root, {"changelog.d/982.added.md": FRAGMENT_TEXT})

    result = _check(root, "0123456789abcdef0123456789abcdef01234567")

    assert result.returncode != 0
    assert "Could not find the merge base" in result.stdout


# --- Proof --------------------------------------------------------------------


def test_rule_proof_changelog_fragment(tmp_path):
    """The check rejects policyengine-api #4084's fragment and accepts it moved.

    #4084 added changelog.d/added/4083.md. policyengine-api's check accepted
    it, and its release, which reads only <name>.<type>.md files directly in
    changelog.d/, found no fragment and failed.
    """
    results = {}
    for fixture in ("bad", "good"):
        root = make_repository(tmp_path / fixture)
        git(root, "checkout", "--quiet", "-b", "pull-request")
        shutil.copytree(PROOF_FIXTURES / fixture, root, dirs_exist_ok=True)
        commit_all(root, f"Pull request ({fixture} fixture)")
        results[fixture] = _check(root)

    assert results["bad"].returncode == 1
    assert _errors(results["bad"])[0].startswith(
        "::error file=changelog.d/added/4083.md::"
    )
    assert results["good"].returncode == 0, results["good"].stdout
    assert "changelog.d/4083.added.md (added)" in results["good"].stdout
