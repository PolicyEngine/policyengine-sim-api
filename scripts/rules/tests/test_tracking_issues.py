"""Tests for the check that each [rule.downgrade] names an open tracking issue.

A fake ``gh`` first on PATH answers the lookup, so no test reaches GitHub. The
fake prints ``<number>.json`` from its answers directory and succeeds, or
prints ``<number>.stderr`` and fails.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts.rules import guard, tracking_issues
from scripts.rules.load import load_text
from scripts.rules.tests.support import REGISTRY, Repo

ISSUE_REPOSITORY = "example/repo"
REGISTRY_WITH_ISSUES = REGISTRY.replace(
    'repo = "example"\n',
    f'repo = "example"\nissue_repository = "{ISSUE_REPOSITORY}"\n',
)
FAKE_GH = """#!/bin/sh
printf '%s\\n' "$*" >> "$FAKE_GH_CALLS"
answer="$FAKE_GH_ANSWERS/${2##*/}"
if [ -f "$answer.json" ]; then
  cat "$answer.json"
  exit 0
fi
cat "$answer.stderr" >&2
exit 1
"""
NOT_FOUND = "gh: Not Found (HTTP 404)"


def downgraded_rule(rule_id: str, issue: int) -> str:
    """Return a ratcheted rule whose [rule.downgrade] names ``issue``."""
    return f"""
