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
from scripts.rules.pyright import (
    PYRIGHT_VERSION,
    Comparison,
    PyrightError,
    PyrightRunError,
    compare,
    errors_in_report,
    read_baseline,
    run_pyright,
)
from scripts.rules.tests.support import REGISTRY, Repo

ROOT = Path("/repository")
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

    def test_list_packages_prints_every_package_a_rule_lists(self) -> None:
        self.assertEqual(
            self.run_pyright_command("--list-packages"),
            (0, ["libs/alpha", "projects/service"]),
        )
        self.assertEqual(self.uv_calls(), [])


if __name__ == "__main__":
    unittest.main()
