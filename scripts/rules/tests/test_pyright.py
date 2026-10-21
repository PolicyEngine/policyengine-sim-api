"""Tests for the Pyright check: one package, its locked environment, its rule."""

from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts.rules.cli import main
from scripts.rules.load import load_text
from scripts.rules.pyright import (
    PYRIGHT_VERSION,
    Comparison,
    PyrightError,
    PyrightRunError,
    check_coverage,
    compare,
    errors_in_report,
    read_baseline,
    run_pyright,
    unlisted_packages,
)
from scripts.rules.tests.support import REGISTRY, Repo

ROOT = Path("/repository")
FIXTURES = Path(__file__).resolve().parents[3] / "rules" / "fixtures"
ENTRY_BACKEND = (
    "projects/policyengine-simulation-entry/src/policyengine_simulation_entry/"
    "stage12_backend.py"
)
GATEWAY_ENDPOINTS = (
    "projects/policyengine-simulation-gateway/src/policyengine_simulation_gateway/"
    "endpoints.py"
)
EXECUTOR_APP = "projects/policyengine-simulation-executor/src/modal/app.py"
APIS_INTEG_TESTS = "projects/policyengine-apis-integ/tests/simulation"
SERVICE_FILE = "projects/service/src/service.py"
LIBRARY_FILE = "libs/alpha/src/alpha.py"
UNBOUND = "reportPossiblyUnboundVariable"
ROUTE_UNBOUND_ENTRY = f'{SERVICE_FILE} :: {UNBOUND} :: "route" is possibly unbound'

PYRIGHT_RULES = """
[[rule]]
id = "ex.quality.pyright-library"
category = "quality"
statement = "The library has no Pyright errors."
rationale = "docs/guide.md"
scope = ["libs/alpha/src/*.py"]
tier = "mechanical"
status = "enforced"
autofix = false
[rule.enforcer]
kind = "pyright"
projects = ["libs/alpha"]
[[rule.ci]]
workflow = ".github/workflows/pr.yml"
job = "test"
invocation = "make test"

[[rule]]
id = "ex.quality.pyright-service"
category = "quality"
statement = "The service has no Pyright errors beyond its baseline."
rationale = "docs/guide.md"
scope = ["projects/service/src/*.py"]
tier = "mechanical"
status = "ratcheted"
autofix = false
[rule.enforcer]
kind = "pyright"
projects = ["projects/service"]
[[rule.ci]]
workflow = ".github/workflows/pr.yml"
job = "test"
invocation = "make test"
[rule.baseline]
file = "rules/baselines/pyright-service.txt"
count = 2
"""

SERVICE_BASELINE_HEADER = "# Tolerated Pyright errors in the service.\n#\n"
SERVICE_BASELINE = (
    SERVICE_BASELINE_HEADER + f"{ROUTE_UNBOUND_ENTRY}\n{ROUTE_UNBOUND_ENTRY}\n"
)

# A stand-in for uv: it records how it was called and prints the report the
# test left in the package directory, as `uv tool run ... pyright` would.
FAKE_UV = """#!/bin/sh
printf '%s|%s|%s\\n' "$(pwd -P)" "$*" "${PYRIGHT_PYTHON_FORCE_VERSION-unset}" \
    >> "$FAKE_UV_CALLS"
if [ "$1" = "sync" ]; then
    if [ -f uv-sync-stderr.txt ]; then cat uv-sync-stderr.txt >&2; fi
    exit "$(cat uv-sync-exit-code.txt 2>/dev/null || echo 0)"
fi
cat pyright-report.json 2>/dev/null
if [ -f pyright-stderr.txt ]; then cat pyright-stderr.txt >&2; fi
exit "$(cat pyright-exit-code.txt 2>/dev/null || echo 0)"
"""


def diagnostic(
    file: Path,
    line: int,
    message: str,
    *,
    rule: str | None = UNBOUND,
    severity: str = "error",
) -> dict[str, object]:
    """Return one ``generalDiagnostics`` entry, as ``pyright --outputjson`` prints."""
    entry: dict[str, object] = {
        "file": str(file),
        "severity": severity,
        "message": message,
        "range": {
            "start": {"line": line - 1, "character": 8},
            "end": {"line": line - 1, "character": 13},
        },
    }
    if rule is not None:
        entry["rule"] = rule
    return entry


