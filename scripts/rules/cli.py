"""Command line for the rules tool.

``python -m scripts.rules {validate,render,guard,ci-scripts,pyright}``
"""

from __future__ import annotations

import argparse
from datetime import date
from pathlib import Path

from scripts.rules import ci_scripts, pyright
from scripts.rules.guard import REPO_ROOT, run_guard
from scripts.rules.load import load_registry
from scripts.rules.render import write_outputs
from scripts.rules.resolve import clear_caches
from scripts.rules.validate import validate


def _report(message: str) -> None:
    """Print one line of the command's output."""
    print(message)


def _print(violations: list, notes: list[str] | None = None) -> int:
    for note in notes or []:
        _report(f"note: {note}")
    for violation in violations:
        _report(f"  - {violation}")
    _report("rules: failed" if violations else "rules: passed")
    return 1 if violations else 0


def _ci_scripts_command(root: Path, *, write_baselines: bool) -> int:
    """Check the workflow scripts, or record today's findings as the baselines."""
    if not write_baselines:
        violations, notes = ci_scripts.run_check(root)
        return _print(violations, notes)
    registry, violations = load_registry(root)
    if registry is None or registry.ci_scripts is None:
        return _print(violations or ["no [ci_scripts] table"])
    ci_scripts.write_baselines(
        registry, ci_scripts.find(registry.ci_scripts, root), root
    )
    _report("wrote ci_scripts baselines; update the rules' baseline counts")
    return 0


def _pyright_command(
    root: Path,
    packages: list[str],
    *,
    sync: bool,
    write_baseline: bool,
    list_packages: bool,
) -> int:
    """Check each package against its rule, or rewrite ratcheted baselines.

    Without packages, check every package a Pyright rule lists or, with
    ``write_baseline``, rewrite the baseline of every ratcheted one.
    """
    registry, violations = load_registry(root)
    if registry is None:
        return _print(violations)
    if list_packages:
        for package in pyright.listed_packages(registry):
            _report(package)
        return 0
    failed = False
    default_packages = (
        pyright.ratcheted_packages(registry)
        if write_baseline
        else pyright.listed_packages(registry)
    )
    for package in packages or default_packages:
        passed, lines = pyright.check_package(
            registry, root, package, sync=sync, write_baseline=write_baseline
        )
        for line in lines:
            _report(line)
        failed = failed or not passed
    _report("pyright: failed" if failed else "pyright: passed")
    return 1 if failed else 0


def _registry_command(command: str, root: Path) -> int:
    """Validate the registry and, for ``render``, write its views once it is valid."""
    clear_caches()
    registry, violations = load_registry(root)
    if registry is None:
        return _print(violations)
    violations += validate(registry, root, date.today())
    if command != "render":
        return _print(violations)
    if violations:
        _report("render refused: fix the registry first")
        return _print(violations)
    for path in write_outputs(registry, root):
        _report(f"wrote {path.relative_to(root)}")
    return 0


def main(argv: list[str] | None = None) -> int:
    """Run one rules command and return 1 if it found violations.

    ``render`` refuses to write anything while the registry fails validation.
    """
    parser = argparse.ArgumentParser(
        prog="python -m scripts.rules", description=__doc__
    )
    parser.add_argument(
        "command", choices=["validate", "render", "guard", "ci-scripts", "pyright"]
    )
    parser.add_argument(
        "packages",
        nargs="*",
        help="pyright only: package directories to check (default: every one a "
        "Pyright rule lists)",
    )
    parser.add_argument("--root", type=Path, default=REPO_ROOT, help="repository root")
    parser.add_argument(
        "--base-ref",
        help="git ref to compare baselines against (default: origin/$GITHUB_BASE_REF)",
    )
    parser.add_argument(
        "--write-baselines",
        action="store_true",
        help=(
            "ci-scripts only: record today's findings as the baselines "
            "(when adopting the rule)"
        ),
    )
    parser.add_argument(
        "--sync",
        action="store_true",
        help=(
            "pyright only: run `uv sync --frozen --all-extras` in each package "
            "before checking it"
        ),
    )
    parser.add_argument(
        "--write-baseline",
        action="store_true",
        help=(
            "pyright only: rewrite each ratcheted package's baseline (by default "
            "every one) as the errors that occur now, refusing an error an "
            "existing baseline doesn't list"
        ),
    )
    parser.add_argument(
        "--list-packages",
        action="store_true",
        help="pyright only: print every package a Pyright rule lists",
    )
    args = parser.parse_args(argv)
    if args.command != "pyright" and (
        args.packages or args.sync or args.write_baseline or args.list_packages
    ):
        parser.error(
            "packages, --sync, --write-baseline and --list-packages apply only "
            "to pyright"
        )
    root = args.root.resolve()

    if args.command == "ci-scripts":
        return _ci_scripts_command(root, write_baselines=args.write_baselines)
    if args.command == "pyright":
        return _pyright_command(
            root,
            args.packages,
            sync=args.sync,
            write_baseline=args.write_baseline,
            list_packages=args.list_packages,
        )
    if args.command == "guard":
        result = run_guard(root, base_ref=args.base_ref)
        return _print(result.violations, result.notes)
    return _registry_command(args.command, root)
