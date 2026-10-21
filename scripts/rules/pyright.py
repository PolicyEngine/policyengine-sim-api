"""Run Pyright on a package in its own locked environment, and hold it to its rule.

``python -m scripts.rules pyright <package>`` finds the Pyright rule whose
enforcer lists the package, then runs the pinned Pyright version in the package
directory against the interpreter of the package's locked environment
(``.venv``, created by ``uv sync --frozen --all-extras``, which ``--sync`` runs
first). The package's own ``[tool.pyright]`` settings decide what is checked.

- A package under an enforced rule may have no errors.
- A package under a ratcheted rule may have exactly the errors its baseline
  lists. A new error fails, and so does an entry for an error that no longer
  occurs, so each fix removes its entry and the baseline only shrinks.

A baseline entry is ``<path> :: <Pyright rule> :: <first line of the message>``,
one line per error, sorted and without a line number, so edits elsewhere in a
file don't change it, and the rule's baseline count is the number of errors it
tolerates.

Pyright's messages and checks change between releases, so every package is
checked with one pinned version, PYRIGHT_VERSION, rather than the version each
package's lock file happens to hold.
"""

from __future__ import annotations

import json
import os
import subprocess
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from scripts.rules.load import load_registry
from scripts.rules.model import Kind, Registry, Rule, Status, Violation

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

PYRIGHT_VERSION = "1.1.414"
REPO_ROOT = Path(__file__).resolve().parents[2]
# Where uv packages live: each directory here with a pyproject.toml is one.
PACKAGE_GLOBS = ("libs/*/pyproject.toml", "projects/*/pyproject.toml")
COVERAGE_GUARD = "pyright-coverage"
ENTRY_SEPARATOR = " :: "
# The name a baseline entry gives an error Pyright reports without a rule.
UNNAMED_RULE = "error"
# Pyright exits with 0 when it finds no errors and 1 when it finds some.
COMPLETED_EXIT_CODES = (0, 1)
LOCKED_INTERPRETER = Path(".venv/bin/python")
SYNC_COMMAND = ("uv", "sync", "--frozen", "--all-extras")
# Read by the pyright PyPI package: one would run another version, the other
# silences its advice to upgrade.
FORCE_VERSION_VARIABLE = "PYRIGHT_PYTHON_FORCE_VERSION"
IGNORE_WARNINGS_VARIABLE = "PYRIGHT_PYTHON_IGNORE_WARNINGS"
# How much of Pyright's output a run error quotes.
QUOTED_OUTPUT_CHARACTERS = 2000
ENFORCED_FIX = (
    "  Fix: fix each error. Suppress one only as a last resort, "
    "with # pyright: ignore[<rule>] and the reason."
)
NEW_ERROR_FIX = "  Fix: fix each new error; the baseline only shrinks."


class PyrightRunError(Exception):
    """Pyright could not check a package; the message says why and what to do."""


@dataclass(frozen=True, order=True)
class PyrightError:
    """One error from Pyright's report, at a repository-relative path."""

    path: str
    line: int
    column: int
    rule: str
    message: str

    @property
    def summary(self) -> str:
        """Return the first line of the message, which names what is wrong."""
        lines = self.message.splitlines()
        return lines[0].strip() if lines else ""

    @property
    def baseline_entry(self) -> str:
        """Return the baseline entry that tolerates this error."""
        return ENTRY_SEPARATOR.join((self.path, self.rule, self.summary))

    def describe(self, label: str) -> list[str]:
        """Return the error as indented output lines, with its full message."""
        details = [line.strip() for line in self.message.splitlines()[1:]]
        return [
            f"  {label}: {self.path}:{self.line}:{self.column} - "
            f"{self.summary} ({self.rule})",
            *(f"      {detail}" for detail in details if detail),
        ]


@dataclass(frozen=True)
class Comparison:
    """The errors a baseline doesn't list, and the entries no error matches."""

    new_errors: tuple[PyrightError, ...]
    stale_entries: tuple[str, ...]

    @property
    def passed(self) -> bool:
        """Whether the errors are exactly the ones the baseline lists."""
        return not self.new_errors and not self.stale_entries


