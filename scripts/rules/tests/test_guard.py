"""Tests for the rules registry guard, using throwaway repositories."""

from __future__ import annotations

import subprocess
import unittest
from datetime import date
from typing import TYPE_CHECKING, ClassVar

from scripts.rules.baseline import check_baselines, check_enforced_packages
from scripts.rules.load import load_text
from scripts.rules.render import render_agents_block, render_rules_md
from scripts.rules.tests.support import REGISTRY, Repo

if TYPE_CHECKING:
    from scripts.rules.model import Registry

RATCHETED_RULE = """
[[rule]]
id = "ex.layers.ratcheted"
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
[rule.baseline]
file = "rules/baselines/ratcheted.txt"
count = {count}
"""


def with_rule(extra: str) -> dict[str, str]:
    return {"rules/registry.toml": REGISTRY + extra}


def per_file_ignore_rule(name: str, pattern: str) -> str:
    """Return a registry rule describing a ruff per-file ignore of E501."""
    return f"""
[[rule]]
id = "ex.quality.{name}-ignores"
category = "quality"
statement = "Long lines are allowed here."
rationale = "docs/guide.md"
scope = ["rules/registry.toml"]
tier = "mechanical"
status = "advisory"
autofix = false
[rule.enforcer]
kind = "ruff"
mode = "per-file-ignore"
pattern = "{pattern}"
codes = ["E501"]
"""


def per_file_ignore_config(pattern: str) -> str:
    """Return a pyproject.toml whose ruff config ignores E501 for ``pattern``."""
    return (
        '[tool.mypy]\nfiles = ["pkg"]\n\n'
        f'[tool.ruff.lint.per-file-ignores]\n"{pattern}" = ["E501"]\n'
    )


class ValidRepositoryTest(unittest.TestCase):
    def test_valid_repository_passes(self) -> None:
        with Repo() as repo:
            self.assertEqual(repo.messages(), [])

    def test_render_is_deterministic(self) -> None:
        with Repo() as repo:
            registry, _ = load_text(repo.read("rules/registry.toml"))
            assert registry is not None
            self.assertEqual(render_rules_md(registry), render_rules_md(registry))
            self.assertEqual(
                render_agents_block(registry), render_agents_block(registry)
            )

    def test_rules_md_lists_gaps(self) -> None:
        with Repo() as repo:
            rules_md = repo.read("docs/generated/rules.md")
            self.assertIn(
                "ex.layers.connectors-never-commit", rules_md.split("## Gaps")[1]
            )
            self.assertIn("```mermaid", rules_md)
            self.assertIn("## Rules", repo.read("AGENTS.md"))


class SchemaTest(unittest.TestCase):
    def assert_violation(self, repo: Repo, fragment: str) -> None:
        messages = repo.messages()
        self.assertTrue(any(fragment in m for m in messages), messages)

    def test_missing_required_field(self) -> None:
        with Repo(render=False) as repo:
            repo.edit(
                "rules/registry.toml", 'statement = "Routes never issue SQL."\n', ""
            )
            self.assert_violation(repo, "statement must be a non-empty string")

    def test_unknown_enforcer_kind(self) -> None:
        with Repo(render=False) as repo:
            repo.edit("rules/registry.toml", 'kind = "mypy"', 'kind = "flake8"')
            self.assert_violation(repo, "enforcer.kind is 'flake8'")

    def test_bad_status(self) -> None:
        with Repo(render=False) as repo:
            repo.edit(
                "rules/registry.toml", 'status = "doc-only"', 'status = "optional"'
            )
            self.assert_violation(repo, "status is 'optional'")

    def test_unknown_key(self) -> None:
        with Repo(render=False) as repo:
            repo.edit(
                "rules/registry.toml",
                "autofix = true",
                'autofix = true\nowner = "someone"',
            )
            self.assert_violation(repo, "unknown key 'owner'")

    def test_duplicate_rule_id(self) -> None:
        with Repo(render=False) as repo:
            repo.edit(
                "rules/registry.toml",
                'id = "ex.quality.types"',
                'id = "ex.quality.format"',
            )
            self.assert_violation(repo, "duplicate rule id")

    def test_scope_glob_must_match(self) -> None:
        with Repo(render=False) as repo:
            repo.edit(
                "rules/registry.toml",
                'scope = ["rules/registry.toml"]',
                'scope = ["nowhere/*.py"]',
            )
            self.assert_violation(repo, "scope 'nowhere/*.py' matches no files")

    def test_doc_only_rule_cannot_claim_enforcer(self) -> None:
        with Repo(render=False) as repo:
            repo.edit(
                "rules/registry.toml",
                'status = "enforced"\nautofix = true',
                'status = "doc-only"\nautofix = true',
            )
            self.assert_violation(repo, "doc-only rules have no enforcer")

    def test_enforced_rule_needs_ci(self) -> None:
        extra = """
[[rule]]
id = "ex.layers.local-only"
category = "layers"
statement = "Only checked locally."
rationale = "docs/guide.md"
scope = ["pkg/*.py"]
tier = "mechanical"
status = "enforced"
autofix = false
[rule.enforcer]
kind = "pytest"
nodes = ["tests/unit/test_boundaries.py::test_routes_have_no_sql"]
"""
        with Repo(with_rule(extra), render=False) as repo:
            self.assert_violation(repo, "enforced rules must run in CI")

    def test_ruff_is_never_ratcheted(self) -> None:
        with Repo(render=False) as repo:
            repo.write("rules/baselines/ruff.txt", "pkg/routes.py E501\n")
            repo.edit(
                "rules/registry.toml",
                'kind = "ruff"\nmode = "format"',
                'kind = "ruff"\nmode = "format"\n[rule.baseline]\n'
                'file = "rules/baselines/ruff.txt"\ncount = 1',
            )
            self.assert_violation(repo, "ruff is never ratcheted")

    def test_ratcheted_without_baseline(self) -> None:
        with Repo(render=False) as repo:
            repo.edit(
                "rules/registry.toml",
                'status = "enforced"\nautofix = false\n[rule.enforcer]\nkind = "mypy"',
                'status = "ratcheted"\nautofix = false\n[rule.enforcer]\nkind = "mypy"',
            )
            self.assert_violation(repo, "ratcheted rules need a baseline")

    def test_baseline_count_must_match_file(self) -> None:
        with Repo(with_rule(RATCHETED_RULE.format(count=3)), render=False) as repo:
            repo.write(
                "rules/baselines/ratcheted.txt",
                "# tolerated\npkg/routes.py\npkg/other.py\n",
            )
            self.assert_violation(
                repo, "baseline count is 3 but rules/baselines/ratcheted.txt lists 2"
            )

    def test_judgment_rules_need_review_enforcer(self) -> None:
        with Repo(render=False) as repo:
            repo.edit(
                "rules/registry.toml",
                'tier = "mechanical"\nstatus = "enforced"\nautofix = true',
                'tier = "judgment"\nstatus = "enforced"\nautofix = true',
            )
            self.assert_violation(repo, "judgment rules are enforced by review")