def report(
    *diagnostics: dict[str, object],
    version: str = PYRIGHT_VERSION,
    files_analyzed: int = 3,
) -> dict[str, object]:
    """Return a ``pyright --outputjson`` report holding ``diagnostics``."""
    return {
        "version": version,
        "generalDiagnostics": list(diagnostics),
        "summary": {
            "filesAnalyzed": files_analyzed,
            "errorCount": sum(1 for d in diagnostics if d["severity"] == "error"),
        },
    }


def route_unbound(root: Path, line: int) -> dict[str, object]:
    """Return the service's tolerated error: ``route`` possibly unbound."""
    return diagnostic(root / SERVICE_FILE, line, '"route" is possibly unbound')


def pyright_error(
    path: str, line: int, message: str, rule: str = UNBOUND
) -> PyrightError:
    return PyrightError(path=path, line=line, column=9, rule=rule, message=message)


def proof_comparison(rule_id: str, fixture: str) -> Comparison:
    """Compare a proof fixture's recorded Pyright report with its baseline, if any.

    The reports were recorded with the pinned Pyright on main at ceb30e17, with
    the checkout's path replaced by /repository.
    """
    fixture_dir = FIXTURES / rule_id / fixture
    recorded_report = json.loads((fixture_dir / "pyright.json").read_text())
    baseline = fixture_dir / "baseline.txt"
    return compare(
        errors_in_report(recorded_report, ROOT),
        read_baseline(baseline) if baseline.exists() else [],
    )


class TestPyrightProof(unittest.TestCase):
    """Proofs: each Pyright rule's check rejects its bad fixture, accepts its good."""

    def test_rule_proof_pyright_libraries_and_entry(self) -> None:
        rule_id = "sim.quality.pyright-libraries-and-entry"
        # The entry's real errors before its fix: invocation_id is assigned
        # inside a span, which Pyright took to swallow exceptions.
        bad = proof_comparison(rule_id, "bad")

        invocation_id_unbound = (
            f'{ENTRY_BACKEND} :: {UNBOUND} :: "invocation_id" is possibly unbound'
        )
        self.assertEqual(
            [(error.line, error.baseline_entry) for error in bad.new_errors],
            [(550, invocation_id_unbound), (563, invocation_id_unbound)],
        )
        self.assertTrue(proof_comparison(rule_id, "good").passed)

    def test_rule_proof_pyright_gateway(self) -> None:
        rule_id = "sim.quality.pyright-gateway"
        # The baseline lists one of two "route" errors, and an error that no
        # longer occurs.
        bad = proof_comparison(rule_id, "bad")

        self.assertEqual(
            [(error.line, error.summary) for error in bad.new_errors],
            [(924, '"route" is possibly unbound')],
        )
        self.assertEqual(
            bad.stale_entries,
            (f'{GATEWAY_ENDPOINTS} :: {UNBOUND} :: "bundle" is possibly unbound',),
        )
        self.assertTrue(proof_comparison(rule_id, "good").passed)

    def test_rule_proof_pyright_executor(self) -> None:
        rule_id = "sim.quality.pyright-executor"
        bad = proof_comparison(rule_id, "bad")

        self.assertEqual(
            [(error.line, error.summary) for error in bad.new_errors],
            [(123, '"Secret" is not a known attribute of module "modal"')],
        )
        self.assertEqual(
            bad.stale_entries,
            (
                f"{EXECUTOR_APP} :: reportAttributeAccessIssue :: "
                '"Image" is not a known attribute of module "modal"',
            ),
        )
        self.assertTrue(proof_comparison(rule_id, "good").passed)

    def test_rule_proof_pyright_apis_integ(self) -> None:
        rule_id = "sim.quality.pyright-apis-integ"
        client_mismatch = (
            'Argument of type "Client | AuthenticatedClient" cannot be assigned to '
            'parameter "client" of type "AuthenticatedClient" in function '
            '"sync_detailed"'
        )
        bad = proof_comparison(rule_id, "bad")

        self.assertEqual(
            [(error.path, error.line) for error in bad.new_errors],
            [(f"{APIS_INTEG_TESTS}/test_calculate.py", 99)],
        )
        self.assertEqual(
            bad.stale_entries,
            (
                f"{APIS_INTEG_TESTS}/conftest.py :: reportArgumentType :: "
                f"{client_mismatch}",
            ),
        )
        self.assertTrue(proof_comparison(rule_id, "good").passed)