[[rule]]
id = "{rule_id}"
category = "layers"
statement = "Ratcheted rule."
rationale = "docs/guide.md"
scope = ["pkg/*.py"]
tier = "mechanical"
status = "ratcheted"
autofix = false
[rule.enforcer]
kind = "pytest"
nodes = ["tests/unit/test_boundaries.py::test_routes_have_no_sql"]
[[rule.ci]]
workflow = ".github/workflows/pr.yml"
job = "test"
invocation = "make test"
[rule.downgrade]
reason = "a checker upgrade"
issue = {issue}
[rule.baseline]
file = "rules/baselines/{rule_id}.txt"
count = 1
"""


def issue_answer(
    number: int,
    *,
    state: str = "open",
    repository: str = ISSUE_REPOSITORY,
    **fields: object,
) -> dict[str, object]:
    """Return the fields gh api prints for an issue, with ``fields`` added."""
    return {
        "number": number,
        "state": state,
        "repository_url": f"https://api.github.com/repos/{repository}",
        **fields,
    }


class TrackingIssueTestCase(unittest.TestCase):
    """Puts the fake gh first on PATH, outside CI and without a token."""

    def setUp(self) -> None:
        fake_directory = tempfile.TemporaryDirectory()
        self.addCleanup(fake_directory.cleanup)
        self.fake_root = Path(fake_directory.name)
        self.bin_directory = self.fake_root / "bin"
        self.answers = self.fake_root / "answers"
        self.bin_directory.mkdir()
        self.answers.mkdir()
        fake_gh = self.bin_directory / "gh"
        fake_gh.write_text(FAKE_GH)
        fake_gh.chmod(0o755)
        self.calls_file = self.fake_root / "calls.txt"
        environment = mock.patch.dict(
            os.environ,
            {
                "PATH": f"{self.bin_directory}{os.pathsep}{os.environ['PATH']}",
                "FAKE_GH_CALLS": str(self.calls_file),
                "FAKE_GH_ANSWERS": str(self.answers),
            },
        )
        environment.start()
        self.addCleanup(environment.stop)
        for name in ("GH_TOKEN", "GITHUB_TOKEN", "GITHUB_ACTIONS"):
            os.environ.pop(name, None)

    def answer(self, number: int, issue: dict[str, object]) -> None:
        """Make the fake gh print ``issue`` for ``number`` and succeed."""
        (self.answers / f"{number}.json").write_text(json.dumps(issue))

    def fail_lookup(self, number: int, stderr: str) -> None:
        """Make the fake gh print ``stderr`` for ``number`` and fail."""
        (self.answers / f"{number}.stderr").write_text(stderr)

    def gh_calls(self) -> list[str]:
        """Return the arguments of each gh call, one string per call."""
        if not self.calls_file.exists():
            return []
        return self.calls_file.read_text().splitlines()

    def check(self, rules: str) -> tuple[list[str], list[str]]:
        """Check the registry with ``rules`` added; return violations and notes."""
        registry, problems = load_text(REGISTRY_WITH_ISSUES + rules)
        assert registry is not None and not problems, problems
        violations, notes = tracking_issues.check_tracking_issues(registry)
        return [str(violation) for violation in violations], notes


class LookupTest(TrackingIssueTestCase):
    """With a token, each downgrade's issue is looked up, and must be open."""

    def setUp(self) -> None:
        super().setUp()
        os.environ["GH_TOKEN"] = "token-for-the-fake-gh"

    def test_an_open_issue_passes(self) -> None:
        self.answer(912, issue_answer(912))

        self.assertEqual(
            self.check(downgraded_rule("ex.layers.ratcheted", 912)),
            ([], ["rule ex.layers.ratcheted: tracking issue #912 is open"]),
        )
        self.assertEqual(self.gh_calls(), [f"api repos/{ISSUE_REPOSITORY}/issues/912"])

    def test_a_closed_issue_fails(self) -> None:
        self.answer(912, issue_answer(912, state="closed"))

        violations, _ = self.check(downgraded_rule("ex.layers.ratcheted", 912))

        self.assertEqual(
            violations,
            [
                "[ex.layers.ratcheted] downgrade: issue = 912 names no open issue: "
                "Issue #912 is closed. Fix: reopen issue #912 while the rule stays "
                "weakened, or restore the rule and remove [rule.downgrade]."
            ],
        )

    def test_a_number_that_names_no_issue_here_fails(self) -> None:
        pull_request = issue_answer(
            912, pull_request={"url": "https://api.github.com/pulls/912"}
        )
        moved = issue_answer(5, repository="example/elsewhere")
        cases = {
            "There is no issue #912 in example/repo": None,
            "#912 is a pull request, not an issue": pull_request,
            "#912 is not an issue in example/repo: it moved to example/elsewhere#5": (
                moved
            ),
        }
        for problem, answer in cases.items():
            with self.subTest(problem=problem):
                for previous_answer in self.answers.iterdir():
                    previous_answer.unlink()
                if answer is None:
                    self.fail_lookup(912, NOT_FOUND)
                else:
                    self.answer(912, answer)

                violations, _ = self.check(downgraded_rule("ex.layers.ratcheted", 912))

                self.assertEqual(
                    violations,
                    [
                        "[ex.layers.ratcheted] downgrade: issue = 912 names no open "
                        f"issue: {problem}. Fix: set issue to the number of an open "
                        "issue in example/repo that tracks restoring the rule."
                    ],
                )

    def test_a_failed_lookup_fails(self) -> None:
        self.fail_lookup(912, "gh: Bad credentials (HTTP 401)")

        violations, _ = self.check(downgraded_rule("ex.layers.ratcheted", 912))

        self.assertEqual(
            violations,
            [
                "[ex.layers.ratcheted] downgrade: couldn't check that issue #912 is "
                "open: Could not look up #912 in example/repo: gh: Bad credentials "
                "(HTTP 401). Fix: run the guard again; if it fails again, check "
                "the token in GH_TOKEN, which needs read access to the issues of "
                "example/repo."
            ],
        )

    def test_a_missing_gh_fails(self) -> None:
        empty_directory = self.fake_root / "empty"
        empty_directory.mkdir()
        os.environ["PATH"] = str(empty_directory)

        violations, _ = self.check(downgraded_rule("ex.layers.ratcheted", 912))

        self.assertEqual(len(violations), 1)
        self.assertIn(
            "[ex.layers.ratcheted] downgrade: couldn't check that issue #912 is "
            "open: gh could not run (",
            violations[0],
        )

    def test_github_token_is_a_token_too(self) -> None:
        del os.environ["GH_TOKEN"]
        os.environ["GITHUB_TOKEN"] = "token-for-the-fake-gh"
        self.answer(912, issue_answer(912))

        self.assertEqual(self.check(downgraded_rule("ex.layers.ratcheted", 912))[0], [])
        self.assertEqual(len(self.gh_calls()), 1)

    def test_each_downgrade_is_checked(self) -> None:
        self.answer(912, issue_answer(912))
        self.answer(913, issue_answer(913, state="closed"))

        violations, notes = self.check(
            downgraded_rule("ex.layers.first", 912)
            + downgraded_rule("ex.layers.second", 913)
        )

        self.assertEqual(len(violations), 1)
        self.assertIn("[ex.layers.second] downgrade: issue = 913", violations[0])
        self.assertEqual(notes, ["rule ex.layers.first: tracking issue #912 is open"])
        self.assertEqual(
            self.gh_calls(),
            [
                f"api repos/{ISSUE_REPOSITORY}/issues/912",
                f"api repos/{ISSUE_REPOSITORY}/issues/913",
            ],
        )

    def test_the_lookup_must_load(self) -> None:
        missing_script = self.fake_root / "missing.py"
        script_without_the_lookup = self.fake_root / "renamed.py"
        script_without_the_lookup.write_text("def look_up_issue():\n    pass\n")
        for script in (missing_script, script_without_the_lookup):
            with self.subTest(script=script.name):
                with mock.patch.object(tracking_issues, "LOOKUP_SCRIPT", script):
                    violations, _ = self.check(
                        downgraded_rule("ex.layers.ratcheted", 912)
                    )

                self.assertEqual(len(violations), 1)
                self.assertIn(
                    f"[registry] downgrade: couldn't load the issue lookup from "
                    f"{script}: ",
                    violations[0],
                )
                self.assertEqual(self.gh_calls(), [])

    def test_a_registry_without_downgrades_looks_nothing_up(self) -> None:
        self.assertEqual(self.check(""), ([], []))
        self.assertEqual(self.gh_calls(), [])