class ResolutionTest(unittest.TestCase):
    def assert_violation(self, repo: Repo, fragment: str) -> None:
        messages = repo.messages()
        self.assertTrue(any(fragment in m for m in messages), messages)

    def test_unresolved_pytest_node(self) -> None:
        with Repo() as repo:
            repo.edit(
                "tests/unit/test_boundaries.py",
                "def test_routes_have_no_sql",
                "def test_renamed",
            )
            self.assert_violation(repo, "has no test 'test_routes_have_no_sql'")

    def test_parametrized_node_resolves(self) -> None:
        with Repo() as repo:
            repo.edit(
                "rules/registry.toml",
                'test_boundaries.py::test_routes_have_no_sql"',
                'test_boundaries.py::test_routes_have_no_sql[a]"',
            )
            repo.render()
            self.assertEqual(repo.messages(), [])

    def test_test_outside_make_target_is_not_collected(self) -> None:
        with Repo() as repo:
            repo.edit("Makefile", "tests/unit", "tests/contract")
            self.assert_violation(repo, "is not collected by 'make test'")

    def test_project_must_be_in_matrix(self) -> None:
        workflow = """
jobs:
  test:
    strategy:
      matrix:
        project:
          - projects/alpha
    steps:
      - run: |
          cd ${{ matrix.project }}
          uv run pytest tests/
"""
        extra = """
[[rule]]
id = "ex.layers.beta"
category = "layers"
statement = "Beta rule."
rationale = "docs/guide.md"
scope = ["projects/beta/src/*.py"]
tier = "mechanical"
status = "enforced"
autofix = false
[rule.enforcer]
kind = "pytest"
project = "projects/beta"
nodes = ["tests/test_beta.py::test_beta"]
[[rule.ci]]
workflow = ".github/workflows/matrix.yml"
job = "test"
invocation = "pytest tests/"
"""
        files = {
            ".github/workflows/matrix.yml": workflow,
            "projects/beta/tests/test_beta.py": "def test_beta():\n    pass\n",
            "projects/beta/src/beta.py": "",
            **with_rule(extra),
        }
        with Repo(files) as repo:
            self.assert_violation(
                repo, "project projects/beta is not in the matrix of job 'test'"
            )

    def test_missing_job(self) -> None:
        with Repo() as repo:
            repo.edit("rules/registry.toml", 'job = "typecheck"', 'job = "types"')
            repo.render()
            self.assert_violation(repo, "no job 'types'")

    def test_invocation_only_in_comment_does_not_count(self) -> None:
        with Repo() as repo:
            repo.edit(".github/workflows/pr.yml", "run: make test", "run: echo skipped")
            self.assert_violation(repo, "does not run 'make test'")

    def test_unknown_quality_guard(self) -> None:
        with Repo() as repo:
            repo.edit(
                "rules/registry.toml", 'name = "rules-registry"', 'name = "renamed"'
            )
            repo.render()
            self.assert_violation(repo, "no guard named 'renamed'")

    def test_mypy_path_must_be_configured(self) -> None:
        with Repo() as repo:
            repo.edit(
                "rules/registry.toml", 'paths = ["pkg"]', 'paths = ["pkg", "other"]'
            )
            repo.render()
            self.assert_violation(repo, "['other'] not in [tool.mypy].files")

    def test_ruff_lint_codes_must_be_selected(self) -> None:
        extra = """
[[rule]]
id = "ex.quality.bugbear"
category = "quality"
statement = "No bugbear findings."
rationale = "docs/guide.md"
scope = ["pkg/*.py"]
tier = "mechanical"
status = "advisory"
autofix = false
[rule.enforcer]
kind = "ruff"
mode = "lint"
codes = ["B"]
"""
        with Repo(with_rule(extra)) as repo:
            self.assert_violation(repo, "ruff does not select ['B']")

    def test_proof_files_must_exist(self) -> None:
        with Repo(render=False) as repo:
            repo.edit(
                "rules/registry.toml",
                'kind = "mypy"\npaths = ["pkg"]',
                'kind = "mypy"\npaths = ["pkg"]\n[rule.proof]\n'
                'bad_fixture = "rules/fixtures/bad.py"\n'
                'good_fixture = "rules/fixtures/good.py"\n'
                'test = "tests/unit/test_boundaries.py::test_routes_have_no_sql"',
            )
            self.assert_violation(
                repo, "proof bad_fixture rules/fixtures/bad.py does not exist"
            )


