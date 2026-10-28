"""Build throwaway git repositories to run the changelog scripts in.

The changelog scripts under ``.github/scripts/`` read ``towncrier.toml``,
``VERSION`` and ``changelog.d/`` from the directory they run in, and ask git
what a pull request adds. Each test builds a repository in ``tmp_path`` with
this repository's ``towncrier.toml``, commits the files it needs, and runs the
real scripts there. Git runs without the user's or the system's configuration,
so local settings such as commit signing can't change a result.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
from typing import TYPE_CHECKING

from workflow_script_support import REPOSITORY_ROOT, SCRIPTS_DIR

if TYPE_CHECKING:
    from pathlib import Path
    from types import ModuleType

FRAGMENTS_MODULE = SCRIPTS_DIR / "changelog_fragments.py"
TOWNCRIER_CONFIG = REPOSITORY_ROOT / "towncrier.toml"
FRAGMENT_TYPES = ("breaking", "added", "changed", "fixed", "removed")

GIT_ENVIRONMENT = {
    **os.environ,
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_NAME": "Changelog Test",
    "GIT_AUTHOR_EMAIL": "changelog-test@example.com",
    "GIT_COMMITTER_NAME": "Changelog Test",
    "GIT_COMMITTER_EMAIL": "changelog-test@example.com",
}


def git(root: Path, *arguments: str) -> str:
    """Run git in ``root`` and return what it printed, without the final newline."""
    completed = subprocess.run(
        ["git", *arguments],
        cwd=root,
        env=GIT_ENVIRONMENT,
        capture_output=True,
        text=True,
        check=True,
    )
    return completed.stdout.rstrip("\n")


def write_files(root: Path, files: dict[str, str]) -> None:
    """Write each ``path: text`` pair under ``root``, creating folders as needed."""
    for relative_path, text in files.items():
        path = root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def commit_all(root: Path, message: str) -> str:
    """Commit every change in ``root`` and return the new commit's hash."""
    git(root, "add", "--all")
    git(root, "commit", "--quiet", "--allow-empty", "--message", message)
    return git(root, "rev-parse", "HEAD")


def make_repository(tmp_path: Path, files: dict[str, str] | None = None) -> Path:
    """Return a repository on ``main`` with this repository's towncrier.toml.

    Its first commit also holds ``changelog.d/.gitkeep``, a ``README.md`` and
    ``files``.
    """
    root = tmp_path / "repository"
    root.mkdir(parents=True)
    git(root, "init", "--quiet", "--initial-branch", "main")
    shutil.copyfile(TOWNCRIER_CONFIG, root / "towncrier.toml")
    write_files(
        root,
        {"changelog.d/.gitkeep": "", "README.md": "Base.\n", **(files or {})},
    )
    commit_all(root, "Base")
    return root


def load_fragments_module() -> ModuleType:
    """Load ``.github/scripts/changelog_fragments.py`` as a module in this process."""
    spec = importlib.util.spec_from_file_location(
        "changelog_fragments", FRAGMENTS_MODULE
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
