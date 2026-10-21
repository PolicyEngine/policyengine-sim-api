"""Tests for the locked-installs guard: lock, install and test from uv.lock."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from scripts.rules.load import load_text
from scripts.rules.locked_installs import (
    INSTALL_RULE,
    find_violations,
    lock_violations,
    relock_violations,
    scan,
    tracked_files,
)

FIXTURES = Path(__file__).parent / "fixtures" / "locked_installs"
REGISTRY_HEADER = """
schema_version = 1
repo = "example"

[[category]]
id = "dependencies"
title = "Dependencies"
"""
RULE_TEMPLATE = """
[[rule]]
id = "{rule_id}"
category = "dependencies"
statement = "Installs come from uv.lock."
rationale = "docs/x.md"
scope = ["**"]
tier = "mechanical"
status = "enforced"
autofix = false
[rule.enforcer]
kind = "quality-guard"
name = "locked-installs"
"""


def registry_with(exceptions: str = "", rule_id: str = INSTALL_RULE) -> object:
    text = REGISTRY_HEADER + RULE_TEMPLATE.format(rule_id=rule_id) + exceptions
    registry, violations = load_text(text)
    assert violations == [], violations
    return registry


def violation_messages(root: Path, exceptions: str = "") -> list[str]:
    return [
        violation.message
        for violation in find_violations(root, registry_with(exceptions))
        if violation.rule_id == INSTALL_RULE
    ]


class TestLockedInstallsProof(unittest.TestCase):
    def test_rule_proof_install_from_lock(self) -> None:
        bad = scan(FIXTURES / "install_bad")
        good = scan(FIXTURES / "install_good")

        self.assertEqual(
            sorted(site.text for site in bad.unlocked_installs),
            [
                "pip install policyengine",
                "pip install requests",
                "pip_install(policyengine-us)",
                "uv sync --extra test",
            ],
        )
        self.assertEqual(good.unlocked_installs, [])

    def test_rule_proof_test_from_lock(self) -> None:
        bad = scan(FIXTURES / "test_bad")
        good = scan(FIXTURES / "test_good")

        self.assertEqual(
            sorted(site.text for site in bad.unlocked_tests),
            ["coverage run -m pytest", "python -m pytest tests/"],
        )
        self.assertEqual(good.unlocked_tests, [])

    def test_rule_proof_lock_committed(self) -> None:
        bad_root = FIXTURES / "lock_bad"
        good_root = FIXTURES / "lock_good"

        self.assertEqual(
            [
                violation.message
                for violation in lock_violations(bad_root, tracked_files(bad_root))
            ],
            [
                "app/pyproject.toml has no committed uv.lock",
                "no .github/workflows/pr.yml job runs uv lock --check",
            ],
        )
        self.assertEqual(lock_violations(good_root, tracked_files(good_root)), [])

    def test_rule_proof_scheduled_relock(self) -> None:
        bad_root = FIXTURES / "relock_bad"
        good_root = FIXTURES / "relock_good"

        self.assertEqual(
            [
                violation.message
                for violation in relock_violations(bad_root, tracked_files(bad_root))
            ],
            [".github/workflows/update.yml does not relock projects/two/uv.lock"],
        )
        self.assertEqual(relock_violations(good_root, tracked_files(good_root)), [])


class TestCommandClassification(unittest.TestCase):
    def scan_script(self, script: str) -> tuple[list[str], list[str]]:
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        (root / "run.sh").write_text(script, encoding="utf-8")
        findings = scan(root)
        return (
            [site.text for site in findings.unlocked_installs],
            [site.text for site in findings.unlocked_tests],
        )

    def test_frozen_and_locked_syncs_and_runs_are_locked(self) -> None:
        installs, tests = self.scan_script(
            "uv sync --frozen --extra test\n"
            "uv sync --locked\n"
            "uv run --frozen pytest tests/\n"
            "uv run --locked python -m unittest\n"
            "uv lock --upgrade\n"
        )

        self.assertEqual((installs, tests), ([], []))

    def test_every_unlocked_installer_is_reported(self) -> None:
        installs, _tests = self.scan_script(
            "uv run python -m tool\n"
            "uv run --frozen --with requests python x.py\n"
            "uv pip install requests\n"
            "uv tool install ruff\n"
            "uvx ruff check\n"
            "python -m pip install requests\n"
            "pipx install poetry\n"
            "uv add requests\n"
            "poetry install\n"
        )

        self.assertEqual(len(installs), 9, installs)

    def test_continuations_and_chains_are_read_as_commands(self) -> None:
        installs, tests = self.scan_script(
            "cd app && uv sync \\\n  --extra test && uv run pytest\n"
        )

        self.assertEqual(installs, ["uv sync  --extra test", "uv run pytest"])
        self.assertEqual(tests, ["uv run pytest"])

    def test_comments_and_echoed_instructions_are_not_commands(self) -> None:
        installs, tests = self.scan_script(
            '# uv sync --extra test\necho "  uv sync"\nprintf "pip install x"\n'
        )

        self.assertEqual((installs, tests), ([], []))


class TestExceptions(unittest.TestCase):
    def test_an_exception_allows_only_the_site_it_names(self) -> None:
        exceptions = """
[[rule.exception]]
path = ".github/workflows/ci.yml"
match = "pip install requests"
reason = "example"
removal = "never"
"""
        messages = violation_messages(FIXTURES / "install_bad", exceptions)

        self.assertFalse(any("pip install requests" in m for m in messages))
        self.assertTrue(any("uv sync --extra test" in m for m in messages))

    def test_an_exception_needs_a_site_and_an_end(self) -> None:
        exceptions = """
[[rule.exception]]
path = ".github/workflows/ci.yml"
reason = "no site and no end"
"""
        messages = violation_messages(FIXTURES / "install_bad", exceptions)

        self.assertIn("exception for .github/workflows/ci.yml names no site", messages)
        self.assertIn(
            "exception for .github/workflows/ci.yml has no removal condition "
            "and no tracking issue",
            messages,
        )

    def test_a_dated_exception_with_an_issue_is_accepted(self) -> None:
        exceptions = """
[[rule.exception]]
path = "Dockerfile"
match = "pip install policyengine"
reason = "example"
expires = "2099-01-01"
issue = 1
"""
        messages = violation_messages(FIXTURES / "install_bad", exceptions)

        self.assertFalse(any("Dockerfile" in m for m in messages), messages)

    def test_a_pinned_tool_exception_must_pin_an_exact_version(self) -> None:
        exceptions = """
[[rule.exception]]
path = ".github/workflows/ci.yml"
match = "pip install requests"
category = "pinned-tool"
reason = "example"
"""
        messages = violation_messages(FIXTURES / "install_bad", exceptions)

        self.assertIn(
            "pinned-tool exception for .github/workflows/ci.yml does not pin an "
            "exact version",
            messages,
        )

    def test_an_exception_that_matches_no_site_is_stale(self) -> None:
        exceptions = """
[[rule.exception]]
path = ".github/workflows/ci.yml"
match = "pip install gone"
reason = "example"
removal = "never"
"""
        messages = violation_messages(FIXTURES / "install_good", exceptions)

        self.assertEqual(
            messages,
            [
                "exception for .github/workflows/ci.yml (pip install gone) "
                "matches no site"
            ],
        )


if __name__ == "__main__":
    unittest.main()
