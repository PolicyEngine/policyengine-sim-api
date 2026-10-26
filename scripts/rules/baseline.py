"""Compare ratchets against the base branch: baselines may only shrink."""

from __future__ import annotations

import os
import subprocess
from typing import TYPE_CHECKING

from scripts.rules.load import REGISTRY_PATH, load_text
from scripts.rules.model import (
    Kind,
    Registry,
    Rule,
    Status,
    Violation,
    normalized_package,
)

if TYPE_CHECKING:
    from pathlib import Path


# How a person, never an agent, records a weaker rule; each fix ends with it.
PERSON_DOWNGRADE = (
    'only a person may weaken a rule, by adding [rule.downgrade] with reason = "..." '
    "and issue = <number of the open issue that tracks restoring the rule>"
)


# The statuses a package or a rule may keep, or move to, without a downgrade.
_AT_LEAST_AS_STRONG = {
    Status.ENFORCED: (Status.ENFORCED,),
    Status.RATCHETED: (Status.ENFORCED, Status.RATCHETED),
}


def _weakens(old: Status, new: Status) -> bool:
    """Whether going from ``old`` to ``new`` weakens a rule that blocked merges.

    A ratcheted rule tolerates its baseline, so ratcheting an enforced rule
    weakens it too, with whatever count its new baseline starts at.
    """
    return old in _AT_LEAST_AS_STRONG and new not in _AT_LEAST_AS_STRONG[old]


class BaseUnavailableError(Exception):
    """The base ref could not be read."""


def default_base_ref() -> str | None:
    """Return ``origin/<GITHUB_BASE_REF>`` on a pull-request run, otherwise None."""
    base = os.environ.get("GITHUB_BASE_REF")
    return f"origin/{base}" if base else None


def load_base_registry(root: Path, ref: str) -> Registry | None:
    """Return the registry as it is on ``ref``, or None if the base has none yet.

    Raise BaseUnavailableError when ``ref`` does not name a commit.
    """
    verify = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    if verify.returncode != 0:
        raise BaseUnavailableError(ref)
    shown = subprocess.run(
        ["git", "show", f"{ref}:{REGISTRY_PATH.as_posix()}"],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    if shown.returncode != 0:
        return None
    registry, _ = load_text(shown.stdout)
    return registry


def check_baselines(
    current: Registry, base: Registry
) -> tuple[list[Violation], list[str]]:
    """Return (violations, notes)."""
    violations: list[Violation] = []
    notes: list[str] = []
    current_ids = {rule.id for rule in current.rules}
    for old in base.rules:
        new = current.rule(old.id)
        if new is None:
            notes.append(f"rule {old.id} was removed")
            continue
        if old.baseline and new.baseline and new.baseline.count > old.baseline.count:
            violations.append(
                Violation(
                    new.id,
                    "baseline",
                    f"baseline grew from {old.baseline.count} to {new.baseline.count}",
                    "fix the new violations instead of adding them to the baseline",
                )
            )
        if _weakens(old.status, new.status):
            if new.downgrade:
                notes.append(
                    f"rule {new.id} downgraded to {new.status}: "
                    f"{new.downgrade.reason} (issue #{new.downgrade.issue})"
                )
            else:
                violations.append(
                    Violation(
                        new.id,
                        "baseline",
                        f"status weakened from {old.status} to {new.status}",
                        f'restore status = "{old.status}"; {PERSON_DOWNGRADE}',
                    )
                )
    for rule_id in sorted(current_ids - {rule.id for rule in base.rules}):
        notes.append(f"rule {rule_id} is new")
        new_violations, new_notes = _check_new_rule(current.rule(rule_id))
        violations += new_violations
        notes += new_notes
    return violations, notes


def _check_new_rule(rule: Rule | None) -> tuple[list[Violation], list[str]]:
    """Check a rule the base branch doesn't have; return (violations, notes).

    A new rule that starts ratcheted with tolerated violations is weaker than
    an enforced one from the start, so it needs ``[rule.downgrade]`` too.
    """
    if (
        rule is None
        or rule.status is not Status.RATCHETED
        or rule.baseline is None
        or rule.baseline.count <= 0
    ):
        return [], []
    starts = f"starts ratcheted with {rule.baseline.count} tolerated violations"
    if rule.downgrade:
        return [], [
            f"rule {rule.id} {starts}: {rule.downgrade.reason} "
            f"(issue #{rule.downgrade.issue})"
        ]
    return [
        Violation(
            rule.id,
            "baseline",
            f"new rule {starts}",
            f"fix them and make the rule enforced; {PERSON_DOWNGRADE}",
        )
    ], []


def _pyright_listings(registry: Registry) -> dict[str, Rule]:
    """Map each package a Pyright rule lists to that rule.

    A package listed twice maps to the first rule, the one the Pyright check
    holds it to.
    """
    listings: dict[str, Rule] = {}
    for rule in registry.rules:
        if rule.enforcer.kind is not Kind.PYRIGHT:
            continue
        for project in rule.enforcer.get("projects", []):
            listings.setdefault(normalized_package(str(project)), rule)
    return listings


def check_pyright_packages(
    current: Registry, base: Registry, root: Path
) -> tuple[list[Violation], list[str]]:
    """Check that each package a blocking Pyright rule lists on the base still is.

    Return (violations, notes). A package from an enforced rule stays in an
    enforced Pyright rule, and one from a ratcheted rule in an enforced or
    ratcheted one, unless its ``pyproject.toml`` was deleted. Moving it to a
    weaker rule needs ``[rule.downgrade]`` on that rule. A rule that keeps the
    package under the same id is left to ``check_baselines``, which compares
    its status, and so is a ratcheted rule's baseline count.
    """
    violations: list[Violation] = []
    notes: list[str] = []
    head_listings = _pyright_listings(current)
    for package, base_rule in _pyright_listings(base).items():
        allowed = _AT_LEAST_AS_STRONG.get(base_rule.status)
        if allowed is None or not (root / package / "pyproject.toml").is_file():
            continue
        relist = (
            f"list {package} in an "
            f"{' or '.join(str(status) for status in allowed)} Pyright rule again"
        )
        head_rule = head_listings.get(package)
        if head_rule is None:
            violations.append(
                Violation(
                    base_rule.id,
                    "baseline",
                    f"package {package} left {base_rule.status} rule {base_rule.id}, "
                    "and no Pyright rule lists it now",
                    f"{relist}, or delete the package",
                )
            )
            continue
        if head_rule.status in allowed or head_rule.id == base_rule.id:
            continue
        move = (
            f"package {package} moved from {base_rule.status} rule {base_rule.id} "
            f"to {head_rule.status} rule {head_rule.id}"
        )
        if head_rule.downgrade:
            notes.append(
                f"{move}: {head_rule.downgrade.reason} "
                f"(issue #{head_rule.downgrade.issue})"
            )
        else:
            violations.append(
                Violation(
                    head_rule.id,
                    "baseline",
                    move,
                    f"{relist}; {PERSON_DOWNGRADE}",
                )
            )
    return violations, notes