class ProofCiTest(unittest.TestCase):
    PROOF_FILES: ClassVar[dict[str, str]] = {
        "rules/fixtures/bad.py": "import sqlalchemy\n",
        "rules/fixtures/good.py": "x = 1\n",
    }

    def with_proof(self, repo: Repo, rule_marker: str, proof: str) -> None:
        repo.edit(
            "rules/registry.toml", rule_marker, rule_marker + "\n[rule.proof]\n" + proof
        )
        repo.render()

    def test_pytest_rule_proof_runs_in_its_ci_job(self) -> None:
        files = {
            **self.PROOF_FILES,
            "tests/unit/test_proof.py": "def test_rule_proof_routes():\n    pass\n",
        }
        with Repo(files) as repo:
            self.with_proof(
                repo,
                'nodes = ["tests/unit/test_boundaries.py::test_routes_have_no_sql"]',
                'bad_fixture = "rules/fixtures/bad.py"\n'
                'good_fixture = "rules/fixtures/good.py"\n'
                'test = "tests/unit/test_proof.py::test_rule_proof_routes"',
            )
            self.assertEqual(repo.messages(), [])

    def test_proof_outside_the_ci_command_is_rejected(self) -> None:
        files = {
            **self.PROOF_FILES,
            "tests/contract/test_proof.py": "def test_rule_proof_routes():\n    pass\n",
        }
        with Repo(files) as repo:
            self.with_proof(
                repo,
                'nodes = ["tests/unit/test_boundaries.py::test_routes_have_no_sql"]',
                'bad_fixture = "rules/fixtures/bad.py"\n'
                'good_fixture = "rules/fixtures/good.py"\n'
                'test = "tests/contract/test_proof.py::test_rule_proof_routes"',
            )
            messages = "\n".join(repo.messages())
            self.assertIn(
                "proof: tests/contract/test_proof.py is not collected by 'make test'",
                messages,
            )

    def test_non_pytest_rule_proof_must_name_its_job(self) -> None:
        files = {
            **self.PROOF_FILES,
            "tests/unit/test_proof.py": "def test_rule_proof_types():\n    pass\n",
        }
        with Repo(files) as repo:
            self.with_proof(
                repo,
                'paths = ["pkg"]',
                'bad_fixture = "rules/fixtures/bad.py"\n'
                'good_fixture = "rules/fixtures/good.py"\n'
                'test = "tests/unit/test_proof.py::test_rule_proof_types"',
            )
            self.assertIn(
                "the proof test is not tied to a CI job", "\n".join(repo.messages())
            )

    def test_proof_with_named_unittest_job(self) -> None:
        workflow_job = (
            "  tool-tests:\n    steps:\n"
            "      - run: python -m unittest discover -s tools/tests -t .\n"
        )
        files = {
            **self.PROOF_FILES,
            "tools/tests/test_proof.py": (
                "class TestProof:\n    def test_rule_proof_types(self):\n        pass\n"
            ),
        }
        with Repo(files, render=False) as repo:
            repo.write(
                ".github/workflows/pr.yml",
                repo.read(".github/workflows/pr.yml") + workflow_job,
            )
            repo.edit(
                "rules/registry.toml",
                "[discovery]\n",
                "[discovery]\n"
                'ignore_jobs = [{ job = "tool-tests", reason = "runs tool tests" }]\n',
            )
            proof = (
                'bad_fixture = "rules/fixtures/bad.py"\n'
                'good_fixture = "rules/fixtures/good.py"\n'
                'test = "tools/tests/test_proof.py::TestProof::test_rule_proof_types"\n'
                'workflow = ".github/workflows/pr.yml"\njob = "tool-tests"\n'
                'invocation = "unittest discover -s tools/tests"'
            )
            self.with_proof(repo, 'paths = ["pkg"]', proof)
            self.assertEqual(repo.messages(), [])

            repo.edit("rules/registry.toml", 'job = "tool-tests"\n', "")
            repo.render()
            self.assertIn(
                "proof names its CI job only partly", "\n".join(repo.messages())
            )


