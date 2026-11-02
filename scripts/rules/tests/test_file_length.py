"""Tests for the file-length guard: no tracked Python file is too long."""

from __future__ import annotations

import subprocess
import tempfile
import tomllib
import unittest
from pathlib import Path

from scripts.rules.file_length import (
    SOURCE_LINE_LIMIT,
    TEST_LINE_LIMIT,
    LongFile,
    compare,
    count_lines,
    find_long_files,
    is_test_code,
    line_limit,
    run_check,
)
from scripts.rules.load import load_text

REPO_ROOT = Path(__file__).resolve().parents[3]
FIXTURES = REPO_ROOT / "rules" / "fixtures" / "sim.quality.python-file-length"

FILE_LENGTH_RULE = """
schema_version = 1
repo = "example"

[[category]]
id = "quality"
title = "Quality"

[[rule]]
id = "ex.quality.python-file-length"
category = "quality"
statement = "No Python file is too long."
rationale = "docs/guide.md"
scope = ["**/*.py"]
tier = "mechanical"
status = "ratcheted"
autofix = false
[rule.enforcer]
kind = "quality-guard"
name = "file-length"
[[rule.ci]]
workflow = ".github/workflows/pr.yml"
job = "quality-guards"
invocation = "run_quality_guards.py"
[rule.baseline]
file = "rules/baselines/python-file-length.txt"
count = 1
"""


def write_lines(root: Path, path: str, line_count: int) -> None:
    """Write a file of ``line_count`` short Python lines at ``root / path``."""
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        "".join(f"value_{index} = {index}\n" for index in range(line_count))
    )


def materialize(fixture_name: str, root: Path) -> list[dict[str, object]]:
    """Write every file a proof fixture manifest lists under ``root``; return them."""
    manifest = tomllib.loads((FIXTURES / fixture_name).read_text())
    files = manifest["file"]
    for listed_file in files:
        write_lines(root, str(listed_file["path"]), int(listed_file["lines"]))
    return files


def long_paths_in_fixture(fixture_name: str) -> tuple[list[str], list[str]]:
    """Return (paths the guard finds too long, paths the manifest lists)."""
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        files = materialize(fixture_name, root)
        found = [long_file.path for long_file in find_long_files(root)]
    return found, sorted(str(listed_file["path"]) for listed_file in files)


class TestFileLengthProof(unittest.TestCase):
    """Proof: the guard rejects files one line over their limit, accepts the rest."""

    def test_rule_proof_python_file_length(self) -> None:
        found, listed = long_paths_in_fixture("bad_files.toml")
        self.assertEqual(found, listed)

        found, _ = long_paths_in_fixture("good_files.toml")
        self.assertEqual(found, [])


class TestLineLimit(unittest.TestCase):
    def test_files_under_tests_and_fixtures_directories_are_test_code(self) -> None:
        for path in (
            "projects/example/tests/test_service.py",
            "projects/example/tests/support/frames.py",
            "scripts/rules/tests/test_guard.py",
            "rules/fixtures/sim.example/bad.py",
            "projects/example/src/example/fixtures/sample.py",
        ):
            with self.subTest(path=path):
                self.assertTrue(is_test_code(path))
                self.assertEqual(line_limit(path), TEST_LINE_LIMIT)

    def test_conftest_files_are_test_code_anywhere(self) -> None:
        self.assertTrue(is_test_code("conftest.py"))
        self.assertTrue(is_test_code("projects/example/conftest.py"))

    def test_other_files_are_source_code(self) -> None:
        for path in (
            "projects/example/src/example/service.py",
            "projects/example/src/example/test_data.py",
            "projects/example/src/example/testing.py",
            "projects/example/tests.py",
            "scripts/run_quality_guards.py",
            ".github/scripts/check_pr_description.py",
        ):
            with self.subTest(path=path):
                self.assertFalse(is_test_code(path))
                self.assertEqual(line_limit(path), SOURCE_LINE_LIMIT)


