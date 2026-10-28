"""Changelog fragments: what one is, and the check every pull request runs.

Rule sim.process.changelog-fragment. Every pull request adds at least one
changelog fragment, and after a merge to main Towncrier builds the pending
fragments into CHANGELOG.md. Everything here reads fragments through
``FragmentDefinition``, so what the check accepts is exactly what the release
reads.

A fragment is a file directly in changelog.d/ (``directory`` in towncrier.toml)
named ``<name>.<type>.md``, where:

- ``<name>`` is groups of letters and digits joined by single dots, hyphens or
  underscores, such as ``982`` or ``update-policyengine-6.11.1``;
- ``<type>`` is one of the fragment types towncrier.toml lists;
- its text, read as UTF-8, is not empty or only whitespace.

``.gitkeep``, which keeps changelog.d/ in git once a release has removed every
fragment, is the only other file allowed there. Towncrier skips a file in a
folder or with an unknown type, and writes an empty heading for an empty
fragment, so the release would lose anything else. policyengine-api #4084
added changelog.d/added/4083.md: its check accepted the file, and its release
found no fragment.

Command, run from the repository root:

  check-pull-request  read the paths a pull request adds, NUL-separated on
                      stdin, as check-changelog-fragment.sh passes them, and
                      exit 1 unless one of them is a fragment and every file
                      in changelog.d/ is one
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

import tomllib

RULE_ID = "sim.process.changelog-fragment"
TOWNCRIER_CONFIG = "towncrier.toml"
# Keeps changelog.d/ in git once a release has removed every fragment.
KEEP_FILE = ".gitkeep"
FRAGMENT_FILE_NAME = re.compile(
    r"(?P<name>[A-Za-z0-9]+(?:[._-][A-Za-z0-9]+)*)\.(?P<type>[^.]+)\.md"
)
FRAGMENT_NAME = re.compile(r"[A-Za-z0-9]+(?:[._-][A-Za-z0-9]+)*")
DOCUMENTATION = "docs/engineering/skills/github-prs.md#changelog-fragments"


class ConfigurationError(Exception):
    """towncrier.toml does not say where fragments live or which types they have."""


@dataclass(frozen=True)
class Fragment:
    """A changelog fragment the release reads: its path, name and type."""

    path: str
    name: str
    type: str


@dataclass(frozen=True)
class Scan:
    """What changelog.d/ holds: its fragments, and why each other file isn't one.

    ``problems`` maps a path from the repository root to its reason.
    """

    fragments: tuple[Fragment, ...]
    problems: dict[str, str]


def _listed(names: tuple[str, ...]) -> str:
    """Return ``names`` as "a, b or c"."""
    if len(names) == 1:
        return names[0]
    return f"{', '.join(names[:-1])} or {names[-1]}"


@dataclass(frozen=True)
class FragmentDefinition:
    """Where fragments live and which types they may have, read from towncrier.toml."""

    directory: str
    types: tuple[str, ...]

    @classmethod
    def load(cls, root: Path) -> FragmentDefinition:
        """Read the fragment directory and types from ``root``'s towncrier.toml."""
        config_path = root / TOWNCRIER_CONFIG
        try:
            config = tomllib.loads(config_path.read_text(encoding="utf-8"))
            towncrier = config["tool"]["towncrier"]
            directory = towncrier["directory"]
            types = tuple(entry["directory"] for entry in towncrier["type"])
        except (OSError, tomllib.TOMLDecodeError, KeyError, TypeError) as error:
            msg = (
                f"Could not read the fragment directory and types from "
                f"{config_path}: {error!r}"
            )
            raise ConfigurationError(msg) from error
        if not isinstance(directory, str) or not FRAGMENT_NAME.fullmatch(directory):
            msg = (
                f"{TOWNCRIER_CONFIG} names a fragment directory that is not one "
                f"folder: {directory!r}"
            )
            raise ConfigurationError(msg)
        if not types or len(set(types)) != len(types):
            msg = f"{TOWNCRIER_CONFIG} must list each fragment type once: {types!r}"
            raise ConfigurationError(msg)
        return cls(directory=directory, types=types)

    def how_to_add(self) -> str:
        """Return the sentence that says how to add a fragment."""
        return (
            f"Add a file {self.directory}/<name>.<type>.md, directly in "
            f"{self.directory}/, that says in a sentence or two what changed for "
            f"people who use or run the simulation API. <type> is "
            f"{_listed(self.types)}, and <name> is the issue number or another "
            f"label made of letters, digits, dots, hyphens and underscores. See "
            f"{DOCUMENTATION} (rule {RULE_ID})."
        )

    def _suggested_path(self, path: PurePosixPath) -> str | None:
        """Return where a fragment filed in a type's folder belongs, if it is clear.

        ``changelog.d/added/4083.md`` belongs at ``changelog.d/4083.added.md``.
        """
        folders = path.relative_to(self.directory).parts[:-1]
        name = path.name.removesuffix(".md")
        if (
            len(folders) == 1
            and folders[0] in self.types
            and FRAGMENT_NAME.fullmatch(name)
        ):
            return f"{self.directory}/{name}.{folders[0]}.md"
        return None

    def _nested_problem(self, path: PurePosixPath) -> str:
        """Return why a file in a folder inside the fragment directory isn't read."""
        problem = (
            f"{path} is in a folder inside {self.directory}/. Towncrier and the "
            f"version bump read only files directly in {self.directory}/, so the "
            f"release would leave this one out."
        )
        suggested = self._suggested_path(path)
        if suggested:
            return f"{problem} Move it to {suggested}."
        return f"{problem} Move it to {self.directory}/<name>.<type>.md."

    def _name_problem(self, path: str) -> str | None:
        """Return why ``path`` is not where and how a fragment is named, or None."""
        location = PurePosixPath(path)
        if location.parent != PurePosixPath(self.directory):
            return self._nested_problem(location)
        match = FRAGMENT_FILE_NAME.fullmatch(location.name)
        if not match:
            return (
                f"{path} is not named <name>.<type>.md, so the release can't read "
                f"it. Rename it, or move it out of {self.directory}/."
            )
        fragment_type = match.group("type")
        if fragment_type not in self.types:
            return (
                f'{path} has the type "{fragment_type}", which is not one of the '
                f"types towncrier.toml lists: {_listed(self.types)}. Rename it."
            )
        return None

    @staticmethod
    def _text_problem(path: str, file: Path) -> str | None:
        """Return why ``file`` holds no fragment text, or None."""
        if file.is_symlink() or not file.is_file():
            return f"{path} is not a regular file; a fragment is a text file."
        try:
            text = file.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return f"{path} is not UTF-8 text."
        if not text.strip():
            return (
                f"{path} is empty. Write what changed in it: Towncrier would "
                f"publish an empty entry."
            )
        return None

    def problem_with(self, path: str, file: Path) -> str | None:
        """Return why ``file``, at ``path`` from the repository root, is not a fragment.

        Return None when it is one.
        """
        return self._name_problem(path) or self._text_problem(path, file)

    def scan(self, root: Path) -> Scan:
        """Return the fragments in ``root``'s fragment directory, and every other file.

        Every file in a folder inside the directory is a problem. An empty
        folder is not: git can't commit one, so no checkout the release builds
        from has it.
        """
        directory = root / self.directory
        fragments: list[Fragment] = []
        problems: dict[str, str] = {}
        if not directory.is_dir():
            return Scan(fragments=(), problems=problems)
        for entry in sorted(directory.iterdir()):
            path = f"{self.directory}/{entry.name}"
            if entry.name == KEEP_FILE and entry.is_file():
                continue
            if entry.is_dir() and not entry.is_symlink():
                nested_files = sorted(
                    nested for nested in entry.rglob("*") if not nested.is_dir()
                )
                for nested_file in nested_files:
                    nested_path = nested_file.relative_to(root).as_posix()
                    problems[nested_path] = self._nested_problem(
                        PurePosixPath(nested_path)
                    )
                continue
            problem = self.problem_with(path, entry)
            if problem is None:
                match = FRAGMENT_FILE_NAME.fullmatch(entry.name)
                fragments.append(
                    Fragment(
                        path=path, name=match.group("name"), type=match.group("type")
                    )
                )
            else:
                problems[path] = problem
        return Scan(fragments=tuple(fragments), problems=problems)