def unlisted_in_fixture(fixture: str) -> list[str]:
    """Return the packages no Pyright rule lists in a coverage proof fixture."""
    fixture_dir = FIXTURES / "sim.quality.every-package-type-checked" / fixture
    registry, problems = load_text((fixture_dir / "registry.toml").read_text())
    assert registry is not None, problems
    return unlisted_packages(registry, fixture_dir)


COVERAGE_RULE = """
[[rule]]
id = "ex.quality.every-package-type-checked"
category = "quality"
statement = "Every package is listed by a Pyright rule."
rationale = "docs/guide.md"
scope = ["libs/*/pyproject.toml"]
tier = "mechanical"
status = "enforced"
autofix = false
[rule.enforcer]
kind = "quality-guard"
name = "pyright-coverage"
"""


class CoverageTest(unittest.TestCase):
    """Every uv package is listed by a Pyright rule."""

    def test_the_guard_names_each_unlisted_package_and_the_fix(self) -> None:
        with Repo(
            {
                "rules/registry.toml": REGISTRY + PYRIGHT_RULES + COVERAGE_RULE,
                "libs/alpha/pyproject.toml": "",
                "projects/service/pyproject.toml": "",
                "projects/added/pyproject.toml": "",
            },
            render=False,
        ) as repo:
            self.assertEqual(
                check_coverage(repo.root),
                [
                    "[ex.quality.every-package-type-checked] pyright-coverage: "
                    "package projects/added has no Pyright rule, so CI doesn't "
                    "type-check it. Fix: add it to a Pyright rule's "
                    "enforcer.projects in rules/registry.toml and to the pyright "
                    "job's matrix in .github/workflows/pr.yml."
                ],
            )


class TestCoverageProof(unittest.TestCase):
    """Proof: the coverage guard rejects an unlisted package, accepts full lists."""

    def test_rule_proof_every_package_type_checked(self) -> None:
        # A directory holding only a lock file is not a package.
        self.assertEqual(unlisted_in_fixture("bad"), ["libs/beta"])
        self.assertEqual(unlisted_in_fixture("good"), [])


class ErrorsInReportTest(unittest.TestCase):
    def test_reads_errors_with_repository_paths_and_one_based_positions(self) -> None:
        errors = errors_in_report(
            report(diagnostic(ROOT / SERVICE_FILE, 12, '"call" is possibly unbound')),
            ROOT,
        )

        self.assertEqual(
            errors, [pyright_error(SERVICE_FILE, 12, '"call" is possibly unbound')]
        )

    def test_skips_warnings_and_information(self) -> None:
        errors = errors_in_report(
            report(
                diagnostic(ROOT / SERVICE_FILE, 3, "Import cycle", severity="warning"),
                diagnostic(ROOT / SERVICE_FILE, 4, "Unused", severity="information"),
            ),
            ROOT,
        )

        self.assertEqual(errors, [])

    def test_an_error_without_a_rule_is_named_error(self) -> None:
        errors = errors_in_report(
            report(
                diagnostic(ROOT / SERVICE_FILE, 5, "Expected expression", rule=None)
            ),
            ROOT,
        )

        self.assertEqual(
            [error.rule for error in errors], ["error"], "errors without a rule"
        )

    def test_errors_are_sorted_by_path_and_position(self) -> None:
        errors = errors_in_report(
            report(
                route_unbound(ROOT, 20),
                diagnostic(ROOT / LIBRARY_FILE, 7, '"value" is possibly unbound'),
                route_unbound(ROOT, 10),
            ),
            ROOT,
        )

        self.assertEqual(
            [(error.path, error.line) for error in errors],
            [(LIBRARY_FILE, 7), (SERVICE_FILE, 10), (SERVICE_FILE, 20)],
        )

    def test_baseline_entry_has_no_position_and_only_the_first_message_line(
        self,
    ) -> None:
        error = pyright_error(
            SERVICE_FILE,
            40,
            'Argument of type "str | None" cannot be assigned to parameter "name"\n'
            '\xa0\xa0Type "None" is not assignable to type "str"',
            rule="reportArgumentType",
        )

        self.assertEqual(
            error.baseline_entry,
            f"{SERVICE_FILE} :: reportArgumentType :: "
            'Argument of type "str | None" cannot be assigned to parameter "name"',
        )


