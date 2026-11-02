"""The file-length quality guard: no tracked Python file is too long.

A tracked Python file may have at most 800 lines, or at most 1,500 when it is
test code: a file under a ``tests`` or ``fixtures`` directory, or a
``conftest.py``. While the rule is ratcheted, its baseline file lists the files
that were already over their limit. The guard fails on a file over its limit
that the baseline doesn't list, and on a listed file that is no longer over its
limit or no longer tracked, so the baseline only shrinks.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

from scripts.rules.load import load_registry
from scripts.rules.locked_installs import tracked_files
from scripts.rules.model import Kind, Registry, Rule, Violation

if TYPE_CHECKING:
    from collections.abc import Iterable

REPO_ROOT = Path(__file__).resolve().parents[2]
GUARD_NAME = "file-length"
SOURCE_LINE_LIMIT = 800
TEST_LINE_LIMIT = 1500
# A file under a directory with one of these names is test code.
TEST_DIRECTORIES = frozenset({"tests", "fixtures"})
TEST_FILE_NAMES = frozenset({"conftest.py"})
SPLIT_FIX = (
    "split it into modules that each hold one responsibility and stay under the "
    "limit, keeping its public names importable from the original module "
    "(see docs/engineering/skills/file-length.md)"
)


@dataclass(frozen=True)
class LongFile:
    """A tracked Python file with more lines than its limit allows."""

    path: str
    lines: int
    limit: int

    def describe(self) -> str:
        """Say how long the file is and which limit it exceeds."""
        kind = "test code" if self.limit == TEST_LINE_LIMIT else "source code"
        return (
            f"{self.path} has {self.lines} lines, over the {self.limit}-line "
            f"limit for {kind}"
        )


def is_test_code(path: str) -> bool:
    """Whether the repository-relative ``path`` holds tests or test support."""
    pure = PurePosixPath(path)
    return pure.name in TEST_FILE_NAMES or any(
        part in TEST_DIRECTORIES for part in pure.parts[:-1]
    )


def line_limit(path: str) -> int:
    """Return the most lines the file at ``path`` may have."""
    return TEST_LINE_LIMIT if is_test_code(path) else SOURCE_LINE_LIMIT


def count_lines(file: Path) -> int:
    """Count the file's physical lines, including a last one without a newline."""
    return len(file.read_bytes().splitlines())


def long_files(root: Path, paths: Iterable[str]) -> list[LongFile]:
    """Return the Python files among ``paths`` that are over their limit, by path."""
    found = []
    for path in sorted(paths):
        if not path.endswith(".py"):
            continue
        lines = count_lines(root / path)
        limit = line_limit(path)
        if lines > limit:
            found.append(LongFile(path, lines, limit))
    return found


def find_long_files(root: Path) -> list[LongFile]:
    """Return every tracked Python file under ``root`` that is over its limit."""
    return long_files(root, tracked_files(root))


def file_length_rule(registry: Registry) -> Rule | None:
    """Return the rule this guard enforces, if the registry has one."""
    return next(
        (
            rule
            for rule in registry.rules
            if rule.enforcer.kind is Kind.QUALITY_GUARD
            and rule.enforcer.get("name") == GUARD_NAME
        ),
        None,
    )


def baseline_entries(root: Path, rule: Rule | None) -> set[str]:
    """Return the paths the rule's baseline lists, skipping blanks and comments."""
    if rule is None or rule.baseline is None:
        return set()
    file = root / rule.baseline.file
    if not file.is_file():
        return set()
    return {
        line.strip()
        for line in file.read_text().splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }


def compare(
    found: list[LongFile], baseline: set[str], rule: Rule | None
) -> list[Violation]:
    """Report long files the baseline doesn't list, and listed files now fixed."""
    rule_id = rule.id if rule is not None else None
    violations = [
        Violation(rule_id, GUARD_NAME, long_file.describe(), SPLIT_FIX)
        for long_file in found
        if long_file.path not in baseline
    ]
    if baseline:
        baseline_file = rule.baseline.file if rule and rule.baseline else "baseline"
        found_paths = {long_file.path for long_file in found}
        violations.extend(
            Violation(
                rule_id,
                GUARD_NAME,
                f"baseline entry no longer applies: {path} is within its limit "
                "or is no longer tracked",
                f"remove it from {baseline_file} and lower the rule's baseline count",
            )
            for path in sorted(baseline - found_paths)
        )
    return violations


def run_check(root: Path = REPO_ROOT) -> list[Violation]:
    """Load the registry under ``root`` and return every file-length violation."""
    registry, violations = load_registry(root)
    if registry is None:
        return violations
    rule = file_length_rule(registry)
    return compare(find_long_files(root), baseline_entries(root, rule), rule)


def check() -> list[str]:
    """Adapter for scripts/run_quality_guards.py."""
    return [str(violation) for violation in run_check(REPO_ROOT)]
