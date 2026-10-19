"""Checks behind the simulation schema ownership rules in rules/registry.toml.

Each check takes the files it inspects, so the rule tests run it on the
repository and the proof tests run it on the fixtures under
``rules/fixtures/<rule id>/``.
"""

from __future__ import annotations

import ast
import configparser
import re
from pathlib import Path
from typing import TYPE_CHECKING

import tomllib
import yaml

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator

CHAIN_DIRECTORY = Path("migrations")
CHAIN_CONFIG = CHAIN_DIRECTORY / "alembic.ini"
CHAIN_REVISIONS = CHAIN_DIRECTORY / "versions"
SIMULATION_SCHEMA = "simulation"
IGNORED_DIRECTORY_NAMES = frozenset(
    {".git", ".venv", "node_modules", "__pycache__", ".pytest_cache", ".ruff_cache"}
)
# Proof fixtures deliberately contain bad layouts.
IGNORED_RELATIVE_DIRECTORIES = (Path("rules") / "fixtures",)
PERSISTENCE_DISTRIBUTION = "policyengine-simulation-persistence"
PERSISTENCE_PACKAGE = "policyengine_simulation_persistence"
SIMULATION_DATABASE_CREDENTIAL = re.compile(r"SIMULATION_(?:MIGRATION_)?DATABASE_URL")
MODAL_SECRET_COMMAND = re.compile(r"\bmodal\s+secret\s+create\b")
MODAL_DEPLOY_COMMAND = re.compile(r"\bmodal\s+deploy\b")
SCHEMA_CHANGE = re.compile(
    r"\b(?:CREATE|ALTER|DROP|TRUNCATE)\s+(?:TABLE|TYPE|SCHEMA|INDEX)\b"
    r"|\b(?:create_all|drop_all)\s*\(",
    flags=re.IGNORECASE,
)


def _repository_files(root: Path, pattern: str) -> Iterator[Path]:
    for path in sorted(root.rglob(pattern)):
        relative = path.relative_to(root)
        if IGNORED_DIRECTORY_NAMES.intersection(relative.parts):
            continue
        if any(
            relative.is_relative_to(ignored) for ignored in IGNORED_RELATIVE_DIRECTORIES
        ):
            continue
        yield path


def _configures_simulation_version_table(environment_source: str) -> bool:
    for node in ast.walk(ast.parse(environment_source)):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "configure"
        ):
            continue
        for keyword in node.keywords:
            if keyword.arg != "version_table_schema":
                continue
            value = keyword.value
            if isinstance(value, ast.Name) and value.id == "SIMULATION_SCHEMA":
                return True
            if isinstance(value, ast.Constant) and value.value == SIMULATION_SCHEMA:
                return True
    return False


def migration_chain_violations(root: Path) -> list[str]:
    """Return how ``root`` departs from one simulation chain at ``migrations/``."""
    root = root.resolve()
    violations: list[str] = []
    configurations = [
        path.relative_to(root) for path in _repository_files(root, "alembic*.ini")
    ]
    if configurations != [CHAIN_CONFIG]:
        violations.append(
            f"expected only {CHAIN_CONFIG} as an Alembic configuration, found "
            f"{[str(path) for path in configurations]}"
        )
    for versions in _repository_files(root, "versions"):
        relative = versions.relative_to(root)
        if versions.is_dir() and relative != CHAIN_REVISIONS:
            if any(versions.glob("*.py")):
                violations.append(f"revision directory outside the chain: {relative}")
    for environment in _repository_files(root, "env.py"):
        relative = environment.relative_to(root)
        if relative != CHAIN_DIRECTORY / "env.py" and "alembic" in (
            environment.read_text(encoding="utf-8")
        ):
            violations.append(f"Alembic environment outside the chain: {relative}")

    config_path = root / CHAIN_CONFIG
    if not config_path.is_file():
        return violations
    parser = configparser.ConfigParser(interpolation=None)
    parser.read(config_path, encoding="utf-8")
    script_location = parser.get("alembic", "script_location", fallback="")
    resolved = Path(script_location.replace("%(here)s", str(config_path.parent)))
    if not resolved.is_absolute():
        resolved = root / resolved
    if resolved.resolve() != (root / CHAIN_DIRECTORY).resolve():
        violations.append(f"script_location {script_location!r} is not migrations/")
    environment_path = root / CHAIN_DIRECTORY / "env.py"
    if not environment_path.is_file() or not _configures_simulation_version_table(
        environment_path.read_text(encoding="utf-8")
    ):
        violations.append(
            "migrations/env.py must keep its version table in the simulation schema"
        )
    return violations