class WithoutATokenTest(TrackingIssueTestCase):
    """Without a token, CI fails; a local run notes what it didn't check."""

    def test_on_github_actions_a_missing_token_fails(self) -> None:
        os.environ["GITHUB_ACTIONS"] = "true"

        violations, notes = self.check(downgraded_rule("ex.layers.ratcheted", 912))

        self.assertEqual(
            violations,
            [
                "[ex.layers.ratcheted] downgrade: couldn't check that issue #912 is "
                "open: neither GH_TOKEN nor GITHUB_TOKEN is set. Fix: give the step "
                "the job's token, with env: GH_TOKEN: ${{ github.token }} and "
                "permissions: issues: read."
            ],
        )
        self.assertEqual(notes, [])
        self.assertEqual(self.gh_calls(), [])

    def test_elsewhere_a_missing_token_is_a_note(self) -> None:
        violations, notes = self.check(downgraded_rule("ex.layers.ratcheted", 912))

        self.assertEqual(violations, [])
        self.assertEqual(
            notes,
            [
                "rule ex.layers.ratcheted: tracking issue #912 was not checked, "
                "because neither GH_TOKEN nor GITHUB_TOKEN is set. CI checks it; "
                'to check it here, set one, for example GH_TOKEN="$(gh auth token)"'
            ],
        )
        self.assertEqual(self.gh_calls(), [])

    def test_on_github_actions_no_downgrade_needs_no_token(self) -> None:
        os.environ["GITHUB_ACTIONS"] = "true"

        self.assertEqual(self.check(""), ([], []))