PYRIGHT_WORKFLOW = """
jobs:
  pyright:
    strategy:
      matrix:
        package:
          - libs/alpha
          - projects/beta
    steps:
      - run: python -m scripts.rules pyright "$PACKAGE"
"""

PYRIGHT_RULE = """
[[rule]]
id = "ex.quality.pyright"
category = "quality"
statement = "The packages have no Pyright errors."
rationale = "docs/guide.md"
scope = ["libs/alpha/src/*.py", "projects/beta/src/*.py"]
tier = "mechanical"
status = "enforced"
autofix = false
[rule.enforcer]
kind = "pyright"
projects = ["libs/alpha", "projects/beta"]
[[rule.ci]]
workflow = ".github/workflows/pyright.yml"
job = "pyright"
invocation = "scripts.rules pyright"
"""

PYRIGHT_SETTINGS = '[project]\nname = "package"\n\n[tool.pyright]\ninclude = ["src"]\n'


def pyright_files() -> dict[str, str]:
    """Return a repository whose Pyright rule checks two packages in a matrix job."""
    return {
        ".github/workflows/pyright.yml": PYRIGHT_WORKFLOW,
        "libs/alpha/pyproject.toml": PYRIGHT_SETTINGS,
        "libs/alpha/src/alpha.py": "",
        "projects/beta/pyproject.toml": PYRIGHT_SETTINGS,
        "projects/beta/src/beta.py": "",
        **with_rule(PYRIGHT_RULE),
    }


class PyrightRuleTest(unittest.TestCase):
    """A Pyright rule names the packages it checks; the guard holds CI to them."""

    def assert_violation(self, repo: Repo, fragment: str) -> None:
        messages = repo.messages()
        self.assertTrue(any(fragment in m for m in messages), messages)

    def test_packages_that_declare_their_checks_in_the_matrix_pass(self) -> None:
        with Repo(pyright_files()) as repo:
            self.assertEqual(repo.messages(), [])

    def test_rule_must_name_its_packages(self) -> None:
        with Repo(pyright_files(), render=False) as repo:
            repo.edit(
                "rules/registry.toml",
                'projects = ["libs/alpha", "projects/beta"]',
                "projects = []",
            )
            self.assert_violation(repo, "pyright enforcer names no projects")

    def test_package_must_exist(self) -> None:
        with Repo(pyright_files(), render=False) as repo:
            repo.edit("rules/registry.toml", '"projects/beta"]', '"projects/gamma"]')
            self.assert_violation(repo, "project projects/gamma does not exist")

    def test_package_must_declare_what_pyright_checks(self) -> None:
        with Repo(pyright_files(), render=False) as repo:
            repo.write("libs/alpha/pyproject.toml", '[project]\nname = "alpha"\n')
            self.assert_violation(
                repo, "libs/alpha/pyproject.toml has no [tool.pyright] include"
            )

    def test_package_must_be_in_the_job_matrix(self) -> None:
        with Repo(pyright_files(), render=False) as repo:
            repo.edit(
                ".github/workflows/pyright.yml", "          - projects/beta\n", ""
            )
            self.assert_violation(
                repo, "project projects/beta is not in the matrix of job 'pyright'"
            )

    def test_package_may_belong_to_only_one_pyright_rule(self) -> None:
        second_rule = PYRIGHT_RULE.replace(
            'id = "ex.quality.pyright"', 'id = "ex.quality.pyright-again"'
        ).replace(
            'projects = ["libs/alpha", "projects/beta"]', 'projects = ["libs/alpha"]'
        )
        with Repo(pyright_files(), render=False) as repo:
            repo.write(
                "rules/registry.toml", repo.read("rules/registry.toml") + second_rule
            )
            self.assert_violation(
                repo,
                "[ex.quality.pyright-again] schema: project libs/alpha is also listed "
                "by ex.quality.pyright",
            )

    def test_a_package_written_differently_is_still_the_same_package(self) -> None:
        second_rule = PYRIGHT_RULE.replace(
            'id = "ex.quality.pyright"', 'id = "ex.quality.pyright-again"'
        ).replace(
            'projects = ["libs/alpha", "projects/beta"]', 'projects = ["./libs/alpha"]'
        )
        with Repo(pyright_files(), render=False) as repo:
            repo.write(
                "rules/registry.toml", repo.read("rules/registry.toml") + second_rule
            )
            self.assert_violation(
                repo,
                "[ex.quality.pyright-again] schema: project libs/alpha is also listed "
                "by ex.quality.pyright",
            )

    def test_package_may_not_have_a_pyrightconfig_json(self) -> None:
        with Repo(pyright_files(), render=False) as repo:
            repo.write(
                "projects/beta/pyrightconfig.json", '{"typeCheckingMode": "off"}'
            )
            self.assert_violation(
                repo,
                "projects/beta has a pyrightconfig.json, which Pyright reads instead "
                "of its [tool.pyright]",
            )