class CompareTest(unittest.TestCase):
    def test_errors_matching_the_baseline_pass(self) -> None:
        errors = errors_in_report(
            report(route_unbound(ROOT, 10), route_unbound(ROOT, 20)), ROOT
        )

        comparison = compare(errors, [ROUTE_UNBOUND_ENTRY, ROUTE_UNBOUND_ENTRY])

        self.assertEqual(comparison, Comparison(new_errors=(), stale_entries=()))
        self.assertTrue(comparison.passed)

    def test_an_error_the_baseline_does_not_list_is_new(self) -> None:
        call_unbound = pyright_error(SERVICE_FILE, 30, '"call" is possibly unbound')

        comparison = compare([call_unbound], [])

        self.assertEqual(comparison.new_errors, (call_unbound,))
        self.assertFalse(comparison.passed)

    def test_one_more_error_than_the_baseline_lists_is_new(self) -> None:
        errors = errors_in_report(
            report(route_unbound(ROOT, 10), route_unbound(ROOT, 20)), ROOT
        )

        comparison = compare(errors, [ROUTE_UNBOUND_ENTRY])

        self.assertEqual(
            comparison.new_errors,
            (pyright_error(SERVICE_FILE, 20, '"route" is possibly unbound'),),
        )
        self.assertEqual(comparison.stale_entries, ())

    def test_an_entry_that_no_longer_occurs_is_stale(self) -> None:
        errors = errors_in_report(report(route_unbound(ROOT, 10)), ROOT)

        comparison = compare(errors, [ROUTE_UNBOUND_ENTRY, ROUTE_UNBOUND_ENTRY])

        self.assertEqual(comparison.new_errors, ())
        self.assertEqual(comparison.stale_entries, (ROUTE_UNBOUND_ENTRY,))
        self.assertFalse(comparison.passed)