class TestCountLines(unittest.TestCase):
    def test_counts_physical_lines_including_an_unterminated_last_line(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            file = Path(directory) / "module.py"
            cases = (
                (b"", 0),
                (b"first = 1\n", 1),
                (b"first = 1\nsecond = 2", 2),
                (b"first = 1\r\nsecond = 2\r\n", 2),
                (b"\n\n\n", 3),
                (b'text = """a\x0cb"""\n', 1),
            )
            for content, expected in cases:
                with self.subTest(content=content):
                    file.write_bytes(content)
                    self.assertEqual(count_lines(file), expected)


class TestTrackedFiles(unittest.TestCase):
    def test_measures_only_files_git_tracks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_lines(root, "pkg/tracked.py", SOURCE_LINE_LIMIT + 1)
            write_lines(root, "pkg/untracked.py", SOURCE_LINE_LIMIT + 1)
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            subprocess.run(["git", "add", "pkg/tracked.py"], cwd=root, check=True)

            found = find_long_files(root)

        self.assertEqual(
            found,
            [LongFile("pkg/tracked.py", SOURCE_LINE_LIMIT + 1, SOURCE_LINE_LIMIT)],
        )


class TestCompare(unittest.TestCase):
    def setUp(self) -> None:
        registry, problems = load_text(FILE_LENGTH_RULE)
        assert registry is not None, problems
        self.rule = registry.rules[0]

    def test_an_unlisted_long_file_fails(self) -> None:
        found = [LongFile("pkg/service.py", 801, SOURCE_LINE_LIMIT)]

        messages = [str(violation) for violation in compare(found, set(), self.rule)]

        self.assertEqual(len(messages), 1)
        self.assertIn(
            "[ex.quality.python-file-length] file-length: pkg/service.py has 801 "
            "lines, over the 800-line limit for source code",
            messages[0],
        )

    def test_a_long_test_file_names_the_test_code_limit(self) -> None:
        found = [LongFile("tests/test_service.py", 1501, TEST_LINE_LIMIT)]

        [violation] = compare(found, set(), self.rule)

        self.assertIn("over the 1500-line limit for test code", violation.message)

    def test_a_listed_long_file_passes(self) -> None:
        found = [LongFile("pkg/service.py", 801, SOURCE_LINE_LIMIT)]

        self.assertEqual(compare(found, {"pkg/service.py"}, self.rule), [])

    def test_a_listed_file_within_its_limit_fails_so_the_baseline_shrinks(
        self,
    ) -> None:
        [violation] = compare([], {"pkg/service.py"}, self.rule)

        self.assertEqual(
            violation.message,
            "baseline entry no longer applies: pkg/service.py is within its limit "
            "or is no longer tracked",
        )
        self.assertEqual(
            violation.fix,
            "remove it from rules/baselines/python-file-length.txt and lower the "
            "rule's baseline count",
        )


class TestRunCheck(unittest.TestCase):
    def check_tree(self, baseline: str) -> list[str]:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "rules" / "baselines").mkdir(parents=True)
            (root / "rules" / "registry.toml").write_text(FILE_LENGTH_RULE)
            (root / "rules" / "baselines" / "python-file-length.txt").write_text(
                baseline
            )
            write_lines(root, "pkg/listed.py", SOURCE_LINE_LIMIT + 1)
            write_lines(root, "pkg/short.py", SOURCE_LINE_LIMIT)
            return [violation.message for violation in run_check(root)]

    def test_reads_the_baseline_the_rule_names(self) -> None:
        self.assertEqual(self.check_tree("# Tolerated.\npkg/listed.py\n"), [])

    def test_reports_long_files_the_baseline_leaves_out(self) -> None:
        self.assertEqual(
            self.check_tree("# Tolerated.\npkg/short.py\n"),
            [
                "pkg/listed.py has 801 lines, over the 800-line limit for source code",
                "baseline entry no longer applies: pkg/short.py is within its limit "
                "or is no longer tracked",
            ],
        )


if __name__ == "__main__":
    unittest.main()
