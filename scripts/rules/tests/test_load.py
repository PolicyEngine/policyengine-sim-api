"""Tests for loading rule tables: every schema problem is reported, in order."""

from __future__ import annotations

import tomllib
import unittest
from datetime import date

from scripts.rules.load import parse
from scripts.rules.model import (
    Baseline,
    CiRef,
    Downgrade,
    Enforcer,
    Kind,
    Proof,
    Rule,
    RuleException,
    Status,
    Tier,
)
from scripts.rules.tests.support import REGISTRY

BOUNDARY_NODE = "tests/unit/test_boundaries.py::test_routes_have_no_sql"
KNOWN_RULE_KEYS = (
    "['autofix', 'baseline', 'category', 'ci', 'downgrade', 'enforcer', "
    "'exception', 'id', 'proof', 'rationale', 'review_by', 'scope', 'statement', "
    "'status', 'tier']"
)
KINDS = (
    "['pytest', 'quality-guard', 'ruff', 'mypy', 'pyright', 'import-linter', "
    "'workflow-job', 'docs-guard', 'llm-review', 'none']"
)
MALFORMED = "[ex.layers.malformed] schema: ex.layers.malformed"
DOWNGRADE_ISSUE_FIX = (
    "write issue = <number of the open issue that tracks restoring the rule>"
)

MALFORMED_RULE = {
    "id": "ex.layers.malformed",
    "category": "layers",
    "statement": "Malformed.",
    "rationale": "docs/guide.md",
    "scope": ["pkg/*.py"],
    "tier": "mechanical",
    "status": "enforced",
    "autofix": "yes",
    "review_by": "someday",
    "enforcer": "pytest",
    "ci": [
        5,
        {
            "workflow": ".github/workflows/pr.yml",
            "job": "",
            "invocation": "make test",
            "extra": 1,
        },
    ],
    "proof": "proof.py",
    "baseline": {"file": "rules/baselines/x.txt"},
    "exception": [
        7,
        {"path": "pkg/*.py", "reason": "r", "expires": "soon", "extra": 1},
    ],
    "downgrade": {"why": "x"},
    "bogus": 1,
}
UNKNOWN_TIER_RULE = {
    "category": "layers",
    "tier": "unknown",
    "status": "enforced",
    "autofix": False,
    "enforcer": {"kind": "flake8", "codes": ["E1"]},
}
SINGLE_TABLES_RULE = {
    "id": "ex.layers.single-tables",
    "category": "layers",
    "statement": "Single tables.",
    "rationale": "docs/guide.md",
    "scope": ["pkg/*.py"],
    "tier": "metric",
    "status": "ratcheted",
    "autofix": True,
    "review_by": date(2026, 7, 1),
    "enforcer": {"kind": "pytest", "nodes": [BOUNDARY_NODE]},
    "ci": {
        "workflow": ".github/workflows/pr.yml",
        "job": "test",
        "invocation": "make test",
    },
    "proof": {"test": BOUNDARY_NODE},
    "baseline": {"file": "rules/baselines/x.txt", "count": 2, "extra": True},
    "exception": {"path": "pkg/*.py", "reason": "legacy", "expires": "2026-12-31"},
    "downgrade": {"reason": "too noisy", "issue": 912},
}


