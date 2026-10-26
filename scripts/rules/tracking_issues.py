"""Check that each ``[rule.downgrade]`` names an open issue.

The issue tracks restoring the rule, and lives in the registry's
``issue_repository``. The lookup is the PR description check's
(``.github/scripts/check_pr_description.py``): it runs ``gh api``, which reads its
token from ``GH_TOKEN`` or ``GITHUB_TOKEN``.

On GitHub Actions the check never passes without an answer from GitHub: a
missing token, or a lookup that fails, is a violation. Elsewhere, without a
token, it notes each issue it didn't check, and CI checks them.

A pull request can change ``issue_repository``, but not the repository its
workflow runs in, which the runner sets in ``GITHUB_REPOSITORY``. When that is
set, the two must match, so a pull request can't name an issue elsewhere.
"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
from typing import TYPE_CHECKING

from scripts.rules.model import Violation

if TYPE_CHECKING:
    from types import ModuleType

    from scripts.rules.model import Registry, Rule

REPO_ROOT = Path(__file__).resolve().parents[2]
LOOKUP_SCRIPT = REPO_ROOT / ".github" / "scripts" / "check_pr_description.py"
# gh reads its token from the first of these that is set.
TOKEN_VARIABLES = ("GH_TOKEN", "GITHUB_TOKEN")
CHECK = "downgrade"
NEITHER_VARIABLE_SET = "neither GH_TOKEN nor GITHUB_TOKEN is set"
# What the check uses from the lookup script.
LOOKUP_NAMES = ("require_open_issue", "NotAnOpenIssueError", "IssueLookupError")


class LookupUnavailableError(Exception):
    """The issue lookup script could not be loaded."""


def _load_lookup() -> ModuleType:
    """Load the PR description check, whose ``require_open_issue`` is the lookup."""
    spec = importlib.util.spec_from_file_location("check_pr_description", LOOKUP_SCRIPT)
    if spec is None or spec.loader is None:
        msg = "it is not a Python module"
        raise LookupUnavailableError(msg)
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except (ImportError, OSError, SyntaxError) as error:
        raise LookupUnavailableError(str(error)) from error
    missing = [name for name in LOOKUP_NAMES if not hasattr(module, name)]
    if missing:
        msg = f"it doesn't define {', '.join(missing)}"
        raise LookupUnavailableError(msg)
    return module


def _check_issue(
    lookup: ModuleType, repository: str, token_variable: str, rule: Rule, issue: int
) -> tuple[Violation | None, str | None]:
    """Look up one downgrade's issue; return a violation or a note.

    ``token_variable`` is where gh read its token, for the fix of a lookup
    that fails.
    """
    try:
        lookup.require_open_issue(repository, issue)
    except lookup.NotAnOpenIssueError as error:
        fix = (
            f"set issue to the number of an open issue in {repository} that tracks "
            "restoring the rule"
            if error.state is None
            else f"reopen issue #{issue} while the rule stays weakened, or restore "
            "the rule and remove [rule.downgrade]"
        )
        problem = str(error).rstrip(".")
        return Violation(
            rule.id, CHECK, f"issue = {issue} names no open issue: {problem}", fix
        ), None
    except lookup.IssueLookupError as error:
        problem = str(error).rstrip(".")
    except OSError as error:
        problem = f"gh could not run ({error})"
    else:
        return None, f"rule {rule.id}: tracking issue #{issue} is open"
    return Violation(
        rule.id,
        CHECK,
        f"couldn't check that issue #{issue} is open: {problem}",
        f"run the guard again; if it fails again, check the token in "
        f"{token_variable}, which needs read access to the issues of {repository}",
    ), None


def check_tracking_issues(registry: Registry) -> tuple[list[Violation], list[str]]:
    """Check that each downgrade's issue is open; return (violations, notes).

    ``validate`` reports a downgrade in a registry without an
    ``issue_repository``, so this checks nothing then.
    """
    downgraded = [
        (rule, rule.downgrade.issue)
        for rule in registry.rules
        if rule.downgrade is not None
    ]
    repository = registry.issue_repository
    if not downgraded or not repository:
        return [], []
    run_repository = os.environ.get("GITHUB_REPOSITORY")
    if run_repository and run_repository.lower() != repository.lower():
        return [
            Violation(
                None,
                CHECK,
                f"issue_repository is {repository}, but this run is in "
                f"{run_repository}, whose issues each [rule.downgrade] must name",
                f'set issue_repository = "{run_repository}"',
            )
        ], []
    token_variable = next(
        (name for name in TOKEN_VARIABLES if os.environ.get(name)), None
    )
    if token_variable is None:
        if os.environ.get("GITHUB_ACTIONS"):
            return [
                Violation(
                    rule.id,
                    CHECK,
                    f"couldn't check that issue #{issue} is open: "
                    f"{NEITHER_VARIABLE_SET}",
                    "give the step the job's token, with env: GH_TOKEN: "
                    "${{ github.token }} and permissions: issues: read",
                )
                for rule, issue in downgraded
            ], []
        return [], [
            f"rule {rule.id}: tracking issue #{issue} was not checked, because "
            f"{NEITHER_VARIABLE_SET}. CI checks it; to check it here, set one, "
            'for example GH_TOKEN="$(gh auth token)"'
            for rule, issue in downgraded
        ]
    try:
        lookup = _load_lookup()
    except LookupUnavailableError as error:
        return [
            Violation(
                None,
                CHECK,
                f"couldn't load the issue lookup from {LOOKUP_SCRIPT}: {error}",
                "restore .github/scripts/check_pr_description.py, whose "
                "require_open_issue looks issues up",
            )
        ], []
    violations: list[Violation] = []
    notes: list[str] = []
    for rule, issue in downgraded:
        violation, note = _check_issue(lookup, repository, token_variable, rule, issue)
        if violation is not None:
            violations.append(violation)
        if note is not None:
            notes.append(note)
    return violations, notes