def _repository_path(file: str, root: Path) -> str:
    """Return ``file`` relative to ``root`` when it lies inside it."""
    path = Path(file)
    for base in (root, root.resolve()):
        if path.is_relative_to(base):
            return path.relative_to(base).as_posix()
    return path.as_posix()


def errors_in_report(report: Mapping[str, Any], root: Path) -> list[PyrightError]:
    """Return the errors in a ``pyright --outputjson`` report, sorted by position."""
    errors = []
    for diagnostic in report.get("generalDiagnostics", []):
        if diagnostic.get("severity") != "error":
            continue
        start = diagnostic.get("range", {}).get("start", {})
        errors.append(
            PyrightError(
                path=_repository_path(diagnostic.get("file", ""), root),
                line=start.get("line", 0) + 1,
                column=start.get("character", 0) + 1,
                rule=diagnostic.get("rule", UNNAMED_RULE),
                message=diagnostic.get("message", ""),
            )
        )
    return sorted(errors)


def read_baseline(path: Path) -> list[str]:
    """Return a baseline's entries, skipping blank and ``#`` comment lines."""
    return [
        line.strip()
        for line in path.read_text().splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def compare(errors: Sequence[PyrightError], baseline: Sequence[str]) -> Comparison:
    """Match each error with a baseline entry; each entry tolerates one error."""
    unmatched_entries = Counter(baseline)
    new_errors = []
    for error in errors:
        if unmatched_entries[error.baseline_entry] > 0:
            unmatched_entries[error.baseline_entry] -= 1
        else:
            new_errors.append(error)
    return Comparison(
        new_errors=tuple(new_errors),
        stale_entries=tuple(sorted(unmatched_entries.elements())),
    )


def _quoted(output: str) -> str:
    """Return the end of ``output`` on one line, for an error message."""
    return " ".join(output.strip()[-QUOTED_OUTPUT_CHARACTERS:].split())


def sync_environment(package_dir: Path) -> None:
    """Create or update the package's locked environment, with every extra.

    Raise PyrightRunError when ``uv sync`` fails.
    """
    completed = subprocess.run(
        [*SYNC_COMMAND],
        cwd=package_dir.resolve(),
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        msg = (
            f"`{' '.join(SYNC_COMMAND)}` exited with status {completed.returncode}: "
            f"{_quoted(completed.stderr or completed.stdout)}"
        )
        raise PyrightRunError(msg)


def run_pyright(package_dir: Path) -> dict[str, Any]:
    """Run the pinned Pyright in ``package_dir`` and return its JSON report.

    Raise PyrightRunError when the package has no locked environment, or when
    Pyright fails, prints no report, reports another version or checks nothing.
    """
    package_dir = package_dir.resolve()
    interpreter = package_dir / LOCKED_INTERPRETER
    if not interpreter.exists():
        msg = (
            f"there is no locked environment: {interpreter} does not exist; "
            f"run `uv sync --frozen --all-extras` in {package_dir} first"
        )
        raise PyrightRunError(msg)
    environment = {
        name: value
        for name, value in os.environ.items()
        if name != FORCE_VERSION_VARIABLE
    }
    environment[IGNORE_WARNINGS_VARIABLE] = "1"
    completed = subprocess.run(
        [
            "uv",
            "tool",
            "run",
            "--from",
            f"pyright=={PYRIGHT_VERSION}",
            "pyright",
            "--outputjson",
            "--pythonpath",
            str(interpreter),
        ],
        cwd=package_dir,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode not in COMPLETED_EXIT_CODES:
        msg = (
            f"Pyright exited with status {completed.returncode}: "
            f"{_quoted(completed.stderr or completed.stdout)}"
        )
        raise PyrightRunError(msg)
    try:
        report = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        msg = f"Pyright printed no JSON report: {_quoted(completed.stderr)}"
        raise PyrightRunError(msg) from error
    if report.get("version") != PYRIGHT_VERSION:
        msg = (
            f"Pyright reported version {report.get('version')}, "
            f"not the pinned {PYRIGHT_VERSION}"
        )
        raise PyrightRunError(msg)
    if not report.get("summary", {}).get("filesAnalyzed"):
        msg = (
            "Pyright checked no files; list what it checks in the package's "
            "[tool.pyright] include"
        )
        raise PyrightRunError(msg)
    return report


def _normalized(package: str) -> str:
    """Return a package directory as the registry writes it."""
    return Path(os.path.normpath(package)).as_posix()


def listed_packages(registry: Registry) -> list[str]:
    """Return every package a Pyright rule lists, in registry order."""
    return [
        _normalized(project)
        for rule in registry.rules
        if rule.enforcer.kind is Kind.PYRIGHT
        for project in rule.enforcer.get("projects", [])
    ]


def unlisted_packages(registry: Registry, root: Path) -> list[str]:
    """Return each uv package under ``root`` that no Pyright rule lists."""
    listed = set(listed_packages(registry))
    packages = {
        path.parent.relative_to(root).as_posix()
        for pattern in PACKAGE_GLOBS
        for path in root.glob(pattern)
    }
    return sorted(packages - listed)


def check_coverage(root: Path = REPO_ROOT) -> list[str]:
    """Report each uv package no Pyright rule lists; run_quality_guards.py runs it."""
    registry, violations = load_registry(root)
    if registry is None:
        return [str(violation) for violation in violations]
    rule_id = next(
        (
            rule.id
            for rule in registry.rules
            if rule.enforcer.kind is Kind.QUALITY_GUARD
            and rule.enforcer.get("name") == COVERAGE_GUARD
        ),
        None,
    )
    return [
        str(
            Violation(
                rule_id,
                COVERAGE_GUARD,
                f"package {package} has no Pyright rule, so CI doesn't type-check it",
                "add it to a Pyright rule's enforcer.projects in rules/registry.toml "
                "and to the pyright job's matrix in .github/workflows/pr.yml",
            )
        )
        for package in unlisted_packages(registry, root)
    ]


def rule_for(registry: Registry, package: str) -> Rule | None:
    """Return the Pyright rule whose enforcer lists ``package``, if any."""
    for rule in registry.rules:
        projects = rule.enforcer.get("projects", [])
        if rule.enforcer.kind is Kind.PYRIGHT and package in map(_normalized, projects):
            return rule
    return None


def _baseline_header(rule: Rule, package: str) -> str:
    """Return the comment block that starts a new baseline file."""
    return (
        f"# Tolerated Pyright errors in {package}.\n"
        "# This list may only shrink.\n"
        "#\n"
        f"# Rule {rule.id}. Each entry is\n"
        "# <path> :: <Pyright rule> :: <first line of the message>, one line per\n"
        "# error, sorted. After fixing errors, remove the entries that no longer\n"
        "# occur with\n"
        f"#   python -m scripts.rules pyright {package} --write-baseline\n"
        "# then set the rule's [rule.baseline] count in rules/registry.toml to match.\n"
    )


def _existing_header(path: Path) -> str:
    """Return the comment and blank lines that open an existing baseline file."""
    header = []
    for line in path.read_text().splitlines():
        if line.strip() and not line.lstrip().startswith("#"):
            break
        header.append(line)
    return "".join(f"{line}\n" for line in header)


def _entry_count(count: int) -> str:
    return f"{count} entry" if count == 1 else f"{count} entries"


def _error_count(count: int) -> str:
    return f"{count} Pyright error" if count == 1 else f"{count} Pyright errors"


@dataclass(frozen=True)
class _PackageCheck:
    """One package's rule and what Pyright reported for it."""

    root: Path
    package: str
    rule: Rule
    errors: list[PyrightError]

    @property
    def label(self) -> str:
        return f"({self.rule.id}, {self.rule.status})"


def _check_enforced(check: _PackageCheck) -> tuple[bool, list[str]]:
    if not check.errors:
        return True, [f"{check.package}: passed with no Pyright errors {check.label}"]
    lines = [
        f"{check.package}: failed with {_error_count(len(check.errors))}, where "
        f"{check.rule.id} ({check.rule.status}) allows none"
    ]
    for error in check.errors:
        lines += error.describe("error")
    return False, [*lines, ENFORCED_FIX]


def _check_ratcheted(
    check: _PackageCheck, baseline_file: str
) -> tuple[bool, list[str]]:
    path = check.root / baseline_file
    comparison = compare(check.errors, read_baseline(path) if path.is_file() else [])
    if comparison.passed:
        return True, [
            f"{check.package}: passed with {_error_count(len(check.errors))}, "
            f"each listed in {baseline_file} {check.label}"
        ]
    lines = [
        f"{check.package}: failed, its Pyright errors differ from "
        f"{baseline_file} {check.label}"
    ]
    for error in comparison.new_errors:
        lines += error.describe("new error")
    lines += [f"  no longer occurs: {entry}" for entry in comparison.stale_entries]
    if comparison.new_errors:
        lines.append(NEW_ERROR_FIX)
    if comparison.stale_entries:
        lines.append(
            "  Fix: remove the entries that no longer occur with "
            f"python -m scripts.rules pyright {check.package} --write-baseline, "
            f"then set count under [rule.baseline] of {check.rule.id} to the "
            "number left."
        )
    return False, lines


def _write_baseline(check: _PackageCheck, baseline_file: str) -> tuple[bool, list[str]]:
    """Rewrite the baseline as the errors that occur now; never add one to it."""
    path = check.root / baseline_file
    if path.is_file():
        comparison = compare(check.errors, read_baseline(path))
        if comparison.new_errors:
            lines = [
                f"{check.package}: failed, --write-baseline never adds an error "
                f"to {baseline_file}"
            ]
            for error in comparison.new_errors:
                lines += error.describe("new error")
            return False, [*lines, NEW_ERROR_FIX]
        header = _existing_header(path)
    else:
        header = _baseline_header(check.rule, check.package)
    entries = sorted(error.baseline_entry for error in check.errors)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(header + "".join(f"{entry}\n" for entry in entries))
    return True, [
        f"{check.package}: wrote {baseline_file} with {_entry_count(len(entries))}; "
        f"set count = {len(entries)} under [rule.baseline] of {check.rule.id} "
        "in rules/registry.toml"
    ]


def check_package(
    registry: Registry,
    root: Path,
    package: str,
    *,
    sync: bool = False,
    write_baseline: bool = False,
) -> tuple[bool, list[str]]:
    """Check one package against its Pyright rule; return (passed, output lines).

    With ``sync``, first create or update the package's locked environment.
    With ``write_baseline``, rewrite a ratcheted rule's baseline as the errors
    that occur now, refusing when one of them is new.
    """
    package = _normalized(package)
    rule = rule_for(registry, package)
    if rule is None:
        return False, [
            f"{package}: failed, no Pyright rule lists it in enforcer.projects",
            "  Fix: add it to a Pyright rule in rules/registry.toml.",
        ]
    baseline = rule.baseline if rule.status is Status.RATCHETED else None
    if write_baseline and baseline is None:
        return False, [
            f"{package}: failed, {rule.id} is {rule.status}, "
            f"so {package} has no baseline",
            "  Fix: fix each error instead.",
        ]
    try:
        if sync:
            sync_environment(root / package)
        report = run_pyright(root / package)
    except PyrightRunError as error:
        return False, [f"{package}: failed, Pyright could not check it: {error}"]
    check = _PackageCheck(root, package, rule, errors_in_report(report, root))
    if baseline is None:
        return _check_enforced(check)
    if write_baseline:
        return _write_baseline(check, baseline.file)
    return _check_ratcheted(check, baseline.file)