class RuleTableTest(unittest.TestCase):
    def setUp(self) -> None:
        raw = tomllib.loads(REGISTRY)
        raw["rule"] = [
            "not a table",
            MALFORMED_RULE,
            UNKNOWN_TIER_RULE,
            SINGLE_TABLES_RULE,
        ]
        self.registry, self.violations = parse(raw)

    def test_every_problem_is_reported_in_order(self) -> None:
        self.assertEqual(
            [str(violation) for violation in self.violations],
            [
                "[registry] schema: rule #1 must be a table. Fix: write [[rule]].",
                "[ex.layers.malformed] schema: unknown key 'bogus' in "
                "ex.layers.malformed. Fix: remove it or use one of "
                f"{KNOWN_RULE_KEYS}.",
                f"{MALFORMED}.enforcer must be a table. Fix: add [rule.enforcer].",
                f"{MALFORMED}.ci entries must be tables. Fix: write [[rule.ci]].",
                "[ex.layers.malformed] schema: unknown key 'extra' in "
                "ex.layers.malformed.ci. Fix: remove it or use one of "
                "['invocation', 'job', 'workflow'].",
                f"{MALFORMED}.ci.job must be a non-empty string. Fix: set job.",
                f"{MALFORMED}.proof must be a table. Fix: write [rule.proof].",
                f"{MALFORMED}.baseline needs 'file' and an integer 'count'. "
                "Fix: write [rule.baseline] with file and count.",
                f"{MALFORMED}.exception entries must be tables. "
                "Fix: write [[rule.exception]].",
                "[ex.layers.malformed] schema: unknown key 'extra' in "
                "ex.layers.malformed.exception. Fix: remove it or use one of "
                "['category', 'expires', 'issue', 'match', 'path', 'reason', "
                "'removal'].",
                f"{MALFORMED}.exception.expires must be a date. "
                "Fix: write it as YYYY-MM-DD.",
                "[ex.layers.malformed] schema: unknown key 'why' in "
                "ex.layers.malformed.downgrade. Fix: remove it or use one of "
                "['issue', 'reason'].",
                f"{MALFORMED}.downgrade.reason must be a non-empty string. "
                "Fix: set reason.",
                f"{MALFORMED}.downgrade.issue must be a positive issue number. "
                f"Fix: {DOWNGRADE_ISSUE_FIX}.",
                f"{MALFORMED}.autofix must be true or false. Fix: set autofix.",
                f"{MALFORMED}.review_by must be a date. Fix: write it as YYYY-MM-DD.",
                f"[registry] schema: rule #3.enforcer.kind is 'flake8'. "
                f"Fix: use one of {KINDS}.",
                "[registry] schema: rule #3.tier is 'unknown'. "
                "Fix: use one of ['mechanical', 'metric', 'judgment'].",
                "[ex.layers.single-tables] schema: unknown key 'extra' in "
                "ex.layers.single-tables.baseline. Fix: remove it or use one of "
                "['count', 'file'].",
            ],
        )

    def test_malformed_fields_fall_back_to_safe_values(self) -> None:
        assert self.registry is not None
        self.assertEqual(
            self.registry.rule("ex.layers.malformed"),
            Rule(
                id="ex.layers.malformed",
                category="layers",
                statement="Malformed.",
                rationale="docs/guide.md",
                scope=("pkg/*.py",),
                tier=Tier.MECHANICAL,
                status=Status.ENFORCED,
                autofix=False,
                enforcer=Enforcer(Kind.NONE),
                ci=(CiRef(".github/workflows/pr.yml", "", "make test"),),
                exceptions=(RuleException("pkg/*.py", "r", date.max),),
            ),
        )

    def test_a_rule_with_an_unknown_tier_is_dropped(self) -> None:
        assert self.registry is not None
        self.assertEqual(
            [rule.id for rule in self.registry.rules],
            ["ex.layers.malformed", "ex.layers.single-tables"],
        )

    def test_single_tables_stand_for_one_entry_lists(self) -> None:
        assert self.registry is not None
        self.assertEqual(
            self.registry.rule("ex.layers.single-tables"),
            Rule(
                id="ex.layers.single-tables",
                category="layers",
                statement="Single tables.",
                rationale="docs/guide.md",
                scope=("pkg/*.py",),
                tier=Tier.METRIC,
                status=Status.RATCHETED,
                autofix=True,
                enforcer=Enforcer(Kind.PYTEST, {"nodes": [BOUNDARY_NODE]}),
                ci=(CiRef(".github/workflows/pr.yml", "test", "make test"),),
                review_by=date(2026, 7, 1),
                proof=Proof({"test": BOUNDARY_NODE}),
                baseline=Baseline("rules/baselines/x.txt", 2),
                exceptions=(RuleException("pkg/*.py", "legacy", date(2026, 12, 31)),),
                downgrade=Downgrade("too noisy", 912),
            ),
        )