class ReadBaselineTest(unittest.TestCase):
    def test_skips_blank_and_comment_lines(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            baseline = Path(directory) / "baseline.txt"
            baseline.write_text(SERVICE_BASELINE + "\n  # indented comment\n")

            self.assertEqual(
                read_baseline(baseline), [ROUTE_UNBOUND_ENTRY, ROUTE_UNBOUND_ENTRY]
            )


class FakeUvTestCase(unittest.TestCase):
    """Puts a fake ``uv`` first on PATH and records each call it receives."""

    def setUp(self) -> None:
        bin_directory = tempfile.TemporaryDirectory()
        self.addCleanup(bin_directory.cleanup)
        fake_uv = Path(bin_directory.name) / "uv"
        fake_uv.write_text(FAKE_UV)
        fake_uv.chmod(0o755)
        self.calls_file = Path(bin_directory.name) / "calls.txt"
        environment = mock.patch.dict(
            os.environ,
            {
                "PATH": f"{bin_directory.name}{os.pathsep}{os.environ['PATH']}",
                "FAKE_UV_CALLS": str(self.calls_file),
                "PYRIGHT_PYTHON_FORCE_VERSION": "latest",
            },
        )
        environment.start()
        self.addCleanup(environment.stop)

    def uv_calls(self) -> list[list[str]]:
        """Return each call as [working directory, arguments, forced version]."""
        if not self.calls_file.exists():
            return []
        return [line.split("|") for line in self.calls_file.read_text().splitlines()]


def make_package(root: Path, package: str) -> Path:
    """Create a package directory with a locked environment's interpreter."""
    package_dir = root / package
    interpreter = package_dir / ".venv" / "bin" / "python"
    interpreter.parent.mkdir(parents=True)
    interpreter.write_text("")
    return package_dir


def leave_report(package_dir: Path, pyright_report: object) -> None:
    """Leave the report the fake ``uv`` prints when run in ``package_dir``."""
    (package_dir / "pyright-report.json").write_text(json.dumps(pyright_report))


class RunPyrightTest(FakeUvTestCase):
    def setUp(self) -> None:
        super().setUp()
        root_directory = tempfile.TemporaryDirectory()
        self.addCleanup(root_directory.cleanup)
        self.package_dir = make_package(Path(root_directory.name), "libs/alpha")

    def assert_run_fails(self, fragment: str) -> None:
        with self.assertRaises(PyrightRunError) as raised:
            run_pyright(self.package_dir)
        self.assertIn(fragment, str(raised.exception))

    def test_runs_the_pinned_pyright_with_the_package_interpreter(self) -> None:
        leave_report(self.package_dir, report())

        self.assertEqual(run_pyright(self.package_dir), report())
        interpreter = self.package_dir.resolve() / ".venv" / "bin" / "python"
        self.assertEqual(
            self.uv_calls(),
            [
                [
                    str(self.package_dir.resolve()),
                    f"tool run --from pyright=={PYRIGHT_VERSION} pyright "
                    f"--outputjson --pythonpath {interpreter}",
                    "unset",
                ]
            ],
        )

    def test_requires_the_locked_environment(self) -> None:
        (self.package_dir / ".venv" / "bin" / "python").unlink()

        self.assert_run_fails(
            "there is no locked environment: "
            f"{self.package_dir.resolve() / '.venv' / 'bin' / 'python'} does not "
            "exist; run `uv sync --frozen --all-extras` in "
            f"{self.package_dir.resolve()} first"
        )
        self.assertEqual(self.uv_calls(), [])

    def test_rejects_a_run_that_did_not_finish(self) -> None:
        (self.package_dir / "pyright-exit-code.txt").write_text("3")
        (self.package_dir / "pyright-stderr.txt").write_text("Config file invalid\n")

        self.assert_run_fails("Pyright exited with status 3: Config file invalid")

    def test_rejects_output_that_is_not_a_json_report(self) -> None:
        self.assert_run_fails("Pyright printed no JSON report")

    def test_rejects_another_pyright_version(self) -> None:
        leave_report(self.package_dir, report(version="1.1.400"))

        self.assert_run_fails(
            f"Pyright reported version 1.1.400, not the pinned {PYRIGHT_VERSION}"
        )

    def test_rejects_a_run_that_checked_no_files(self) -> None:
        leave_report(self.package_dir, report(files_analyzed=0))

        self.assert_run_fails("Pyright checked no files")


class PyrightCommandTest(FakeUvTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.repo = Repo(
            {
                "rules/registry.toml": REGISTRY + PYRIGHT_RULES,
                "rules/baselines/pyright-service.txt": SERVICE_BASELINE,
            },
            render=False,
        )
        self.addCleanup(self.repo.__exit__)
        self.root = self.repo.root.resolve()
        self.library = make_package(self.root, "libs/alpha")
        self.service = make_package(self.root, "projects/service")
        leave_report(self.library, report())
        leave_report(
            self.service,
            report(route_unbound(self.root, 10), route_unbound(self.root, 20)),
        )

    def run_pyright_command(self, *arguments: str) -> tuple[int, list[str]]:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            status = main(["pyright", *arguments, "--root", str(self.root)])
        return status, output.getvalue().splitlines()

    def test_enforced_package_without_errors_passes(self) -> None:
        self.assertEqual(
            self.run_pyright_command("libs/alpha"),
            (
                0,
                [
                    "libs/alpha: passed with no Pyright errors "
                    "(ex.quality.pyright-library, enforced)",
                    "pyright: passed",
                ],
            ),
        )

    def test_enforced_package_with_an_error_fails(self) -> None:
        leave_report(
            self.library,
            report(
                diagnostic(
                    self.root / LIBRARY_FILE,
                    7,
                    'Argument of type "str | None" cannot be assigned to parameter '
                    '"name"\n\xa0\xa0Type "None" is not assignable to type "str"',
                    rule="reportArgumentType",
                )
            ),
        )

        self.assertEqual(
            self.run_pyright_command("libs/alpha"),
            (
                1,
                [
                    "libs/alpha: failed with 1 Pyright error, where "
                    "ex.quality.pyright-library (enforced) allows none",
                    f"  error: {LIBRARY_FILE}:7:9 - Argument of type "
                    '"str | None" cannot be assigned to parameter "name" '
                    "(reportArgumentType)",
                    '      Type "None" is not assignable to type "str"',
                    "  Fix: fix each error. Suppress one only as a last resort, "
                    "with # pyright: ignore[<rule>] and the reason.",
                    "pyright: failed",
                ],
            ),
        )

    def test_ratcheted_package_with_exactly_its_baseline_passes(self) -> None:
        self.assertEqual(
            self.run_pyright_command("projects/service"),
            (
                0,
                [
                    "projects/service: passed with 2 Pyright errors, each listed in "
                    "rules/baselines/pyright-service.txt "
                    "(ex.quality.pyright-service, ratcheted)",
                    "pyright: passed",
                ],
            ),
        )

    def test_ratcheted_package_with_a_new_error_fails(self) -> None:
        leave_report(
            self.service,
            report(
                route_unbound(self.root, 10),
                route_unbound(self.root, 20),
                diagnostic(self.root / SERVICE_FILE, 30, '"call" is possibly unbound'),
            ),
        )

        self.assertEqual(
            self.run_pyright_command("projects/service"),
            (
                1,
                [
                    "projects/service: failed, its Pyright errors differ from "
                    "rules/baselines/pyright-service.txt "
                    "(ex.quality.pyright-service, ratcheted)",
                    f"  new error: {SERVICE_FILE}:30:9 - "
                    f'"call" is possibly unbound ({UNBOUND})',
                    "  Fix: fix each new error; the baseline only shrinks.",
                    "pyright: failed",
                ],
            ),
        )

    def test_ratcheted_package_fails_when_a_listed_error_no_longer_occurs(
        self,
    ) -> None:
        leave_report(self.service, report(route_unbound(self.root, 10)))

        self.assertEqual(
            self.run_pyright_command("projects/service"),
            (
                1,
                [
                    "projects/service: failed, its Pyright errors differ from "
                    "rules/baselines/pyright-service.txt "
                    "(ex.quality.pyright-service, ratcheted)",
                    f"  no longer occurs: {ROUTE_UNBOUND_ENTRY}",
                    "  Fix: remove the entries that no longer occur with "
                    "python -m scripts.rules pyright projects/service "
                    "--write-baseline, then set count under [rule.baseline] "
                    "of ex.quality.pyright-service to the number left.",
                    "pyright: failed",
                ],
            ),
        )

    def test_a_new_error_and_a_stale_entry_are_both_reported_with_both_fixes(
        self,
    ) -> None:
        leave_report(
            self.service,
            report(
                route_unbound(self.root, 10),
                diagnostic(self.root / SERVICE_FILE, 30, '"call" is possibly unbound'),
            ),
        )

        status, lines = self.run_pyright_command("projects/service")

        self.assertEqual(status, 1)
        self.assertEqual(
            lines[1:],
            [
                f"  new error: {SERVICE_FILE}:30:9 - "
                f'"call" is possibly unbound ({UNBOUND})',
                f"  no longer occurs: {ROUTE_UNBOUND_ENTRY}",
                "  Fix: fix each new error; the baseline only shrinks.",
                "  Fix: remove the entries that no longer occur with "
                "python -m scripts.rules pyright projects/service "
                "--write-baseline, then set count under [rule.baseline] "
                "of ex.quality.pyright-service to the number left.",
                "pyright: failed",
            ],
        )

    def test_write_baseline_removes_entries_that_no_longer_occur(self) -> None:
        leave_report(self.service, report(route_unbound(self.root, 10)))

        self.assertEqual(
            self.run_pyright_command("projects/service", "--write-baseline"),
            (
                0,
                [
                    "projects/service: wrote rules/baselines/pyright-service.txt "
                    "with 1 entry; set count = 1 under [rule.baseline] of "
                    "ex.quality.pyright-service in rules/registry.toml",
                    "pyright: passed",
                ],
            ),
        )
        self.assertEqual(
            self.repo.read("rules/baselines/pyright-service.txt"),
            SERVICE_BASELINE_HEADER + ROUTE_UNBOUND_ENTRY + "\n",
        )

    def test_write_baseline_never_adds_a_new_error(self) -> None:
        leave_report(
            self.service,
            report(
                route_unbound(self.root, 10),
                diagnostic(self.root / SERVICE_FILE, 30, '"call" is possibly unbound'),
            ),
        )

        self.assertEqual(
            self.run_pyright_command("projects/service", "--write-baseline"),
            (
                1,
                [
                    "projects/service: failed, --write-baseline never adds an "
                    "error to rules/baselines/pyright-service.txt",
                    f"  new error: {SERVICE_FILE}:30:9 - "
                    f'"call" is possibly unbound ({UNBOUND})',
                    "  Fix: fix each new error; the baseline only shrinks.",
                    "pyright: failed",
                ],
            ),
        )
        self.assertEqual(
            self.repo.read("rules/baselines/pyright-service.txt"), SERVICE_BASELINE
        )

    def test_write_baseline_records_every_error_when_the_rule_is_adopted(
        self,
    ) -> None:
        (self.root / "rules/baselines/pyright-service.txt").unlink()

        status, lines = self.run_pyright_command("projects/service", "--write-baseline")

        self.assertEqual(
            (status, lines[0]),
            (
                0,
                "projects/service: wrote rules/baselines/pyright-service.txt "
                "with 2 entries; set count = 2 under [rule.baseline] of "
                "ex.quality.pyright-service in rules/registry.toml",
            ),
        )
        baseline = self.root / "rules/baselines/pyright-service.txt"
        self.assertEqual(
            read_baseline(baseline), [ROUTE_UNBOUND_ENTRY, ROUTE_UNBOUND_ENTRY]
        )
        self.assertIn(
            "python -m scripts.rules pyright projects/service --write-baseline",
            baseline.read_text(),
        )

    def test_write_baseline_sorts_its_entries(self) -> None:
        (self.root / "rules/baselines/pyright-service.txt").unlink()
        leave_report(
            self.service,
            report(
                route_unbound(self.root, 10),
                diagnostic(self.root / SERVICE_FILE, 20, '"call" is possibly unbound'),
            ),
        )

        self.run_pyright_command("projects/service", "--write-baseline")

        self.assertEqual(
            read_baseline(self.root / "rules/baselines/pyright-service.txt"),
            [
                f'{SERVICE_FILE} :: {UNBOUND} :: "call" is possibly unbound',
                ROUTE_UNBOUND_ENTRY,
            ],
        )

    def test_write_baseline_without_packages_rewrites_each_ratcheted_baseline(
        self,
    ) -> None:
        leave_report(self.service, report(route_unbound(self.root, 10)))

        self.assertEqual(
            self.run_pyright_command("--write-baseline"),
            (
                0,
                [
                    "projects/service: wrote rules/baselines/pyright-service.txt "
                    "with 1 entry; set count = 1 under [rule.baseline] of "
                    "ex.quality.pyright-service in rules/registry.toml",
                    "pyright: passed",
                ],
            ),
        )
        self.assertEqual([Path(call[0]) for call in self.uv_calls()], [self.service])

    def test_write_baseline_refuses_an_enforced_rule(self) -> None:
        self.assertEqual(
            self.run_pyright_command("libs/alpha", "--write-baseline"),
            (
                1,
                [
                    "libs/alpha: failed, ex.quality.pyright-library is enforced, "
                    "so libs/alpha has no baseline",
                    "  Fix: fix each error instead.",
                    "pyright: failed",
                ],
            ),
        )
        self.assertEqual(self.uv_calls(), [])

    def test_package_without_a_rule_fails(self) -> None:
        self.assertEqual(
            self.run_pyright_command("libs/unregistered"),
            (
                1,
                [
                    "libs/unregistered: failed, no Pyright rule lists it in "
                    "enforcer.projects",
                    "  Fix: add it to a Pyright rule in rules/registry.toml.",
                    "pyright: failed",
                ],
            ),
        )

    def test_a_pyright_run_error_fails_the_package(self) -> None:
        (self.library / ".venv" / "bin" / "python").unlink()

        status, lines = self.run_pyright_command("libs/alpha")

        self.assertEqual(status, 1)
        self.assertEqual(
            lines,
            [
                "libs/alpha: failed, Pyright could not check it: there is no "
                f"locked environment: {self.library}/.venv/bin/python does not "
                f"exist; run `uv sync --frozen --all-extras` in {self.library} first",
                "pyright: failed",
            ],
        )

    def test_sync_installs_the_locked_environment_before_checking(self) -> None:
        status, _ = self.run_pyright_command("libs/alpha", "--sync")

        self.assertEqual(status, 0)
        self.assertEqual(
            [call[:2] for call in self.uv_calls()],
            [
                [str(self.library), "sync --frozen --all-extras"],
                [
                    str(self.library),
                    f"tool run --from pyright=={PYRIGHT_VERSION} pyright "
                    f"--outputjson --pythonpath {self.library}/.venv/bin/python",
                ],
            ],
        )

    def test_sync_then_write_baseline(self) -> None:
        leave_report(self.service, report(route_unbound(self.root, 10)))

        status, _ = self.run_pyright_command(
            "projects/service", "--sync", "--write-baseline"
        )

        self.assertEqual(status, 0)
        self.assertEqual(
            [call[1].split()[0] for call in self.uv_calls()], ["sync", "tool"]
        )
        self.assertEqual(
            read_baseline(self.root / "rules/baselines/pyright-service.txt"),
            [ROUTE_UNBOUND_ENTRY],
        )

    def test_a_failed_sync_fails_the_package(self) -> None:
        (self.library / "uv-sync-exit-code.txt").write_text("2")
        (self.library / "uv-sync-stderr.txt").write_text(
            "error: The lockfile needs to be updated\n"
        )

        self.assertEqual(
            self.run_pyright_command("libs/alpha", "--sync"),
            (
                1,
                [
                    "libs/alpha: failed, Pyright could not check it: "
                    "`uv sync --frozen --all-extras` exited with status 2: "
                    "error: The lockfile needs to be updated",
                    "pyright: failed",
                ],
            ),
        )
        self.assertEqual(len(self.uv_calls()), 1)

    def test_without_packages_checks_every_package_a_rule_lists(self) -> None:
        status, lines = self.run_pyright_command()

        self.assertEqual(status, 0)
        self.assertEqual(
            [line.split(":")[0] for line in lines],
            ["libs/alpha", "projects/service", "pyright"],
        )
        self.assertEqual(
            [Path(call[0]) for call in self.uv_calls()],
            [self.library, self.service],
        )

    def test_package_arguments_apply_only_to_pyright(self) -> None:
        with (
            contextlib.redirect_stderr(io.StringIO()) as error_output,
            self.assertRaises(SystemExit) as raised,
        ):
            main(["validate", "libs/alpha", "--root", str(self.root)])

        self.assertEqual(raised.exception.code, 2)
        self.assertIn("apply only to pyright", error_output.getvalue())

    def assert_usage_error(self, arguments: list[str], fragment: str) -> None:
        with (
            contextlib.redirect_stderr(io.StringIO()) as error_output,
            self.assertRaises(SystemExit) as raised,
        ):
            main([*arguments, "--root", str(self.root)])
        self.assertEqual(raised.exception.code, 2)
        self.assertIn(fragment, error_output.getvalue())

    def test_the_ci_scripts_baseline_flag_is_refused(self) -> None:
        self.assert_usage_error(
            ["pyright", "--write-baselines"],
            "--write-baselines applies only to ci-scripts; pyright takes "
            "--write-baseline",
        )

    def test_list_packages_takes_no_other_pyright_options(self) -> None:
        for arguments in (
            ["pyright", "--list-packages", "libs/alpha"],
            ["pyright", "--list-packages", "--sync"],
            ["pyright", "--list-packages", "--write-baseline"],
        ):
            with self.subTest(arguments=arguments):
                self.assert_usage_error(
                    arguments, "--list-packages takes no packages or other options"
                )

    def test_write_baseline_without_a_ratcheted_rule_says_so(self) -> None:
        self.repo.edit(
            "rules/registry.toml", 'status = "ratcheted"', 'status = "enforced"'
        )

        self.assertEqual(
            self.run_pyright_command("--write-baseline"),
            (
                0,
                [
                    "No Pyright rule is ratcheted, so there is no baseline to write.",
                    "pyright: passed",
                ],
            ),
        )
        self.assertEqual(self.uv_calls(), [])

    def test_list_packages_prints_every_package_a_rule_lists(self) -> None:
        self.assertEqual(
            self.run_pyright_command("--list-packages"),
            (0, ["libs/alpha", "projects/service"]),
        )
        self.assertEqual(self.uv_calls(), [])


if __name__ == "__main__":
    unittest.main()