def pyright_rule(
    rule_id: str, packages: list[str], *, status: str = "enforced", tail: str = ""
) -> str:
    """Return a Pyright rule over ``packages``; ``tail`` adds tables to its end."""
    listed = ", ".join(f'"{package}"' for package in packages)
    baseline = (
        f'[rule.baseline]\nfile = "rules/baselines/{rule_id}.txt"\ncount = 0\n'
        if status == "ratcheted"
        else ""
    )
    return (
        PYRIGHT_RULE.replace('id = "ex.quality.pyright"', f'id = "{rule_id}"')
        .replace('status = "enforced"', f'status = "{status}"')
        .replace('projects = ["libs/alpha", "projects/beta"]', f"projects = [{listed}]")
        + baseline
        + tail
    )


DOWNGRADE_SECTION = '[rule.downgrade]\nreason = "a Pyright upgrade"\nissue = 912\n'
PERSON_DOWNGRADE_FIX = (
    'only a person may weaken a rule, by adding [rule.downgrade] with reason = "..." '
    "and issue = <number of the open issue that tracks restoring the rule>."
)


class EnforcedPackageTest(unittest.TestCase):
    """A package an enforced Pyright rule lists stays in an enforced Pyright rule."""

    BASE_RULES = pyright_rule("ex.quality.pyright", ["libs/alpha", "projects/beta"])

    def setUp(self) -> None:
        repo = Repo(pyright_files(), render=False)
        self.addCleanup(repo.__exit__)
        self.repo = repo

    def compare(self, head_rules: str) -> tuple[list[str], list[str]]:
        """Compare ``head_rules`` with BASE_RULES; return the violations and notes."""
        base, _ = load_text(REGISTRY + self.BASE_RULES)
        head, _ = load_text(REGISTRY + head_rules)
        assert base is not None and head is not None
        violations, notes = check_enforced_packages(head, base, self.repo.root)
        return [str(violation) for violation in violations], notes

    def test_a_package_may_move_to_another_enforced_rule(self) -> None:
        head_rules = pyright_rule("ex.quality.pyright", ["libs/alpha"]) + pyright_rule(
            "ex.quality.pyright-beta", ["projects/beta"]
        )

        self.assertEqual(self.compare(head_rules), ([], []))

    def test_a_package_moved_to_a_rule_that_is_not_enforced_is_a_downgrade(
        self,
    ) -> None:
        for status in ("ratcheted", "advisory"):
            with self.subTest(status=status):
                head_rules = pyright_rule(
                    "ex.quality.pyright", ["libs/alpha"]
                ) + pyright_rule(
                    "ex.quality.pyright-beta", ["projects/beta"], status=status
                )

                violations, _ = self.compare(head_rules)

                self.assertEqual(
                    violations,
                    [
                        "[ex.quality.pyright-beta] baseline: package projects/beta "
                        "moved from enforced rule ex.quality.pyright to "
                        f"{status} rule ex.quality.pyright-beta. Fix: list "
                        "projects/beta in an enforced Pyright rule again; "
                        f"{PERSON_DOWNGRADE_FIX}"
                    ],
                )

    def test_a_downgrade_on_the_rule_that_lists_the_package_now_allows_it(
        self,
    ) -> None:
        head_rules = pyright_rule("ex.quality.pyright", ["libs/alpha"]) + pyright_rule(
            "ex.quality.pyright-beta",
            ["projects/beta"],
            status="ratcheted",
            tail=DOWNGRADE_SECTION,
        )

        self.assertEqual(
            self.compare(head_rules),
            (
                [],
                [
                    "package projects/beta moved from enforced rule "
                    "ex.quality.pyright to ratcheted rule ex.quality.pyright-beta: "
                    "a Pyright upgrade (issue #912)"
                ],
            ),
        )

    def test_a_downgrade_on_the_rule_the_package_left_does_not_allow_it(
        self,
    ) -> None:
        head_rules = pyright_rule(
            "ex.quality.pyright", ["libs/alpha"], tail=DOWNGRADE_SECTION
        ) + pyright_rule(
            "ex.quality.pyright-beta", ["projects/beta"], status="advisory"
        )

        violations, _ = self.compare(head_rules)

        self.assertEqual(len(violations), 1)
        self.assertIn("package projects/beta moved from enforced rule", violations[0])

    def test_a_package_that_no_rule_lists_must_be_deleted(self) -> None:
        violations, _ = self.compare(pyright_rule("ex.quality.pyright", ["libs/alpha"]))

        self.assertEqual(
            violations,
            [
                "[ex.quality.pyright] baseline: package projects/beta left enforced "
                "rule ex.quality.pyright, and no Pyright rule lists it now. Fix: "
                "list projects/beta in an enforced Pyright rule again, or delete "
                "the package."
            ],
        )

    def test_a_deleted_package_may_leave(self) -> None:
        (self.repo.root / "projects/beta/pyproject.toml").unlink()

        self.assertEqual(
            self.compare(pyright_rule("ex.quality.pyright", ["libs/alpha"])), ([], [])
        )

    def test_a_package_written_differently_is_still_listed(self) -> None:
        head_rules = pyright_rule("ex.quality.pyright", ["./libs/alpha"]) + (
            pyright_rule("ex.quality.pyright-beta", ["projects/beta/"])
        )

        self.assertEqual(self.compare(head_rules), ([], []))

    def test_a_rule_that_keeps_its_packages_is_left_to_the_status_check(
        self,
    ) -> None:
        head_rules = pyright_rule(
            "ex.quality.pyright", ["libs/alpha", "projects/beta"], status="ratcheted"
        )

        self.assertEqual(self.compare(head_rules), ([], []))

    def test_packages_that_were_not_enforced_on_the_base_are_not_compared(
        self,
    ) -> None:
        self.BASE_RULES = pyright_rule(
            "ex.quality.pyright", ["libs/alpha", "projects/beta"], status="ratcheted"
        )
        head_rules = pyright_rule(
            "ex.quality.pyright", ["libs/alpha"], status="ratcheted"
        ) + pyright_rule(
            "ex.quality.pyright-beta", ["projects/beta"], status="advisory"
        )

        self.assertEqual(self.compare(head_rules), ([], []))

    def test_the_guard_compares_packages_with_the_git_base_ref(self) -> None:
        repo = self.repo
        repo.render()
        git = ["git", "-c", "user.email=t@example.com", "-c", "user.name=t"]
        subprocess.run(["git", "init", "-q"], cwd=repo.root, check=True)
        subprocess.run([*git, "add", "-A"], cwd=repo.root, check=True)
        subprocess.run([*git, "commit", "-qm", "base"], cwd=repo.root, check=True)
        repo.write(
            "rules/registry.toml",
            REGISTRY
            + pyright_rule("ex.quality.pyright", ["libs/alpha"])
            + pyright_rule(
                "ex.quality.pyright-beta", ["projects/beta"], status="advisory"
            ),
        )
        repo.render()

        self.assertIn(
            "[ex.quality.pyright-beta] baseline: package projects/beta moved from "
            "enforced rule ex.quality.pyright to advisory rule ex.quality.pyright-beta",
            "\n".join(repo.messages(base_ref="HEAD")),
        )