class ExceptionIssueTest(unittest.TestCase):
    def test_an_exception_issue_must_be_a_positive_whole_number(self) -> None:
        for issue in (0, -5, "5", True, 5.0):
            with self.subTest(issue=issue):
                raw = tomllib.loads(REGISTRY)
                raw["rule"] = [
                    {
                        **SINGLE_TABLES_RULE,
                        "exception": {
                            "path": "pkg/*.py",
                            "reason": "legacy",
                            "issue": issue,
                        },
                    }
                ]

                registry, violations = parse(raw)

                self.assertIn(
                    "[ex.layers.single-tables] schema: ex.layers.single-tables"
                    ".exception.issue must be a positive issue number. "
                    "Fix: write issue = <number>.",
                    [str(violation) for violation in violations],
                )
                assert registry is not None
                rule = registry.rule("ex.layers.single-tables")
                assert rule is not None
                self.assertIsNone(rule.exceptions[0].issue)


class DowngradeTest(unittest.TestCase):
    """A [rule.downgrade] says why the rule is weaker and which issue restores it."""

    def downgrade_problems(self, downgrade: object) -> tuple[Rule | None, list[str]]:
        """Parse SINGLE_TABLES_RULE with ``downgrade``; return it and the problems."""
        raw = tomllib.loads(REGISTRY)
        raw["rule"] = [{**SINGLE_TABLES_RULE, "downgrade": downgrade}]
        registry, violations = parse(raw)
        assert registry is not None
        problems = [
            str(violation)
            for violation in violations
            if "downgrade" in violation.message
        ]
        return registry.rule("ex.layers.single-tables"), problems

    def test_a_reason_and_an_issue_number_make_a_downgrade(self) -> None:
        rule, problems = self.downgrade_problems({"reason": "noisy", "issue": 1})

        self.assertEqual(problems, [])
        assert rule is not None
        self.assertEqual(rule.downgrade, Downgrade("noisy", 1))

    def test_the_issue_must_be_a_positive_whole_number(self) -> None:
        for issue in (None, 0, -912, "912", "#912", 912.0, True):
            with self.subTest(issue=issue):
                downgrade = {"reason": "noisy"}
                if issue is not None:
                    downgrade["issue"] = issue

                rule, problems = self.downgrade_problems(downgrade)

                self.assertEqual(
                    problems,
                    [
                        "[ex.layers.single-tables] schema: ex.layers.single-tables"
                        ".downgrade.issue must be a positive issue number. "
                        f"Fix: {DOWNGRADE_ISSUE_FIX}."
                    ],
                )
                assert rule is not None
                self.assertIsNone(rule.downgrade)

    def test_the_reason_must_not_be_empty(self) -> None:
        rule, problems = self.downgrade_problems({"reason": " ", "issue": 912})

        self.assertEqual(
            problems,
            [
                "[ex.layers.single-tables] schema: ex.layers.single-tables"
                ".downgrade.reason must be a non-empty string. Fix: set reason."
            ],
        )
        assert rule is not None
        self.assertIsNone(rule.downgrade)

    def test_a_downgrade_must_be_a_table(self) -> None:
        rule, problems = self.downgrade_problems("too noisy")

        self.assertEqual(
            problems,
            [
                "[ex.layers.single-tables] schema: ex.layers.single-tables"
                ".downgrade must be a table. Fix: write [rule.downgrade] with "
                'reason = "..." and issue = <number>.'
            ],
        )
        assert rule is not None
        self.assertIsNone(rule.downgrade)


if __name__ == "__main__":
    unittest.main()
