"""Simulation schema ownership: one chain, no executor dependency, none in Modal."""

from __future__ import annotations

from migration_support import REPOSITORY_ROOT
from ownership_checks import (
    declares_persistence_dependency,
    migration_chain_violations,
    modal_credential_violations,
    persistence_dependency_violations,
)

FIXTURES = REPOSITORY_ROOT / "rules" / "fixtures"
WORKFLOWS = REPOSITORY_ROOT / ".github" / "workflows"
WORKFLOW_SCRIPTS = REPOSITORY_ROOT / ".github" / "scripts"
MODAL_PROJECTS = (
    REPOSITORY_ROOT / "projects" / "policyengine-simulation-executor",
    REPOSITORY_ROOT / "projects" / "policyengine-simulation-gateway",
)
SIMULATION_DATABASE_VARIABLES = (
    "SIMULATION_DATABASE_URL",
    "SIMULATION_MIGRATION_DATABASE_URL",
)


def _texts(directory, pattern):
    return {
        path.name: path.read_text(encoding="utf-8")
        for path in sorted(directory.glob(pattern))
    }


def test_repository_has_one_simulation_migration_chain() -> None:
    assert migration_chain_violations(REPOSITORY_ROOT) == []


def test_rule_proof_one_migration_chain() -> None:
    """Proof for rule sim.persistence.one-migration-chain (rules/registry.toml)."""
    fixtures = FIXTURES / "sim.persistence.one-migration-chain"

    assert migration_chain_violations(fixtures / "bad_repository") != []
    assert migration_chain_violations(fixtures / "good_repository") == []


def test_modal_projects_never_depend_on_the_persistence_library() -> None:
    for project_root in MODAL_PROJECTS:
        assert persistence_dependency_violations(project_root) == []


def test_rule_proof_executor_independent() -> None:
    """Proof for rule sim.persistence.executor-independent (rules/registry.toml)."""
    fixtures = FIXTURES / "sim.persistence.executor-independent"

    assert declares_persistence_dependency(
        (fixtures / "bad_pyproject.toml").read_text(encoding="utf-8")
    )
    assert not declares_persistence_dependency(
        (fixtures / "good_pyproject.toml").read_text(encoding="utf-8")
    )


def test_modal_never_receives_simulation_database_credentials() -> None:
    assert (
        modal_credential_violations(
            _texts(WORKFLOWS, "*.yml"),
            _texts(WORKFLOW_SCRIPTS, "*.sh"),
        )
        == []
    )
    for project_root in MODAL_PROJECTS:
        for path in sorted((project_root / "src").rglob("*.py")):
            source = path.read_text(encoding="utf-8")
            for variable in SIMULATION_DATABASE_VARIABLES:
                assert variable not in source, path


def test_rule_proof_no_modal_database_credentials() -> None:
    """Proof for rule sim.persistence.no-modal-database-credentials."""
    fixtures = FIXTURES / "sim.persistence.no-modal-database-credentials"
    no_scripts: dict[str, str] = {}

    assert modal_credential_violations(
        {"bad.yml": (fixtures / "bad_workflow.yml").read_text(encoding="utf-8")},
        no_scripts,
    )
    assert not modal_credential_violations(
        {"good.yml": (fixtures / "good_workflow.yml").read_text(encoding="utf-8")},
        no_scripts,
    )