class HiddenRuleTest(unittest.TestCase):
    def assert_violation(self, repo: Repo, fragment: str) -> None:
        messages = repo.messages()
        self.assertTrue(any(fragment in m for m in messages), messages)

    def test_unregistered_quality_guard(self) -> None:
        with Repo() as repo:
            repo.write(
                "scripts/run_quality_guards.py",
                'GUARDS = (("rules-registry", lambda: []), '
                '("secret-guard", lambda: []))\n',
            )
            self.assert_violation(repo, "quality guard 'secret-guard'")

    def test_unregistered_guard_test(self) -> None:
        with Repo() as repo:
            text = repo.read("tests/unit/test_boundaries.py")
            repo.write(
                "tests/unit/test_boundaries.py",
                text + "\n\ndef test_new_boundary():\n    pass\n",
            )
            self.assert_violation(
                repo, "test_boundaries.py::test_new_boundary is not referenced"
            )

    def test_unregistered_mypy_file(self) -> None:
        with Repo() as repo:
            repo.write("pyproject.toml", '[tool.mypy]\nfiles = ["pkg", "scripts"]\n')
            self.assert_violation(repo, "mypy checks 'scripts'")

    def test_unregistered_ruff_ignore(self) -> None:
        with Repo() as repo:
            repo.write(
                "pyproject.toml",
                '[tool.mypy]\nfiles = ["pkg"]\n\n'
                '[tool.ruff.lint.per-file-ignores]\n"pkg/*.py" = ["E501"]\n',
            )
            self.assert_violation(repo, "ruff per-file-ignores 'pkg/*.py' has no rule")

    def test_file_level_exceptions_only_for_tests_ci_and_scripts(self) -> None:
        program = 'def main():\n    pass\n\n\nif __name__ == "__main__":\n    main()\n'
        allowed = {
            "tests/**": "tests",
            ".github/**": "ci",
            "scripts/**": "scripts",
            "pkg/cli.py": "cli",
        }
        for pattern, name in allowed.items():
            with (
                self.subTest(pattern=pattern),
                Repo(
                    {
                        "pkg/cli.py": program,
                        ".github/scripts/release.py": "print(1)\n",
                        **with_rule(per_file_ignore_rule(name, pattern)),
                    }
                ) as repo,
            ):
                repo.write("pyproject.toml", per_file_ignore_config(pattern))
                self.assertEqual([m for m in repo.messages() if "per-file" in m], [])

    def test_file_level_exception_outside_allowed_files_is_rejected(self) -> None:
        with Repo(with_rule(per_file_ignore_rule("routes", "pkg/*.py"))) as repo:
            repo.write("pyproject.toml", per_file_ignore_config("pkg/*.py"))
            self.assert_violation(
                repo,
                "covers files that may not take file-level exceptions: pkg/routes.py",
            )

    def test_file_level_exception_needs_a_program_outside_scripts(self) -> None:
        # A module with a main() that is never run as a program isn't a
        # command-line script.
        with Repo(
            {
                "pkg/cli.py": "def main():\n    pass\n",
                **with_rule(per_file_ignore_rule("cli", "pkg/cli.py")),
            }
        ) as repo:
            repo.write("pyproject.toml", per_file_ignore_config("pkg/cli.py"))
            self.assert_violation(
                repo, "may not take file-level exceptions: pkg/cli.py"
            )

    def test_file_level_exception_star_crosses_directories(self) -> None:
        # Ruff's * also matches "/", so "*/routes.py" reaches pkg/routes.py.
        with Repo(with_rule(per_file_ignore_rule("star", "*/routes.py"))) as repo:
            repo.write("pyproject.toml", per_file_ignore_config("*/routes.py"))
            self.assert_violation(
                repo, "may not take file-level exceptions: pkg/routes.py"
            )

    def test_negated_file_level_exception_is_rejected(self) -> None:
        with Repo(with_rule(per_file_ignore_rule("negated", "!tests/**"))) as repo:
            repo.write("pyproject.toml", per_file_ignore_config("!tests/**"))
            self.assert_violation(repo, "pattern '!tests/**' is negated")

    def test_file_level_exception_must_match_a_file(self) -> None:
        with Repo(with_rule(per_file_ignore_rule("stale", "nowhere/**"))) as repo:
            repo.write("pyproject.toml", per_file_ignore_config("nowhere/**"))
            self.assert_violation(repo, "pattern 'nowhere/**' matches no files")

    def test_unregistered_ci_job(self) -> None:
        with Repo() as repo:
            text = repo.read(".github/workflows/pr.yml")
            repo.write(
                ".github/workflows/pr.yml",
                text + "  deploy:\n    steps:\n      - run: ./deploy.sh\n",
            )
            self.assert_violation(repo, "CI job 'deploy'")

    def test_ignored_ci_job_with_reason_passes(self) -> None:
        with Repo() as repo:
            text = repo.read(".github/workflows/pr.yml")
            repo.write(
                ".github/workflows/pr.yml",
                text + "  deploy:\n    steps:\n      - run: ./deploy.sh\n",
            )
            repo.edit(
                "rules/registry.toml",
                "[discovery]\n",
                "[discovery]\n"
                'ignore_jobs = [{ job = "deploy", '
                'reason = "deploys, enforces nothing" }]\n',
            )
            repo.render()
            self.assertEqual(repo.messages(), [])