def schema_change_violations(source_roots: Iterable[Path]) -> list[str]:
    """Return runtime source files that create or change schema objects."""
    return [
        str(path)
        for source_root in source_roots
        for path in sorted(source_root.rglob("*.py"))
        if SCHEMA_CHANGE.search(path.read_text(encoding="utf-8"))
    ]


def declares_persistence_dependency(pyproject_text: str) -> bool:
    """Return whether a pyproject declares the persistence library anywhere."""
    project = tomllib.loads(pyproject_text).get("project", {})
    requirements = [
        *project.get("dependencies", []),
        *(
            requirement
            for group in project.get("optional-dependencies", {}).values()
            for requirement in group
        ),
    ]
    return any(
        re.split(r"[\s\[<>=!~;]", requirement, maxsplit=1)[0]
        == PERSISTENCE_DISTRIBUTION
        for requirement in requirements
    )


def persistence_dependency_violations(project_root: Path) -> list[str]:
    """Return where a project depends on or imports the persistence library."""
    violations: list[str] = []
    pyproject = project_root / "pyproject.toml"
    if declares_persistence_dependency(pyproject.read_text(encoding="utf-8")):
        violations.append(f"{pyproject} declares {PERSISTENCE_DISTRIBUTION}")
    lock = project_root / "uv.lock"
    if lock.is_file() and f'name = "{PERSISTENCE_DISTRIBUTION}"' in lock.read_text(
        encoding="utf-8"
    ):
        violations.append(f"{lock} locks {PERSISTENCE_DISTRIBUTION}")
    violations.extend(
        f"{path} imports {PERSISTENCE_PACKAGE}"
        for path in sorted((project_root / "src").rglob("*.py"))
        if PERSISTENCE_PACKAGE in path.read_text(encoding="utf-8")
    )
    return violations


def _step_text(step: dict) -> str:
    return "\n".join(
        str(value)
        for value in (step.get("run", ""), step.get("uses", ""), step.get("with", ""))
    )


def modal_credential_violations(
    workflow_texts: dict[str, str],
    script_texts: dict[str, str],
) -> list[str]:
    """Return Modal secret or deploy steps that carry simulation database URLs.

    ``workflow_texts`` and ``script_texts`` map a file name to its contents. A
    step counts as giving Modal a value when it creates a Modal secret or
    deploys a Modal application, directly or through a script that does.
    """
    modal_scripts = {
        name
        for name, text in script_texts.items()
        if MODAL_SECRET_COMMAND.search(text) or MODAL_DEPLOY_COMMAND.search(text)
    }
    violations = [
        f"{name} gives Modal a simulation database credential"
        for name in sorted(modal_scripts)
        if SIMULATION_DATABASE_CREDENTIAL.search(script_texts[name])
    ]
    for workflow_name, workflow_text in sorted(workflow_texts.items()):
        workflow = yaml.safe_load(workflow_text) or {}
        for job_name, job in (workflow.get("jobs") or {}).items():
            for step in job.get("steps") or []:
                text = _step_text(step)
                reaches_modal = (
                    MODAL_SECRET_COMMAND.search(text)
                    or MODAL_DEPLOY_COMMAND.search(text)
                    or any(name in text for name in modal_scripts)
                )
                if not reaches_modal:
                    continue
                environment = step.get("env") or {}
                carried = [
                    f"{key}={value}"
                    for key, value in environment.items()
                    if SIMULATION_DATABASE_CREDENTIAL.search(f"{key}={value}")
                ]
                if carried or SIMULATION_DATABASE_CREDENTIAL.search(text):
                    violations.append(
                        f"{workflow_name} job {job_name} step "
                        f"{step.get('name', '<unnamed>')!r} gives Modal a "
                        "simulation database credential"
                    )
    return violations
