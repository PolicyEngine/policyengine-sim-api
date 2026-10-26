"""The registry guard: every check in one pass."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from scripts.rules.adapters import check_adapters
from scripts.rules.baseline import (
    BaseUnavailableError,
    check_baselines,
    check_enforced_packages,
    default_base_ref,
    load_base_registry,
)
from scripts.rules.discover import discover_hidden
from scripts.rules.load import load_registry
from scripts.rules.model import Registry, Violation
from scripts.rules.render import (
    AGENTS_MD,
    RULES_MD,
    extract_block,
    render_agents_block,
    render_rules_md,
)
from scripts.rules.resolve import (
    clear_caches,
    resolve_ci,
    resolve_enforcer,
    resolve_proof_ci,
)
from scripts.rules.tracking_issues import check_tracking_issues
from scripts.rules.validate import validate

REPO_ROOT = Path(__file__).resolve().parents[2]
RENDER_FIX = "run python -m scripts.rules render and commit the result"


@dataclass
class GuardResult:
    """Guard violations, which fail the run, and notes, which only inform."""

    violations: list[Violation] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def _stale_outputs(registry: Registry, root: Path) -> list[Violation]:
    violations = []
    rules_md = root / RULES_MD
    if not rules_md.exists():
        violations.append(
            Violation(None, "render", f"{RULES_MD} is missing", RENDER_FIX)
        )
    elif rules_md.read_text() != render_rules_md(registry):
        violations.append(Violation(None, "render", f"{RULES_MD} is stale", RENDER_FIX))
    agents = root / AGENTS_MD
    block = extract_block(agents.read_text()) if agents.exists() else None
    if block is None:
        violations.append(
            Violation(
                None, "render", f"{AGENTS_MD} has no generated rules block", RENDER_FIX
            )
        )
    elif block != render_agents_block(registry):
        violations.append(
            Violation(
                None, "render", f"the rules block in {AGENTS_MD} is stale", RENDER_FIX
            )
        )
    return violations


def run_guard(
    root: Path = REPO_ROOT,
    *,
    base_ref: str | None = None,
    today: date | None = None,
    compare_base: bool = True,
) -> GuardResult:
    """Run every registry check, then compare the registry with the base ref.

    Stop after the schema errors if the registry does not load.
    """
    clear_caches()
    result = GuardResult()
    registry, schema_violations = load_registry(root)
    result.violations += schema_violations
    if registry is None:
        return result

    result.violations += validate(registry, root, today or date.today())
    for rule in registry.rules:
        result.violations += resolve_enforcer(rule, root)
        result.violations += resolve_ci(rule, root)
        result.violations += resolve_proof_ci(rule, root)
    result.violations += discover_hidden(registry, root)
    if registry.agents is not None:
        result.violations += check_adapters(registry.agents, root)
    result.violations += _stale_outputs(registry, root)
    violations, notes = check_tracking_issues(registry)
    result.violations += violations
    result.notes += notes

    if compare_base:
        _compare_with_base(registry, root, base_ref or default_base_ref(), result)
    return result


def _compare_with_base(
    registry: Registry, root: Path, ref: str | None, result: GuardResult
) -> None:
    """Add what comparing ``registry`` with the one on ``ref`` finds to ``result``.

    An unreadable base ref is a violation on GitHub Actions and only a note
    elsewhere.
    """
    if ref is None:
        result.notes.append(
            "no base ref (set --base-ref or GITHUB_BASE_REF); "
            "baseline comparison skipped"
        )
        return
    try:
        base = load_base_registry(root, ref)
    except BaseUnavailableError:
        if os.environ.get("GITHUB_ACTIONS"):
            result.violations.append(
                Violation(
                    None,
                    "baseline",
                    f"base ref {ref} is not available",
                    "check out with fetch-depth: 0",
                )
            )
        else:
            result.notes.append(
                f"base ref {ref} is not available; baseline comparison skipped"
            )
        return
    if base is None:
        result.notes.append(f"{ref} has no registry yet; baseline comparison skipped")
        return
    for violations, notes in (
        check_baselines(registry, base),
        check_enforced_packages(registry, base, root),
    ):
        result.violations += violations
        result.notes += notes


def check(root: Path = REPO_ROOT) -> list[str]:
    """Adapter for scripts/run_quality_guards.py: print the notes, return failures.

    The notes say what the guard didn't check, such as a tracking issue it
    had no token to look up.
    """
    result = run_guard(root)
    for note in result.notes:
        print(f"note: {note}")
    return [str(violation) for violation in result.violations]