class RenderFreshnessTest(unittest.TestCase):
    def test_stale_rules_md(self) -> None:
        with Repo() as repo:
            repo.write(
                "docs/generated/rules.md",
                repo.read("docs/generated/rules.md") + "edited\n",
            )
            self.assertIn(
                "[registry] render: docs/generated/rules.md is stale",
                "\n".join(repo.messages()),
            )

    def test_missing_agents_markers(self) -> None:
        with Repo() as repo:
            repo.write("AGENTS.md", "# Agent Instructions\n")
            self.assertIn(
                "AGENTS.md has no generated rules block", "\n".join(repo.messages())
            )

    def test_stale_agents_block(self) -> None:
        with Repo() as repo:
            repo.edit("AGENTS.md", "Routes never issue SQL.", "Routes may issue SQL.")
            self.assertIn(
                "the rules block in AGENTS.md is stale", "\n".join(repo.messages())
            )

    def test_render_keeps_surrounding_agents_text(self) -> None:
        with Repo() as repo:
            agents = repo.read("AGENTS.md")
            self.assertTrue(
                agents.startswith("# Agent Instructions\n\nRead the guide.\n")
            )
            repo.render()
            self.assertEqual(repo.read("AGENTS.md"), agents)


class ExpiryTest(unittest.TestCase):
    def test_expired_review_by(self) -> None:
        with Repo(render=False) as repo:
            repo.edit(
                "rules/registry.toml",
                'statement = "Code is formatted."',
                'statement = "Code is formatted."\nreview_by = 2026-01-01',
            )
            repo.render()
            messages = "\n".join(repo.messages())
            self.assertIn("review_by 2026-01-01 has passed", messages)

    def test_expired_exception(self) -> None:
        with Repo(render=False) as repo:
            repo.edit(
                "rules/registry.toml",
                'kind = "mypy"\npaths = ["pkg"]',
                'kind = "mypy"\npaths = ["pkg"]\n[[rule.exception]]\n'
                'path = "pkg/routes.py"\n'
                'reason = "migrating"\nexpires = 2026-05-01',
            )
            repo.render()
            self.assertIn(
                "exception for pkg/routes.py expired on 2026-05-01",
                "\n".join(repo.messages()),
            )
            self.assertEqual(repo.messages(today=date(2026, 4, 1)), [])

    def test_exception_is_listed_in_rules_md(self) -> None:
        with Repo(render=False) as repo:
            repo.edit(
                "rules/registry.toml",
                'kind = "mypy"\npaths = ["pkg"]',
                'kind = "mypy"\npaths = ["pkg"]\n[[rule.exception]]\n'
                'path = "pkg/routes.py"\n'
                'reason = "migrating"\nexpires = 2026-12-01',
            )
            repo.render()
            self.assertIn(
                "exception for `pkg/routes.py` until 2026-12-01",
                repo.read("docs/generated/rules.md"),
            )