def _annotation_text(message: str) -> str:
    """Escape ``message`` so the runner reads it as one annotation message."""
    return message.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def check_pull_request(root: Path, added_paths: set[str]) -> int:
    """Check the merged result in ``root`` of a pull request that adds ``added_paths``.

    Print one error annotation for each file in the fragment directory that is
    not a fragment, and one if the pull request adds no fragment; return the
    exit status.
    """
    definition = FragmentDefinition.load(root)
    scan = definition.scan(root)
    for path, problem in scan.problems.items():
        message = problem
        if path not in added_paths:
            message += (
                " It is already on the base branch, and the next release can't "
                "be built until it is fixed."
            )
        print(f"::error file={path}::{_annotation_text(message)}")

    added = [fragment for fragment in scan.fragments if fragment.path in added_paths]
    if not added:
        message = (
            "This pull request adds no changelog fragment. Fragments already on "
            f"the base branch belong to the pull requests that added them. "
            f"{definition.how_to_add()}"
        )
        print(f"::error::{_annotation_text(message)}")
    if scan.problems or not added:
        return 1

    listed = ", ".join(f"{fragment.path} ({fragment.type})" for fragment in added)
    count = (
        "1 changelog fragment"
        if len(added) == 1
        else f"{len(added)} changelog fragments"
    )
    print(f"This pull request adds {count}: {listed}.")
    return 0


def _paths_from_stdin() -> set[str]:
    """Return the NUL-separated paths on stdin, as ``git diff -z`` prints them."""
    data = sys.stdin.buffer.read()
    return {
        path.decode("utf-8", errors="surrogateescape")
        for path in data.split(b"\0")
        if path
    }


def main(arguments: list[str] | None = None) -> int:
    """Run the command named in ``arguments``; return the exit status."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser(
        "check-pull-request",
        help="check the merged result of a pull request; its added paths on stdin",
    )
    options = parser.parse_args(arguments)
    root = Path.cwd()
    try:
        if options.command == "check-pull-request":
            return check_pull_request(root, _paths_from_stdin())
    except ConfigurationError as error:
        print(f"::error::{_annotation_text(str(error))}")
        return 1
    parser.error(f"unknown command {options.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