class GuardTest(TrackingIssueTestCase):
    """The rules guard checks tracking issues, and prints what it didn't check."""

    def repo(self) -> Repo:
        """Return a valid repository whose registry has one downgrade."""
        repo = Repo(
            {
                "rules/registry.toml": REGISTRY_WITH_ISSUES
                + downgraded_rule("ex.layers.ratcheted", 912),
                "rules/baselines/ex.layers.ratcheted.txt": "pkg/routes.py\n",
            }
        )
        self.addCleanup(repo.__exit__)
        return repo

    def test_the_guard_fails_on_a_closed_tracking_issue(self) -> None:
        repo = self.repo()
        os.environ["GH_TOKEN"] = "token-for-the-fake-gh"
        self.answer(912, issue_answer(912, state="closed"))

        self.assertEqual(
            [message for message in repo.messages() if "downgrade" in message],
            [
                "[ex.layers.ratcheted] downgrade: issue = 912 names no open issue: "
                "Issue #912 is closed. Fix: reopen issue #912 while the rule stays "
                "weakened, or restore the rule and remove [rule.downgrade]."
            ],
        )

    def test_the_quality_guard_prints_the_guard_notes(self) -> None:
        repo = self.repo()
        printed = io.StringIO()

        with contextlib.redirect_stdout(printed):
            violations = guard.check(repo.root)

        self.assertEqual(violations, [])
        self.assertIn(
            "note: rule ex.layers.ratcheted: tracking issue #912 was not checked, ",
            printed.getvalue(),
        )


class RepositoryLookupTest(unittest.TestCase):
    def test_the_pr_description_check_provides_the_lookup(self) -> None:
        lookup = tracking_issues._load_lookup()

        for name in tracking_issues.LOOKUP_NAMES:
            self.assertTrue(hasattr(lookup, name), name)

    def test_the_registry_names_this_repository_for_tracking_issues(self) -> None:
        registry, problems = load_text(
            (tracking_issues.REPO_ROOT / "rules/registry.toml").read_text()
        )

        self.assertEqual(problems, [])
        assert registry is not None
        self.assertEqual(registry.issue_repository, "PolicyEngine/policyengine-sim-api")


class RegistrySchemaTest(unittest.TestCase):
    """Where tracking issues live, and which rules may have a downgrade."""

    def test_the_issue_repository_is_owner_and_name(self) -> None:
        for value in ('"repo"', '"a/b/c"', '"owner/"', "912"):
            with self.subTest(value=value):
                _, problems = load_text(
                    REGISTRY.replace(
                        'repo = "example"\n',
                        f'repo = "example"\nissue_repository = {value}\n',
                    )
                )

                self.assertEqual(
                    [str(problem) for problem in problems],
                    [
                        "[registry] schema: registry.issue_repository must be "
                        "<owner>/<name>, the GitHub repository that tracks issues. "
                        'Fix: set issue_repository = "<owner>/<name>".'
                    ],
                )

    def test_a_downgrade_needs_the_issue_repository(self) -> None:
        with Repo(
            {
                "rules/registry.toml": REGISTRY
                + downgraded_rule("ex.layers.ratcheted", 912),
                "rules/baselines/ex.layers.ratcheted.txt": "pkg/routes.py\n",
            },
            render=False,
        ) as repo:
            self.assertIn(
                "[registry] schema: a [rule.downgrade] names an issue, but the "
                "registry has no issue_repository. Fix: set issue_repository = "
                '"<owner>/<name>" at the top of rules/registry.toml.',
                repo.messages(),
            )

    def test_an_enforced_rule_has_no_downgrade(self) -> None:
        enforced = downgraded_rule("ex.layers.restored", 912).replace(
            'status = "ratcheted"', 'status = "enforced"'
        )
        enforced = enforced.split("[rule.baseline]")[0]
        with Repo(
            {"rules/registry.toml": REGISTRY_WITH_ISSUES + enforced}, render=False
        ) as repo:
            self.assertIn(
                "[ex.layers.restored] schema: an enforced rule has no downgrade. "
                "Fix: remove [rule.downgrade] once the rule is enforced again; "
                "the issue it names can then be closed.",
                repo.messages(),
            )


if __name__ == "__main__":
    unittest.main()