class BaselineTest(unittest.TestCase):
    def registry(self, extra: str):
        registry, violations = load_text(REGISTRY + extra)
        assert registry is not None and not violations, violations
        return registry

    def test_baseline_may_shrink(self) -> None:
        base = self.registry(RATCHETED_RULE.format(count=3))
        current = self.registry(RATCHETED_RULE.format(count=2))
        self.assertEqual(check_baselines(current, base)[0], [])

    def test_baseline_may_not_grow(self) -> None:
        base = self.registry(RATCHETED_RULE.format(count=2))
        current = self.registry(RATCHETED_RULE.format(count=3))
        violations = check_baselines(current, base)[0]
        self.assertIn("baseline grew from 2 to 3", str(violations[0]))

    def test_downgrade_needs_reason_and_issue(self) -> None:
        base = self.registry("")
        weakened = REGISTRY.replace(
            'status = "enforced"\nautofix = true\n[rule.enforcer]\nkind = "ruff"\n'
            'mode = "format"\n[[rule.ci]]\n'
            'workflow = ".github/workflows/pr.yml"\njob = "lint"\n'
            'invocation = "ruff format --check"',
            'status = "advisory"\nautofix = true\n[rule.enforcer]\nkind = "ruff"\n'
            'mode = "format"',
        )
        current, _ = load_text(weakened)
        assert current is not None
        violations = check_baselines(current, base)[0]
        self.assertIn("status weakened from enforced to advisory", str(violations[0]))

        def with_downgrade(section: str) -> Registry:
            registry, _ = load_text(
                weakened.replace('mode = "format"', f'mode = "format"\n{section}', 1)
            )
            assert registry is not None
            return registry

        reason_only = with_downgrade('[rule.downgrade]\nreason = "flaky"')
        self.assertIn(
            "status weakened from enforced to advisory",
            str(check_baselines(reason_only, base)[0][0]),
        )
        reason_and_issue = with_downgrade(
            '[rule.downgrade]\nreason = "flaky"\nissue = 912'
        )
        violations, notes = check_baselines(reason_and_issue, base)
        self.assertEqual(violations, [])
        self.assertIn(
            "rule ex.quality.format downgraded to advisory: flaky (issue #912)", notes
        )

    def test_ratcheting_an_enforced_rule_needs_reason(self) -> None:
        ratcheted = RATCHETED_RULE.format(count=50)
        enforced = ratcheted.replace(
            'status = "ratcheted"', 'status = "enforced"'
        ).split("[rule.baseline]")[0]
        base = self.registry(enforced)
        current = self.registry(ratcheted)

        violations = check_baselines(current, base)[0]
        self.assertEqual(
            [str(violation) for violation in violations],
            [
                "[ex.layers.ratcheted] baseline: status weakened from enforced to "
                'ratcheted. Fix: restore status = "enforced"; only a person may '
                'weaken a rule, by adding [rule.downgrade] with reason = "..." and '
                "issue = <number of the open issue that tracks restoring the rule>."
            ],
        )
        with_reason = self.registry(
            ratcheted.replace(
                "[rule.baseline]",
                '[rule.downgrade]\nreason = "a Pyright upgrade"\nissue = 912\n'
                "[rule.baseline]",
            )
        )
        self.assertEqual(check_baselines(with_reason, base)[0], [])

    def test_compares_against_git_base_ref(self) -> None:
        with Repo(with_rule(RATCHETED_RULE.format(count=1)), render=False) as repo:
            repo.write("rules/baselines/ratcheted.txt", "pkg/routes.py\n")
            repo.render()
            git = ["git", "-c", "user.email=t@example.com", "-c", "user.name=t"]
            subprocess.run(["git", "init", "-q"], cwd=repo.root, check=True)
            subprocess.run([*git, "add", "-A"], cwd=repo.root, check=True)
            subprocess.run([*git, "commit", "-qm", "base"], cwd=repo.root, check=True)
            repo.edit("rules/registry.toml", "count = 1", "count = 2")
            repo.write("rules/baselines/ratcheted.txt", "pkg/routes.py\npkg/other.py\n")
            repo.render()
            self.assertIn(
                "baseline grew from 1 to 2", "\n".join(repo.messages(base_ref="HEAD"))
            )


if __name__ == "__main__":
    unittest.main()
